"""Durable, idempotent execution/schedule conflicts."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import audit, db


def _key(kind: str, *, entity_type: str | None, entity_id: str | None,
         activity_uid: int | None, field: str | None) -> str:
    identity = json.dumps([kind, entity_type, entity_id, activity_uid, field],
                          separators=(",", ":"), default=str)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def upsert(project_id: str, *, kind: str, detail: str,
           entity_type: str | None = None, entity_id: str | None = None,
           activity_uid: int | None = None, field: str | None = None,
           official_value: Any = None, observed_value: Any = None,
           evidence_ids: list[str] | None = None,
           source_event_id: str | None = None,
           proposal_group_id: str | None = None,
           provenance: str = "DETERMINISTIC_CALCULATION") -> str:
    conflict_key = _key(kind, entity_type=entity_type, entity_id=entity_id,
                        activity_uid=activity_uid, field=field)
    row = db.q1("SELECT id FROM conflicts WHERE project_id=? AND conflict_key=?",
                [project_id, conflict_key])
    values = {
        "kind": kind, "entity_type": entity_type, "entity_id": entity_id,
        "activity_uid": activity_uid, "field": field,
        "official_value": None if official_value is None else str(official_value),
        "observed_value": None if observed_value is None else str(observed_value),
        "detail": detail, "evidence_ids_json": db.jdumps(evidence_ids or []),
        "source_event_id": source_event_id, "proposal_group_id": proposal_group_id,
        "status": "open", "resolution": None, "resolved_by": None,
        "resolved_at": None, "provenance": provenance, "updated_at": db.now(),
    }
    if row:
        db.update("conflicts", row["id"], values)
        return row["id"]
    return db.insert("conflicts", {"project_id": project_id,
                                    "conflict_key": conflict_key, **values})


def shape(row: dict) -> dict:
    out = dict(row)
    out["evidence_ids"] = db.jloads(out.pop("evidence_ids_json", None), []) or []
    return out


def list_for_project(project_id: str, status: str = "") -> list[dict]:
    sql = "SELECT * FROM conflicts WHERE project_id=?"
    params: list[Any] = [project_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    return [shape(row) for row in db.q(sql + " ORDER BY created_at DESC", params)]


def resolve(conflict_id: str, *, resolution: str, resolved_by: str) -> dict:
    row = db.q1("SELECT * FROM conflicts WHERE id=?", [conflict_id])
    if not row:
        raise KeyError("no such conflict")
    if not str(resolution or "").strip():
        raise ValueError("resolution is required")
    db.update("conflicts", conflict_id, {
        "status": "resolved", "resolution": str(resolution).strip()[:1000],
        "resolved_by": str(resolved_by or "human")[:180],
        "resolved_at": db.now(), "updated_at": db.now(),
    })
    audit.record(row["project_id"], actor=resolved_by, actor_type="human",
                 action="conflict_resolved", entity_type="conflict",
                 entity_id=conflict_id, previous_value="open", new_value="resolved",
                 result=str(resolution)[:300])
    return shape(db.q1("SELECT * FROM conflicts WHERE id=?", [conflict_id]) or {})
