"""Pipeline welding Execution Proof Contract (DPR + welding + NDT/NCR).

The contract proves only what its sources can prove. It can certify a start
without inventing a completion percentage; finish requires a known denominator,
complete weld traceability, accepted NDT, and no open linked NCR.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .. import audit, db
from . import conflicts

CONTRACT_TYPE = "PIPELINE_WELDING_EXECUTION"
CONTRACT_VERSION = "1.0"
ACCEPTED = re.compile(r"\b(?:accept(?:ed)?|pass(?:ed)?|clear(?:ed)?|approved|satisfactory)\b", re.I)
REJECTED = re.compile(r"\b(?:reject(?:ed)?|fail(?:ed)?|repair|required|unacceptable)\b", re.I)
OPEN = re.compile(r"\b(?:open|pending|hold|under investigation|in progress)\b", re.I)


def _raw(row: dict) -> dict:
    raw = db.jloads(row.get("raw_json"), {}) or {}
    typed = raw.get("typed") if isinstance(raw, dict) else None
    return typed if isinstance(typed, dict) else raw


def _weld_id(value: Any) -> str | None:
    value = str(value or "").strip().upper()
    if not value:
        return None
    return re.sub(r"-R\d+$", "", value)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
        return float(match.group(0)) if match else None


def _linked_evidence(project_id: str, activity_uid: int) -> list[dict]:
    return db.q(
        "SELECT DISTINCT e.* FROM evidence e JOIN evidence_links l ON l.evidence_id=e.id "
        "WHERE e.project_id=? AND l.activity_uid=? AND COALESCE(l.is_candidate,0)=0 "
        "AND (l.committed_uid=? OR l.human_decision='accepted' OR l.relation='supporting') "
        "ORDER BY e.date,e.created_at", [project_id, activity_uid, activity_uid])


def _scope_total(activity: dict, evidence: list[dict]) -> tuple[float | None, str | None]:
    custom = db.jloads(activity.get("custom_json"), {}) or {}
    for key in ("planned_welds", "total_welds", "planned_joints", "scope_quantity"):
        value = _number(custom.get(key))
        if value and value > 0:
            return value, "schedule custom field " + key
    for row in evidence:
        if row.get("document_type") not in {"DAILY_PROGRESS_REPORT", "DAILY_CONSTRUCTION_REPORT"}:
            continue
        raw = _raw(row)
        for key in ("total_qty", "total_quantity", "planned_quantity"):
            value = _number(raw.get(key))
            if value and value > 0 and str(row.get("unit") or raw.get("unit") or "").lower() in {
                    "joint", "joints", "weld", "welds"}:
                return value, "DPR stated total quantity"
    return None, None


def _gate(name: str, state: str, detail: str, count: int | None = None) -> dict:
    out = {"name": name, "state": state, "detail": detail}
    if count is not None:
        out["count"] = count
    return out


def evaluate(project_id: str, activity_uid: int) -> dict:
    activity = db.q1("SELECT * FROM activities WHERE project_id=? AND uid=?",
                     [project_id, activity_uid])
    if not activity:
        raise KeyError("no such activity")
    evidence = _linked_evidence(project_id, activity_uid)
    dpr = [row for row in evidence if row.get("document_type") in {
        "DAILY_PROGRESS_REPORT", "DAILY_CONSTRUCTION_REPORT", "SITE_DIARY"}]
    weld_rows = [row for row in evidence if row.get("document_type") == "WELDING_REGISTER"]
    ndt_rows = [row for row in evidence if row.get("document_type") == "NDT_REGISTER"]
    ncr_rows = [row for row in evidence if row.get("document_type") in {
        "NCR_REGISTER", "ISSUE_REGISTER"}]

    weld_ids = {_weld_id(_raw(row).get("weld_no")) for row in weld_rows}
    weld_ids.discard(None)
    accepted_ids: set[str] = set()
    rejected_ids: set[str] = set()
    for row in ndt_rows:
        raw = _raw(row)
        wid = _weld_id(raw.get("weld_no") or raw.get("original_weld_no"))
        result = str(raw.get("result") or raw.get("ndt_result") or
                     raw.get("re_ndt_result") or row.get("description") or "")
        if wid and ACCEPTED.search(result) and not REJECTED.search(result):
            accepted_ids.add(wid)
        if wid and REJECTED.search(result):
            rejected_ids.add(wid)
    open_ncr = []
    for row in ncr_rows:
        raw = _raw(row)
        state = str(raw.get("status") or row.get("description") or "")
        if OPEN.search(state) and not re.search(r"\bclosed\b", state, re.I):
            open_ncr.append(row)
    scope_total, scope_basis = _scope_total(activity, evidence)
    accepted_scope = weld_ids & accepted_ids
    gates = [
        _gate("DPR execution record", "pass" if dpr else "missing",
              "At least one linked daily execution observation is present." if dpr else
              "No linked DPR/site diary execution observation.", len(dpr)),
        _gate("Weld traceability", "pass" if weld_ids else "missing",
              f"{len(weld_ids)} unique weld identity/identities are linked." if weld_ids else
              "No linked welding-register weld identity.", len(weld_ids)),
        _gate("Independent NDT source", "pass" if ndt_rows else "missing",
              f"{len(ndt_rows)} linked NDT record(s)." if ndt_rows else
              "No separately linked NDT register row; embedded welding results alone are insufficient.",
              len(ndt_rows)),
        _gate("NDT acceptance coverage",
              "fail" if rejected_ids else ("pass" if weld_ids and weld_ids <= accepted_ids else "incomplete"),
              f"{len(accepted_scope)} of {len(weld_ids)} traced welds have an accepted result; "
              f"{len(rejected_ids)} have a rejected/repair result."),
        _gate("Open NCR hold", "fail" if open_ncr else "pass",
              f"{len(open_ncr)} linked NCR(s) remain open/held." if open_ncr else
              "No linked open NCR blocks acceptance.", len(open_ncr)),
        _gate("Scope denominator", "pass" if scope_total else "warning",
              (f"Known scope is {scope_total:g} welds/joints from {scope_basis}." if scope_total else
               "No authoritative total weld/joint denominator; completion percent and finish are not certified.")),
    ]
    missing = [gate for gate in gates[:3] if gate["state"] == "missing"]
    failed = [gate for gate in gates if gate["state"] == "fail"]
    incomplete = [gate for gate in gates if gate["state"] == "incomplete"]
    status = "insufficient_evidence" if missing else (
        "blocked" if failed or incomplete else "admissible")
    dates = sorted(str(row.get("date"))[:10] for row in dpr + weld_rows + ndt_rows
                   if row.get("date"))
    recommended: dict[str, Any] = {}
    if dates and dpr and weld_ids:
        recommended["actualStart"] = dates[0]
    if scope_total:
        recommended["percentComplete"] = round(min(100.0, len(accepted_scope) / scope_total * 100), 2)
        if status == "admissible" and len(accepted_scope) >= scope_total and dates:
            recommended.update({"actualFinish": dates[-1], "percentComplete": 100.0,
                                "remainingDuration": 0.0})
    return {
        "contract_type": CONTRACT_TYPE, "contract_version": CONTRACT_VERSION,
        "project_id": project_id, "activity_uid": activity_uid,
        "activity": {"uid": activity_uid, "display_id": activity.get("display_id"),
                     "name": activity.get("name"), "wbs": activity.get("wbs")},
        "status": status, "gates": gates,
        "metrics": {"dpr_records": len(dpr), "weld_records": len(weld_rows),
                    "unique_welds": len(weld_ids), "ndt_records": len(ndt_rows),
                    "accepted_welds": len(accepted_scope), "rejected_welds": len(rejected_ids),
                    "open_ncrs": len(open_ncr), "scope_total": scope_total,
                    "scope_basis": scope_basis},
        "recommended_actuals": recommended,
        "evidence_ids": [row["id"] for row in evidence],
        "sources": sorted({str(row.get("source_file")) for row in evidence if row.get("source_file")}),
        "limitations": (["Completion is not certified without an authoritative scope denominator."]
                        if not scope_total else []),
    }


def certify(project_id: str, activity_uid: int, *, created_by: str = "system") -> dict:
    payload = evaluate(project_id, activity_uid)
    proof_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    proof_hash = hashlib.sha256(proof_json.encode("utf-8")).hexdigest()
    existing = db.q1("SELECT * FROM actuals_certificates WHERE project_id=? AND activity_uid=? "
                     "AND proof_hash=? ORDER BY created_at DESC LIMIT 1",
                     [project_id, activity_uid, proof_hash])
    if existing:
        return shape(existing)
    contract = db.q1("SELECT * FROM execution_contracts WHERE project_id=? AND activity_uid=? "
                     "AND contract_type=? AND contract_version=?",
                     [project_id, activity_uid, CONTRACT_TYPE, CONTRACT_VERSION])
    if not contract:
        contract_id = db.insert("execution_contracts", {
            "project_id": project_id, "activity_uid": activity_uid,
            "contract_type": CONTRACT_TYPE, "contract_version": CONTRACT_VERSION,
            "config_json": db.jdumps({"required_sources": ["DPR", "WELDING_REGISTER", "NDT_REGISTER"],
                                      "blocking_source": "open NCR"}),
            "state": "active", "updated_at": db.now()})
    else:
        contract_id = contract["id"]
    previous = db.q1("SELECT id FROM actuals_certificates WHERE project_id=? AND activity_uid=? "
                     "ORDER BY created_at DESC LIMIT 1", [project_id, activity_uid])
    cert_id = db.insert("actuals_certificates", {
        "project_id": project_id, "activity_uid": activity_uid,
        "contract_id": contract_id, "contract_type": CONTRACT_TYPE,
        "contract_version": CONTRACT_VERSION, "status": payload["status"],
        "proof_hash": proof_hash, "certificate_json": proof_json,
        "supersedes_id": (previous or {}).get("id"), "created_by": created_by})
    if payload["status"] == "blocked":
        conflicts.upsert(
            project_id, kind="execution_proof_blocked",
            detail="Pipeline execution proof is blocked by NDT coverage, rejection, or an open NCR.",
            entity_type="actuals_certificate", entity_id=cert_id,
            activity_uid=activity_uid, field="execution_proof",
            evidence_ids=payload["evidence_ids"])
    audit.record(project_id, actor=created_by, actor_type="system",
                 action="actuals_certificate_created", source="execution_proof_contract",
                 entity_type="actuals_certificate", entity_id=cert_id,
                 verification=payload["status"], result=payload["status"],
                 detail={"proof_hash": proof_hash, "contract_version": CONTRACT_VERSION})
    return shape(db.q1("SELECT * FROM actuals_certificates WHERE id=?", [cert_id]) or {})


def certify_project(project_id: str, *, created_by: str = "system") -> dict:
    rows = db.q(
        "SELECT DISTINCT l.activity_uid FROM evidence_links l JOIN evidence e ON e.id=l.evidence_id "
        "WHERE l.project_id=? AND l.activity_uid IS NOT NULL AND COALESCE(l.is_candidate,0)=0 "
        "AND e.document_type IN ('DAILY_PROGRESS_REPORT','DAILY_CONSTRUCTION_REPORT','SITE_DIARY',"
        "'WELDING_REGISTER','NDT_REGISTER','NCR_REGISTER')", [project_id])
    certificates = [certify(project_id, int(row["activity_uid"]), created_by=created_by)
                    for row in rows]
    return {"certificates": certificates, "count": len(certificates)}


def shape(row: dict) -> dict:
    out = dict(row)
    out["certificate"] = db.jloads(out.pop("certificate_json", None), {}) or {}
    return out


def list_for_project(project_id: str) -> list[dict]:
    rows = db.q(
        "SELECT c.* FROM actuals_certificates c JOIN ("
        "SELECT activity_uid,MAX(created_at) latest FROM actuals_certificates "
        "WHERE project_id=? GROUP BY activity_uid) x "
        "ON x.activity_uid=c.activity_uid AND x.latest=c.created_at "
        "WHERE c.project_id=? ORDER BY c.created_at DESC", [project_id, project_id])
    return [shape(row) for row in rows]
