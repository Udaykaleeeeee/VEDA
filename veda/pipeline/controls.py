"""Execution controls layered on top of VEDA's immutable evidence model.

These records never update a schedule directly. New-scope activity suggestions
are deliberately routed through :mod:`proposals`, which retains validation,
dry-run, human approval and independent verification as separate gates.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .. import audit, db
from . import conflicts, proposals


HINDRANCE_STATUSES = {"open", "monitoring", "cleared", "closed"}
CONSTRAINT_STATUSES = {"open", "in_progress", "cleared", "waived"}
CONSTRAINT_TYPES = {
    "drawing", "material", "permit", "access", "workfront", "crew",
    "equipment", "inspection", "ndt", "safety", "interface", "other",
}
SEVERITIES = {"low", "medium", "high", "critical"}
CRITICALITIES = {"watch", "blocker"}
BIM_TYPES = {"IFC_GUID", "REVIT_UNIQUE_ID", "COBIE_TAG", "BIM_OBJECT_ID", "MODEL_ELEMENT_ID"}
_NEW_SCOPE_HINT = re.compile(
    r"\b(?:additional|new|extra|unplanned|variation|change\s+order|scope\s+change|"
    r"added\s+scope|out[- ]of[- ]scope|outside\s+(?:the\s+)?(?:current\s+)?schedule)\b",
    re.IGNORECASE,
)
_REFERENCE_SOURCE_HINTS = (
    "ground_truth", "challenge_difficulty", "risk_register", "milestone_register",
    "resource_master", "calendar_definition", "standards_reference",
)


class BIMIdentityConflict(ValueError):
    """A model identifier is already bound to a different activity."""


def _text(value: Any, limit: int = 1000) -> str:
    return str(value or "").strip()[:limit]


def _iso_day(value: Any, *, field: str, optional: bool = True) -> str | None:
    raw = _text(value, 32)
    if not raw and optional:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(field + " must be an ISO date (YYYY-MM-DD)") from exc


def _number(value: Any, *, field: str, minimum: float | None = None,
            maximum: float | None = None) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(field + " must be numeric") from exc
    if minimum is not None and result < minimum:
        raise ValueError(field + " must be at least " + str(minimum))
    if maximum is not None and result > maximum:
        raise ValueError(field + " must be at most " + str(maximum))
    return result


def _ids(value: Any, *, limit: int = 100) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[\s,;]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    return list(dict.fromkeys(_text(item, 100) for item in values if _text(item, 100)))[:limit]


def _uids(project_id: str, value: Any, *, required: bool = False) -> list[int]:
    raw = _ids(value)
    try:
        uids = list(dict.fromkeys(int(item) for item in raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("activity UIDs must be integers") from exc
    if required and not uids:
        raise ValueError("at least one activity UID is required")
    if uids:
        ph = ",".join("?" for _ in uids)
        found = {int(row["uid"]) for row in db.q(
            "SELECT uid FROM activities WHERE project_id=? AND uid IN (" + ph + ")",
            [project_id] + uids)}
        missing = [uid for uid in uids if uid not in found]
        if missing:
            raise ValueError("unknown activity UID(s): " + ", ".join(map(str, missing)))
    return uids[:100]


def _evidence_ids(project_id: str, value: Any) -> list[str]:
    ids = _ids(value)
    if ids:
        ph = ",".join("?" for _ in ids)
        found = {row["id"] for row in db.q(
            "SELECT id FROM evidence WHERE project_id=? AND id IN (" + ph + ")",
            [project_id] + ids)}
        missing = [item for item in ids if item not in found]
        if missing:
            raise ValueError("unknown evidence id(s): " + ", ".join(missing))
    return ids


def shape_hindrance(row: dict) -> dict:
    item = dict(row)
    item["activity_uids"] = db.jloads(item.pop("activity_uids_json", None), []) or []
    item["evidence_ids"] = db.jloads(item.pop("evidence_ids_json", None), []) or []
    item["context"] = db.jloads(item.pop("context_json", None), {}) or {}
    return item


def shape_constraint(row: dict) -> dict:
    item = dict(row)
    item["evidence_ids"] = db.jloads(item.pop("evidence_ids_json", None), []) or []
    return item


def shape_bim(row: dict) -> dict:
    return dict(row)


def list_hindrances(project_id: str, status: str = "") -> list[dict]:
    sql = "SELECT * FROM hindrances WHERE project_id=?"
    params: list[Any] = [project_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'monitoring' THEN 1 ELSE 2 END, COALESCE(started_on,reported_on) DESC, created_at DESC"
    return [shape_hindrance(row) for row in db.q(sql, params)]


def create_hindrance(project_id: str, payload: dict, *, actor: str = "human") -> dict:
    title = _text(payload.get("title"), 240)
    description = _text(payload.get("description"), 4000)
    if not title:
        raise ValueError("hindrance title is required")
    status = _text(payload.get("status") or "open", 24).lower()
    severity = _text(payload.get("severity") or "medium", 24).lower()
    if status not in HINDRANCE_STATUSES:
        raise ValueError("unsupported hindrance status")
    if severity not in SEVERITIES:
        raise ValueError("unsupported hindrance severity")
    started = _iso_day(payload.get("started_on"), field="started_on")
    reported = _iso_day(payload.get("reported_on") or date.today().isoformat(), field="reported_on")
    cleared = _iso_day(payload.get("cleared_on"), field="cleared_on")
    if started and cleared and cleared < started:
        raise ValueError("cleared_on cannot be before started_on")
    uids = _uids(project_id, payload.get("activity_uids"))
    evidence_ids = _evidence_ids(project_id, payload.get("evidence_ids"))
    now = db.now()
    with db.transaction():
        hid = db.insert("hindrances", {
            "project_id": project_id,
            "ref": _text(payload.get("ref"), 80) or None,
            "title": title, "description": description or None,
            "category": _text(payload.get("category") or "other", 80).lower(),
            "cause": _text(payload.get("cause"), 500) or None,
            "responsibility": _text(payload.get("responsibility"), 120) or None,
            "owner": _text(payload.get("owner"), 120) or None,
            "status": status, "severity": severity,
            "started_on": started, "reported_on": reported, "cleared_on": cleared,
            "schedule_impact_days": _number(payload.get("schedule_impact_days"), field="schedule impact", minimum=0),
            "impact_basis": _text(payload.get("impact_basis"), 1000) or None,
            "activity_uids_json": db.jdumps(uids),
            "evidence_ids_json": db.jdumps(evidence_ids),
            "context_json": db.jdumps(payload.get("context") if isinstance(payload.get("context"), dict) else {}),
            "provenance": "HUMAN_INPUT", "created_by": actor,
            "created_at": now, "updated_at": now,
        })
        if bool(payload.get("create_readiness_blocker")):
            for uid in uids:
                create_constraint(project_id, {
                    "activity_uid": uid,
                    "constraint_type": payload.get("constraint_type") or "other",
                    "description": title + (": " + description if description else ""),
                    "owner": payload.get("owner"), "required_by": payload.get("required_by"),
                    "status": "open", "criticality": "blocker",
                    "evidence_ids": evidence_ids, "source_hindrance_id": hid,
                }, actor=actor)
    audit.record(project_id, actor=actor, actor_type="human", action="hindrance_created",
                 source="website", entity_type="hindrance", entity_id=hid,
                 approval="human", result="registered",
                 detail={"activities": uids, "readiness_blocker": bool(payload.get("create_readiness_blocker"))})
    return shape_hindrance(db.q1("SELECT * FROM hindrances WHERE id=?", [hid]) or {})


def update_hindrance(project_id: str, hindrance_id: str, payload: dict,
                      *, actor: str = "human") -> dict:
    row = db.q1("SELECT * FROM hindrances WHERE id=? AND project_id=?", [hindrance_id, project_id])
    if not row:
        raise KeyError("no such hindrance")
    status = _text(payload.get("status") or row.get("status"), 24).lower()
    if status not in HINDRANCE_STATUSES:
        raise ValueError("unsupported hindrance status")
    cleared = _iso_day(payload.get("cleared_on"), field="cleared_on") if "cleared_on" in payload else row.get("cleared_on")
    if status in {"cleared", "closed"} and not cleared:
        cleared = date.today().isoformat()
    db.update("hindrances", hindrance_id, {"status": status, "cleared_on": cleared, "updated_at": db.now()})
    if status in {"cleared", "closed"}:
        db.ex("UPDATE readiness_constraints SET status='cleared', resolved_by=?, resolved_at=?, updated_at=? WHERE project_id=? AND source_hindrance_id=? AND status IN ('open','in_progress')",
              [actor, db.now(), db.now(), project_id, hindrance_id])
    audit.record(project_id, actor=actor, actor_type="human", action="hindrance_status_changed",
                 source="website", entity_type="hindrance", entity_id=hindrance_id,
                 previous_value=row.get("status"), new_value=status, approval="human", result="ok")
    return shape_hindrance(db.q1("SELECT * FROM hindrances WHERE id=?", [hindrance_id]) or {})


def create_constraint(project_id: str, payload: dict, *, actor: str = "human") -> dict:
    uid = _uids(project_id, [payload.get("activity_uid")], required=True)[0]
    kind = _text(payload.get("constraint_type") or "other", 40).lower().replace(" ", "_")
    if kind not in CONSTRAINT_TYPES:
        kind = "other"
    description = _text(payload.get("description"), 2000)
    if not description:
        raise ValueError("constraint description is required")
    status = _text(payload.get("status") or "open", 24).lower()
    criticality = _text(payload.get("criticality") or "blocker", 24).lower()
    if status not in CONSTRAINT_STATUSES:
        raise ValueError("unsupported readiness status")
    if criticality not in CRITICALITIES:
        raise ValueError("unsupported constraint criticality")
    evidence_ids = _evidence_ids(project_id, payload.get("evidence_ids"))
    now = db.now()
    cid = db.insert("readiness_constraints", {
        "project_id": project_id, "activity_uid": uid,
        "constraint_type": kind, "description": description,
        "owner": _text(payload.get("owner"), 120) or None,
        "required_by": _iso_day(payload.get("required_by"), field="required_by"),
        "status": status, "criticality": criticality,
        "evidence_ids_json": db.jdumps(evidence_ids),
        "source_hindrance_id": _text(payload.get("source_hindrance_id"), 100) or None,
        "provenance": "HUMAN_INPUT", "created_by": actor,
        "created_at": now, "updated_at": now,
    })
    audit.record(project_id, actor=actor, actor_type="human", action="readiness_constraint_created",
                 source="website", entity_type="readiness_constraint", entity_id=cid,
                 approval="human", result="registered", detail={"activity_uid": uid, "type": kind})
    return shape_constraint(db.q1("SELECT * FROM readiness_constraints WHERE id=?", [cid]) or {})


def update_constraint(project_id: str, constraint_id: str, payload: dict,
                      *, actor: str = "human") -> dict:
    row = db.q1("SELECT * FROM readiness_constraints WHERE id=? AND project_id=?",
                [constraint_id, project_id])
    if not row:
        raise KeyError("no such readiness constraint")
    status = _text(payload.get("status") or row.get("status"), 24).lower()
    if status not in CONSTRAINT_STATUSES:
        raise ValueError("unsupported readiness status")
    values: dict[str, Any] = {"status": status, "updated_at": db.now()}
    if status in {"cleared", "waived"}:
        values.update({"resolved_by": actor, "resolved_at": db.now()})
    else:
        values.update({"resolved_by": None, "resolved_at": None})
    db.update("readiness_constraints", constraint_id, values)
    audit.record(project_id, actor=actor, actor_type="human", action="readiness_status_changed",
                 source="website", entity_type="readiness_constraint", entity_id=constraint_id,
                 previous_value=row.get("status"), new_value=status, approval="human", result="ok")
    return shape_constraint(db.q1("SELECT * FROM readiness_constraints WHERE id=?", [constraint_id]) or {})


def _anchor(project_id: str, value: Any = None) -> date:
    if value:
        return date.fromisoformat(_iso_day(value, field="anchor", optional=False) or "")
    snap = db.q1("SELECT data_date,status_date FROM schedule_snapshots WHERE project_id=? AND is_current=1 ORDER BY created_at DESC LIMIT 1", [project_id]) or {}
    raw = snap.get("data_date") or snap.get("status_date")
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else date.today()
    except ValueError:
        return date.today()


def lookahead(project_id: str, *, anchor: Any = None, days: int = 42) -> dict:
    start = _anchor(project_id, anchor)
    horizon = start + timedelta(days=max(7, min(180, int(days or 42))))
    rows = db.q(
        "SELECT * FROM activities WHERE project_id=? AND COALESCE(is_summary,0)=0 "
        "AND COALESCE(actual_finish,'')='' AND COALESCE(percent_complete,0)<100 "
        "AND date(COALESCE(finish,start))>=date(?) AND date(COALESCE(start,finish))<=date(?) "
        "ORDER BY COALESCE(start,finish), critical DESC, uid LIMIT 500",
        [project_id, start.isoformat(), horizon.isoformat()])
    constraints_by_uid: dict[int, list[dict]] = {}
    for item in db.q("SELECT * FROM readiness_constraints WHERE project_id=? ORDER BY required_by,created_at", [project_id]):
        constraints_by_uid.setdefault(int(item["activity_uid"]), []).append(shape_constraint(item))
    counts = {"ready": 0, "blocked": 0, "attention": 0, "not_assessed": 0}
    for activity in rows:
        items = constraints_by_uid.get(int(activity["uid"]), [])
        open_items = [item for item in items if item.get("status") in {"open", "in_progress"}]
        blockers = [item for item in open_items if item.get("criticality") == "blocker"]
        if not items:
            readiness = "not_assessed"
        elif blockers:
            readiness = "blocked"
        elif open_items:
            readiness = "attention"
        else:
            readiness = "ready"
        counts[readiness] += 1
        activity["readiness"] = readiness
        activity["constraints"] = items
        activity["open_constraint_count"] = len(open_items)
        planned = activity.get("start") or activity.get("finish")
        try:
            activity["days_to_start"] = (date.fromisoformat(str(planned)[:10]) - start).days if planned else None
        except ValueError:
            activity["days_to_start"] = None
    return {"anchor": start.isoformat(), "horizon": horizon.isoformat(),
            "days": (horizon - start).days, "activities": rows, "counts": counts,
            "definition": "No constraint record means not assessed, never ready. Ready requires every recorded constraint to be cleared or waived."}


def timeline(project_id: str, *, anchor: Any = None, window: str = "90",
             query: str = "", wbs: str = "", critical: bool = False,
             blockers: bool = False, limit: int = 350) -> dict:
    """Return a source-faithful timeline with evidence/control drill-down counts."""
    pivot = _anchor(project_id, anchor)
    window = str(window or "90").lower()
    days = None if window == "full" else max(14, min(365, int(window) if window.isdigit() else 90))
    start = pivot - timedelta(days=7) if days is not None else None
    finish = pivot + timedelta(days=days) if days is not None else None
    sql = "SELECT * FROM activities WHERE project_id=? AND COALESCE(is_summary,0)=0"
    params: list[Any] = [project_id]
    if query:
        sql += " AND (lower(name) LIKE ? OR lower(COALESCE(display_id,'')) LIKE ? OR lower(COALESCE(wbs,'')) LIKE ?)"
        term = "%" + query.lower() + "%"
        params += [term, term, term]
    if wbs:
        sql += " AND wbs LIKE ?"
        params.append(wbs + "%")
    if critical:
        sql += " AND critical=1"
    if days is not None:
        sql += " AND date(COALESCE(finish,baseline_finish,start,baseline_start))>=date(?) AND date(COALESCE(start,baseline_start,finish,baseline_finish))<=date(?)"
        params += [start.isoformat(), finish.isoformat()]
    if blockers:
        sql += (" AND (EXISTS (SELECT 1 FROM readiness_constraints c "
                "WHERE c.project_id=activities.project_id AND c.activity_uid=activities.uid "
                "AND c.status IN ('open','in_progress')) OR EXISTS (SELECT 1 "
                "FROM hindrances h,json_each(COALESCE(h.activity_uids_json,'[]')) j "
                "WHERE h.project_id=activities.project_id AND h.status IN ('open','monitoring') "
                "AND CAST(j.value AS INTEGER)=activities.uid))")
    sql += " ORDER BY COALESCE(start,baseline_start,finish,baseline_finish),critical DESC,uid LIMIT ?"
    rows = db.q(sql, params + [max(1, min(500, int(limit or 350)))])
    uids = [int(row["uid"]) for row in rows]
    if uids:
        ph = ",".join("?" for _ in uids)
        verified: dict[int, dict[str, str]] = {}
        for item in db.q(
                "SELECT activity_uid,event_state,MIN(event_date) event_date FROM execution_events "
                "WHERE project_id=? AND state='confirmed' AND activity_uid IN (" + ph + ") "
                "AND event_state IN ('start','finish') GROUP BY activity_uid,event_state",
                [project_id] + uids):
            verified.setdefault(int(item["activity_uid"]), {})[str(item["event_state"])] = item["event_date"]
        evidence_counts = {int(item["uid"]): int(item["c"]) for item in db.q(
            "SELECT activity_uid uid,COUNT(*) c FROM evidence_links WHERE project_id=? AND is_candidate=0 AND activity_uid IN (" + ph + ") GROUP BY activity_uid",
            [project_id] + uids)}
        constraint_counts = {int(item["uid"]): int(item["c"]) for item in db.q(
            "SELECT activity_uid uid,COUNT(*) c FROM readiness_constraints WHERE project_id=? AND status IN ('open','in_progress') AND activity_uid IN (" + ph + ") GROUP BY activity_uid",
            [project_id] + uids)}
        bim_counts = {int(item["uid"]): int(item["c"]) for item in db.q(
            "SELECT activity_uid uid,COUNT(*) c FROM bim_identifiers WHERE project_id=? AND status='confirmed' AND activity_uid IN (" + ph + ") GROUP BY activity_uid",
            [project_id] + uids)}
        open_hindrances = list_hindrances(project_id)
        hindrance_counts: dict[int, int] = {}
        for item in open_hindrances:
            if item.get("status") not in {"open", "monitoring"}:
                continue
            for uid in item.get("activity_uids") or []:
                if int(uid) in uids:
                    hindrance_counts[int(uid)] = hindrance_counts.get(int(uid), 0) + 1
        for row in rows:
            uid = int(row["uid"])
            row["verified_actual_start"] = (verified.get(uid) or {}).get("start")
            row["verified_actual_finish"] = (verified.get(uid) or {}).get("finish")
            row["evidence_count"] = evidence_counts.get(uid, 0)
            row["constraint_count"] = constraint_counts.get(uid, 0)
            row["hindrance_count"] = hindrance_counts.get(uid, 0)
            row["bim_identifier_count"] = bim_counts.get(uid, 0)
        links = db.q(
            "SELECT pred_uid,succ_uid,type,lag_days,driving FROM relationships WHERE project_id=? "
            "AND pred_uid IN (" + ph + ") AND succ_uid IN (" + ph + ")",
            [project_id] + uids + uids)
    else:
        links = []

    date_values = []
    for row in rows:
        for key in ("baseline_start", "baseline_finish", "start", "finish", "actual_start",
                    "actual_finish", "verified_actual_start", "verified_actual_finish"):
            try:
                if row.get(key):
                    date_values.append(date.fromisoformat(str(row[key])[:10]))
            except ValueError:
                pass
    range_start = start or (min(date_values) if date_values else pivot)
    range_finish = finish or (max(date_values) if date_values else pivot + timedelta(days=90))
    if range_finish <= range_start:
        range_finish = range_start + timedelta(days=1)
    return {
        "anchor": pivot.isoformat(), "range_start": range_start.isoformat(),
        "range_finish": range_finish.isoformat(), "window": window,
        "activities": rows, "relationships": links, "returned": len(rows),
        "limited": len(rows) >= max(1, min(500, int(limit or 350))),
        "legend": {
            "baseline": "Stored baseline/reference dates",
            "current": "Current schedule dates",
            "actual": "Schedule-recorded actual dates",
            "verified": "Human-confirmed field execution events",
        },
    }


def _parse_resources(value: Any, label_key: str) -> list[dict]:
    if isinstance(value, list):
        rows = value
    else:
        rows = []
        for part in re.split(r"[\n,;]+", _text(value, 4000)):
            part = part.strip()
            if not part:
                continue
            match = re.match(r"(.+?)(?:\s*[:=x]\s*)(\d+(?:\.\d+)?)\s*(.*)$", part)
            rows.append({label_key: (match.group(1) if match else part).strip(),
                         "count": float(match.group(2)) if match else None,
                         "detail": match.group(3).strip() if match and match.group(3) else None})
    clean = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        label = _text(row.get(label_key) or row.get("name") or row.get("trade"), 160)
        if label:
            clean.append({label_key: label, "count": _number(row.get("count"), field=label + " count", minimum=0),
                          "detail": _text(row.get("detail") or row.get("status"), 240) or None})
    return clean


def create_site_context(project_id: str, payload: dict, *, actor: str = "human") -> dict:
    observed_on = _iso_day(payload.get("date") or date.today().isoformat(), field="date", optional=False)
    location = _text(payload.get("location"), 240)
    contractor = _text(payload.get("contractor"), 160)
    weather = _text(payload.get("weather"), 1000)
    manpower = _parse_resources(payload.get("manpower"), "trade")
    equipment = _parse_resources(payload.get("equipment"), "equipment")
    notes = _text(payload.get("notes"), 3000)
    uids = _uids(project_id, payload.get("activity_uids"))
    if not any((weather, manpower, equipment, notes)):
        raise ValueError("provide weather, manpower, equipment or a site note")
    group_id = db.new_id("ctx_")
    created: list[str] = []

    def add(obs_type: str, description: str, values: dict) -> None:
        eid = db.insert("evidence", {
            "project_id": project_id, "source_file": "Manual site context",
            "locator": "context " + group_id, "date": observed_on,
            "author": actor, "contractor": contractor or None,
            "location": location or None, "description": description,
            "observation_type": obs_type, "document_type": "FIELD_CONTEXT",
            "raw_text": description, "raw_values_json": db.jdumps(values),
            "raw_json": db.jdumps({"context_group_id": group_id, "confirmed_by": actor}),
            "extraction_method": "human_confirmed", "extraction_confidence": 1.0,
            "confidence": 1.0, "state": "confirmed", "security_state": "clean",
            "provenance": "HUMAN_INPUT", "created_at": db.now(),
        })
        created.append(eid)
        for uid in uids:
            activity = db.q1("SELECT name FROM activities WHERE project_id=? AND uid=?", [project_id, uid]) or {}
            db.insert("evidence_links", {
                "project_id": project_id, "evidence_id": eid, "activity_uid": uid,
                "activity_name": activity.get("name"), "confidence": 1.0,
                "relation": "context", "human_decision": "confirmed context",
                "decided_by": actor, "decided_at": db.now(), "is_candidate": 0,
                "provenance": "HUMAN_INPUT", "created_at": db.now(),
            })

    with db.transaction():
        if weather:
            add("weather", "Weather: " + weather, {"weather": weather})
        for row in manpower:
            add("manpower", row["trade"] + (" · " + str(row["count"]) if row.get("count") is not None else ""), row)
        for row in equipment:
            add("equipment", row["equipment"] + (" · " + str(row["count"]) if row.get("count") is not None else ""), row)
        if notes:
            add("report_metadata", notes, {"notes": notes})
    audit.record(project_id, actor=actor, actor_type="human", action="site_context_confirmed",
                 source="website", entity_type="site_context", entity_id=group_id,
                 approval="human", result="stored as evidence",
                 detail={"evidence_ids": created, "activity_uids": uids})
    return {"id": group_id, "evidence_ids": created, "activity_uids": uids}


def list_site_context(project_id: str, limit: int = 100,
                      activity_uid: int | None = None) -> list[dict]:
    sql = (
        "SELECT DISTINCT e.* FROM evidence e "
        "WHERE e.project_id=? AND e.observation_type IN "
        "('manpower','equipment','weather','report_metadata')")
    params: list[Any] = [project_id]
    if activity_uid is not None:
        sql += (
            " AND EXISTS (SELECT 1 FROM evidence_links f WHERE f.project_id=e.project_id "
            "AND f.evidence_id=e.id AND f.is_candidate=0 AND f.activity_uid=?)")
        params.append(int(activity_uid))
    sql += " ORDER BY e.date DESC,e.created_at DESC LIMIT ?"
    params.append(max(1, min(500, limit)))
    rows = db.q(sql, params)
    links_by_evidence: dict[str, list[int]] = {}
    evidence_ids = [str(row["id"]) for row in rows]
    if evidence_ids:
        ph = ",".join("?" for _ in evidence_ids)
        for link in db.q(
                "SELECT evidence_id,activity_uid FROM evidence_links WHERE project_id=? "
                "AND evidence_id IN (" + ph + ") AND is_candidate=0 AND activity_uid IS NOT NULL",
                [project_id] + evidence_ids):
            links_by_evidence.setdefault(str(link["evidence_id"]), []).append(int(link["activity_uid"]))
    for row in rows:
        row["values"] = db.jloads(row.pop("raw_values_json", None), {}) or {}
        row["activity_uids"] = links_by_evidence.get(str(row["id"]), [])
    return rows


def list_bim_identifiers(project_id: str) -> list[dict]:
    return [shape_bim(row) for row in db.q(
        "SELECT b.*,a.display_id,a.name activity_name FROM bim_identifiers b "
        "LEFT JOIN activities a ON a.project_id=b.project_id AND a.uid=b.activity_uid "
        "WHERE b.project_id=? ORDER BY b.created_at DESC", [project_id])]


def create_bim_identifier(project_id: str, payload: dict, *, actor: str = "human") -> dict:
    uid = _uids(project_id, [payload.get("activity_uid")], required=True)[0]
    typ = _text(payload.get("identifier_type") or "IFC_GUID", 40).upper()
    if typ not in BIM_TYPES:
        raise ValueError("unsupported BIM identifier type")
    value = _text(payload.get("identifier_value"), 160)
    if not value:
        raise ValueError("BIM identifier value is required")
    model = _text(payload.get("model_name"), 240)
    evidence_ids = _evidence_ids(project_id, [payload.get("evidence_id")]) if payload.get("evidence_id") else []
    existing = db.q1(
        "SELECT * FROM bim_identifiers WHERE project_id=? AND identifier_type=? AND identifier_value=? AND COALESCE(model_name,'')=?",
        [project_id, typ, value, model])
    if existing:
        if existing.get("activity_uid") == uid:
            return shape_bim(existing)
        conflict_id = conflicts.upsert(
            project_id, kind="bim_identifier_conflict",
            detail="BIM identifier " + value + " is already linked to activity " + str(existing.get("activity_uid")) + ".",
            entity_type="bim_identifier", entity_id=existing["id"], activity_uid=uid,
            field="activity_uid", official_value=existing.get("activity_uid"), observed_value=uid)
        raise BIMIdentityConflict(
            "BIM identifier is already linked to another activity; conflict " +
            conflict_id + " was recorded")
    now = db.now()
    bid = db.insert("bim_identifiers", {
        "project_id": project_id, "activity_uid": uid, "identifier_type": typ,
        "identifier_value": value, "model_name": model or None,
        "element_type": _text(payload.get("element_type"), 160) or None,
        "location": _text(payload.get("location"), 240) or None,
        "source_file": _text(payload.get("source_file"), 300) or None,
        "evidence_id": evidence_ids[0] if evidence_ids else None,
        "status": "confirmed", "provenance": "HUMAN_INPUT", "created_by": actor,
        "created_at": now, "updated_at": now,
    })
    # Force only this activity's retrieval document to be rebuilt on the next
    # match so a confirmed model identifier becomes a first-class exact signal.
    db.ex("DELETE FROM retrieval_documents WHERE project_id=? AND activity_uid=?", [project_id, uid])
    audit.record(project_id, actor=actor, actor_type="human", action="bim_identifier_linked",
                 source="website", entity_type="bim_identifier", entity_id=bid,
                 new_value=value, approval="human", result="linked", detail={"activity_uid": uid, "type": typ})
    return shape_bim(db.q1("SELECT * FROM bim_identifiers WHERE id=?", [bid]) or {})


def new_scope_events(project_id: str, limit: int = 50) -> list[dict]:
    """Return conservative candidates for a governed create-activity proposal.

    ``NEW_SCOPE`` in the reality graph also means "no safe current-schedule
    identity".  That larger set remains persisted, but the planner queue only
    exposes execution observations that explicitly describe added scope. This
    prevents reference rows, ``Nil`` values and generic site chat from being
    misrepresented as proposed schedule activities.
    """
    cap = max(1, min(200, limit))
    rows = db.q(
        "SELECT x.id execution_event_id,x.event_date,x.action_type,x.event_state,x.quantity,x.unit,x.confidence,x.source_count, "
        "e.id evidence_id,e.description,e.source_file,e.locator,e.asset_tags_json,e.document_type,e.observation_type,l.basis "
        "FROM execution_event_links l JOIN execution_events x ON x.id=l.execution_event_id "
        "LEFT JOIN execution_event_sources s ON s.execution_event_id=x.id "
        "LEFT JOIN evidence e ON e.id=s.evidence_id "
        "WHERE l.project_id=? AND upper(l.relation)='NEW_SCOPE' "
        "AND e.observation_type='activity_progress' "
        "AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.project_id=l.project_id AND p.source_event_id=x.id AND p.operation='create') "
        "ORDER BY x.event_date DESC,x.created_at DESC,e.created_at DESC LIMIT 2000",
        [project_id])
    selected: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        event_id = str(row.get("execution_event_id") or "")
        description = _text(row.get("description"), 4000)
        source = _text(row.get("source_file"), 300).lower()
        if (not event_id or event_id in seen or len(description) < 8 or
                description.lstrip().startswith(("{", "[")) or
                any(token in source for token in _REFERENCE_SOURCE_HINTS) or
                not _NEW_SCOPE_HINT.search(description)):
            continue
        seen.add(event_id)
        selected.append(row)
        if len(selected) >= cap:
            break
    return selected


def suggest_new_activity(project_id: str, payload: dict, *, actor: str = "human") -> dict:
    name = _text(payload.get("name"), 240)
    if not name:
        raise ValueError("new activity name is required")
    parent_uid = None
    if payload.get("parent_uid") not in (None, ""):
        parent_uid = _uids(project_id, [payload.get("parent_uid")], required=True)[0]
    after_uid = None
    if payload.get("after_uid") not in (None, ""):
        after_uid = _uids(project_id, [payload.get("after_uid")], required=True)[0]
    evidence_ids = _evidence_ids(project_id, payload.get("evidence_ids"))
    source_event_id = _text(payload.get("source_event_id"), 100) or None
    if source_event_id and not db.q1("SELECT id FROM execution_events WHERE id=? AND project_id=?", [source_event_id, project_id]):
        raise ValueError("unknown execution event")
    fields: dict[str, Any] = {"name": name, "active": True}
    duration = _number(payload.get("duration"), field="duration", minimum=0)
    if duration is not None:
        fields["duration"] = duration
    for key in ("start", "finish", "deadline"):
        value = _iso_day(payload.get(key), field=key)
        if value:
            fields[key] = value
    notes = _text(payload.get("notes"), 2000)
    if notes:
        fields["notes"] = notes
    bim_type = _text(payload.get("identifier_type"), 40).upper()
    bim_value = _text(payload.get("identifier_value"), 160)
    model = _text(payload.get("model_name"), 240)
    if bim_value:
        if not bim_type:
            bim_type = "IFC_GUID"
        if bim_type not in BIM_TYPES:
            raise ValueError("unsupported BIM identifier type")
        fields["custom"] = {"bim_identifier_type": bim_type,
                            "bim_identifier": bim_value, "bim_model": model or None}
    proposed_confidence = _number(payload.get("confidence"), field="confidence",
                                  minimum=0, maximum=1)
    proposal_id = proposals.create(
        project_id, operation="create", target_type="activity", target_name=name,
        task_fields=fields, parent_uid=parent_uid, after_uid=after_uid,
        source_event_id=source_event_id, evidence_ids=evidence_ids,
        confidence=0.7 if proposed_confidence is None else proposed_confidence,
        reason=_text(payload.get("reason"), 1000) or
        "Human-reviewed new-scope suggestion; schedule creation requires dry-run, approval and verified write.",
        provenance="HUMAN_INPUT")
    if bim_value:
        existing = db.q1(
            "SELECT id FROM bim_identifiers WHERE project_id=? AND identifier_type=? AND identifier_value=? AND COALESCE(model_name,'')=?",
            [project_id, bim_type, bim_value, model])
        if not existing:
            db.insert("bim_identifiers", {
                "project_id": project_id, "proposal_id": proposal_id,
                "identifier_type": bim_type, "identifier_value": bim_value,
                "model_name": model or None,
                "element_type": _text(payload.get("element_type"), 160) or None,
                "status": "proposed", "provenance": "HUMAN_INPUT", "created_by": actor,
                "created_at": db.now(), "updated_at": db.now(),
            })
    return proposals.shape(db.q1("SELECT * FROM proposals WHERE id=?", [proposal_id]) or {})
