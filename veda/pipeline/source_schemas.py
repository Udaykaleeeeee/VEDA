"""Typed, versioned schemas and relevance policy for execution sources.

The schemas intentionally describe the minimum identity-bearing fields rather
than one contractor's exact spreadsheet. Header aliases are normalized into a
stable payload while the original row is retained verbatim in ``raw_json``.
"""
from __future__ import annotations

import re
from typing import Any


SCHEMA_VERSION = "1.0"


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9%]+", "_", str(value or "").strip().lower()).strip("_")


COMMON = {
    "date": ("date", "report_date", "work_date", "date_received", "inspection_date"),
    "description": ("description", "activity", "work_description", "work_done", "remarks", "comments"),
    "status": ("status", "state", "disposition"),
    "location": ("location", "area", "spread", "storage_location"),
    "chainage": ("chainage", "chainage_from", "ch", "km"),
    "quantity": ("quantity", "qty", "achieved_today", "actual_today"),
    "unit": ("unit", "uom"),
    "progress": ("progress", "progress_pct", "progress_%", "percent_complete", "%_complete", "completion"),
}


SCHEMAS: dict[str, dict] = {
    "dpr": {
        "schema": "DPR", "version": SCHEMA_VERSION,
        "document_type": "DAILY_PROGRESS_REPORT", "observation_type": "activity_progress",
        "filename": (r"\bdpr\b", r"daily[_\s-]+(?:progress|construction)"),
        "anchors": (("dpr_no", "report_no"), ("description", "activity", "work_description", "remarks")),
        "fields": {**COMMON,
            "record_id": ("dpr_no", "report_no", "record_id"),
            "contractor": ("contractor", "company"), "crew": ("crew", "crew_id"),
            "discipline": ("discipline", "trade"), "author": ("reported_by", "author", "foreman"),
        },
    },
    "welding": {
        "schema": "WELDING_REGISTER", "version": SCHEMA_VERSION,
        "document_type": "WELDING_REGISTER", "observation_type": "quality_gate",
        "filename": (r"weld(?:ing)?[_\s-]+register", r"weld[_\s-]+log"),
        "anchors": (("weld_no", "weld_id", "joint_no"), ("welder_id", "procedure", "wps")),
        "fields": {**COMMON,
            "weld_no": ("weld_no", "weld_id", "joint_no"),
            "welder_id": ("welder_id", "welder"), "crew": ("crew", "crew_id"),
            "procedure": ("procedure", "wps", "weld_procedure"),
            "joint_type": ("joint_type", "joint"), "ndt_method": ("ndt_method", "test_method"),
            "ndt_result": ("ndt_result", "result", "acceptance"),
            "repair_required": ("repair_required", "repair", "rework_required"),
        },
    },
    "ndt": {
        "schema": "NDT_REGISTER", "version": SCHEMA_VERSION,
        "document_type": "NDT_REGISTER", "observation_type": "quality_gate",
        "filename": (r"\bndt\b", r"radiograph", r"ultrasonic", r"rt[_\s-]+report"),
        "anchors": (("weld_no", "weld_id", "joint_no"), ("result", "ndt_result", "acceptance")),
        "fields": {**COMMON,
            "report_no": ("report_no", "ndt_no", "test_no"),
            "weld_no": ("weld_no", "weld_id", "joint_no", "original_weld_no"),
            "ndt_method": ("ndt_method", "rt_technique", "re_ndt_method", "test_method"),
            "result": ("result", "ndt_result", "re_ndt_result", "acceptance"),
            "defect": ("defect_found", "defect_type", "indication"),
            "comments": ("comments", "remarks", "notes"),
        },
    },
    "ncr": {
        "schema": "NCR_REGISTER", "version": SCHEMA_VERSION,
        "document_type": "NCR_REGISTER", "observation_type": "issue",
        "filename": (r"\bncr\b", r"non[_\s-]+conformance"),
        "anchors": (("ncr_no", "ncr_id"), ("description", "non_conformance", "issue")),
        "fields": {**COMMON,
            "ncr_no": ("ncr_no", "ncr_id", "reference"),
            "discipline": ("discipline", "trade"), "severity": ("severity", "priority"),
            "action": ("action", "corrective_action", "disposition_action"),
            "actual_close_date": ("actual_close_date", "closed_date", "date_closed"),
        },
    },
    "materials": {
        "schema": "MATERIAL_REGISTER", "version": SCHEMA_VERSION,
        "document_type": "MATERIAL_REGISTER", "observation_type": "material",
        "filename": (r"material[_\s-]+(?:receiv|register|log)", r"\bmrr\b", r"\bmrn\b"),
        "anchors": (("material", "material_description", "item_description"),
                    ("heat_no", "po_number", "mrn_no", "mrr_no")),
        "fields": {**COMMON,
            "receipt_no": ("mrr_no", "mrn_no", "receipt_no"),
            "material": ("material", "material_description", "item_description"),
            "specification": ("specification", "spec", "grade"),
            "heat_no": ("heat_no", "heat_number", "batch_no", "lot_no"),
            "po_number": ("po_number", "po_no", "purchase_order"),
            "supplier": ("supplier", "vendor"), "condition": ("condition",),
            "certificate_received": ("certificate_received", "mtc_received", "certificate"),
            "comments": ("comments", "remarks", "notes"),
        },
    },
}


def classify(headers: list[Any], filename: str = "") -> dict:
    """Return the strongest typed schema, or an explicit generic register."""
    header_set = {norm(h) for h in headers if norm(h)}
    filename_norm = str(filename or "").lower().replace("_", " ")
    ranked: list[tuple[float, str, list[str]]] = []
    for key, schema in SCHEMAS.items():
        matched_groups = []
        for group in schema["anchors"]:
            hit = next((norm(alias) for alias in group if norm(alias) in header_set), None)
            if hit:
                matched_groups.append(hit)
        filename_hit = any(re.search(pattern, filename_norm, re.I)
                           for pattern in schema["filename"])
        score = len(matched_groups) / len(schema["anchors"])
        if filename_hit:
            score += 0.25
        if len(matched_groups) == len(schema["anchors"]):
            ranked.append((score, key, matched_groups))
    if not ranked:
        return {"key": "generic", "schema": "GENERIC_REGISTER", "version": SCHEMA_VERSION,
                "document_type": "EXCEL_PROGRESS_REGISTER",
                "observation_type": "activity_progress", "confidence": 0.35,
                "signals": sorted(header_set)[:12], "fields": COMMON}
    score, key, signals = max(ranked, key=lambda row: row[0])
    return {"key": key, **SCHEMAS[key], "confidence": min(0.99, round(score, 3)),
            "signals": signals}


def canonicalize(headers: list[Any], row: list[Any], schema: dict) -> dict:
    values = {norm(headers[i]): row[i] for i in range(min(len(headers), len(row)))}
    out: dict[str, Any] = {}
    for field, aliases in (schema.get("fields") or {}).items():
        for alias in aliases:
            value = values.get(norm(alias))
            if value not in (None, ""):
                out[field] = value
                break
    out["_schema"] = schema.get("schema")
    out["_schema_version"] = schema.get("version")
    return out


def relevance(document_type: str, *, filename: str = "", text: str = "") -> dict:
    """Classify whether a source can prove execution, only provide context, or is reference material."""
    evidence_types = {"DAILY_CONSTRUCTION_REPORT", "DAILY_PROGRESS_REPORT", "SITE_DIARY",
                      "EXCEL_PROGRESS_REGISTER", "WELDING_REGISTER", "NDT_REGISTER",
                      "NCR_REGISTER", "MATERIAL_REGISTER", "ISSUE_REGISTER", "CHAT_EXPORT"}
    blob = (str(filename or "") + "\n" + str(text or "")[:12000]).lower()
    reference_hits = [label for label, pattern in (
        ("user/manual language", r"\b(?:user|training|reference|installation)\s+(?:guide|manual)\b"),
        ("table of contents", r"\btable\s+of\s+contents\b"),
        ("API documentation", r"\b(?:rest|soap)\s+api\b|\bendpoint\b.*\brequest\b"),
        ("copyright notice", r"\bcopyright\s+©?\s*\d{4}\b"),
    ) if re.search(pattern, blob, re.I)]
    if len(reference_hits) >= 2 or document_type == "REFERENCE_DOCUMENT":
        return {"state": "reference", "score": 0.0,
                "reason": "Reference material is searchable context, not proof of field execution: " +
                          ", ".join(reference_hits[:3])}
    if document_type in evidence_types:
        return {"state": "evidence", "score": 0.9,
                "reason": "Typed execution source: " + document_type}
    if document_type in {"RESOURCE_REPORT", "WEEKLY_REPORT"}:
        return {"state": "context", "score": 0.45,
                "reason": "Project context; individual rows require activity-level identity before use"}
    return {"state": "unclassified", "score": 0.2,
            "reason": "No typed execution-source schema matched"}
