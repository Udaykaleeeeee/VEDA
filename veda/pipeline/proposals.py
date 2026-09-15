"""Proposed schedule changes (spec 46, 47, 48).

    agent proposes -> validators -> Horizun dry-run -> impact -> human approval
    -> verified write

Two rules dominate this module:

  spec 12  the uploaded schedule is a source document. Every write happens on a
           revision copy; the original file is never opened read-write.
  spec 48  a write is not a success because the tool returned. VEDA re-reads the
           value independently and records requested vs resulting.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .. import audit, config, db, events
from ..mcpc import McpError, horizun, schedule_ops, tabular_schedule
from . import validators

FIELD_TO_OP = {
    "percentComplete": "percentComplete",
    "actualStart": "actualStart",
    "actualFinish": "actualFinish",
    "start": "start",
    "finish": "finish",
    "duration": "duration",
    "remainingDuration": "remainingDuration",
    "deadline": "deadline",
    "notes": "notes",
    "name": "name",
    "constraintType": "constraintType",
    "constraintDate": "constraintDate",
}

ACTIVITY_FIELD = {
    "percentComplete": "percent_complete",
    "actualStart": "actual_start",
    "actualFinish": "actual_finish",
    "start": "start",
    "finish": "finish",
    "duration": "duration_days",
    "remainingDuration": "remaining_days",
    "deadline": "deadline",
    "notes": "notes",
    "name": "name",
    "constraintType": "constraint_type",
    "constraintDate": "constraint_date",
}

TASK_QUERY_FIELD = {
    "percentComplete": "percentComplete",
    "actualStart": "actualStart",
    "actualFinish": "actualFinish",
    "start": "start",
    "finish": "finish",
    "duration": "duration",
    "remainingDuration": "remainingDuration",
    "deadline": "deadline",
    "notes": "notes",
    "name": "name",
    "constraintType": "constraintType",
    "constraintDate": "constraintDate",
}

CREATE_TASK_FIELDS = {
    "name", "duration", "start", "finish", "percentComplete",
    "actualStart", "actualFinish", "milestone", "active", "notes",
    "deadline", "constraintType", "constraintDate", "cost", "custom",
}


def _proposal_payload(p: dict) -> dict:
    return db.jloads(p.get("payload_json"), {}) or {}


def _tabular_write_block(src: str) -> dict | None:
    """Return a user-facing guard for adapted tabular schedules.

    CSV/XLSX schedules are accepted for analysis through the semantic adapter,
    but VEDA must never pretend the generated MSPDI analysis copy is the user's
    writable source. Governed edits require a native schedule file.
    """
    try:
        candidate = tabular_schedule.inspect_tabular_schedule(src, relative_path=Path(src).name)
    except Exception:
        return None
    if candidate and candidate.get("is_schedule"):
        return {
            "ok": False,
            "error": (
                "This schedule was imported from a tabular CSV/XLSX source and is "
                "read-only for governed schedule write-back. Export/use a native "
                "Primavera XER/P6 XML or Microsoft Project XML schedule before "
                "dry-run or execution."
            ),
            "code": "TABULAR_SCHEDULE_READ_ONLY",
        }
    return None


def current_schedule_path(project_id: str) -> str | None:
    """The newest schedule VEDA holds - the latest revision, else the upload."""
    snap = db.q1("SELECT source_path FROM schedule_snapshots WHERE project_id=? "
                 "AND is_current=1 ORDER BY created_at DESC LIMIT 1", [project_id])
    if snap and snap.get("source_path") and os.path.exists(snap["source_path"]):
        return snap["source_path"]
    p = db.q1("SELECT schedule_file_id FROM projects WHERE id=?", [project_id])
    if p and p.get("schedule_file_id"):
        f = db.q1("SELECT stored_path FROM files WHERE id=?", [p["schedule_file_id"]])
        if f and os.path.exists(f["stored_path"]):
            return f["stored_path"]
    f = db.q1("SELECT stored_path FROM files WHERE project_id=? AND kind='schedule' "
              "ORDER BY created_at DESC LIMIT 1", [project_id])
    return f["stored_path"] if f and os.path.exists(f["stored_path"]) else None


def create(project_id: str, *, target_uid: int | None = None,
           field: str | None = None, proposed_value: Any = None,
           reason: str = "", target_type: str = "activity",
           target_name: str | None = None, evidence_ids: list | None = None,
           confidence: float = 0.5, job_id: str | None = None,
           provenance: str = "AI_INFERENCE", operation: str = "update",
           parent_uid: int | None = None, after_uid: int | None = None,
           task_fields: dict | None = None,
           proposal_group_id: str | None = None,
           source_event_id: str | None = None) -> str:
    operation = str(operation or "update").strip().lower()
    if operation not in {"update", "create", "delete"}:
        operation = "update"

    if source_event_id and operation == "update":
        existing = db.q1(
            "SELECT * FROM proposals WHERE project_id=? AND source_event_id=? "
            "AND target_uid=? AND field=? ORDER BY created_at DESC LIMIT 1",
            [project_id, source_event_id, target_uid, field])
        if existing:
            merged_evidence = list(dict.fromkeys(
                (db.jloads(existing.get("evidence_ids_json"), []) or []) +
                list(evidence_ids or [])))
            db.update("proposals", existing["id"], {
                "evidence_ids_json": db.jdumps(merged_evidence),
                "confidence": max(float(existing.get("confidence") or 0),
                                  float(confidence or 0)),
                "updated_at": db.now(),
            })
            validate(existing["id"])
            if existing.get("proposal_group_id"):
                _refresh_group(existing["proposal_group_id"], project_id=project_id,
                               source_event_id=source_event_id)
            return existing["id"]

    act = db.q1("SELECT * FROM activities WHERE project_id=? AND uid=?",
                [project_id, target_uid]) if target_uid is not None else None
    current = None
    if operation == "update" and act:
        col = ACTIVITY_FIELD.get(str(field))
        if col:
            current = act.get(col)
    elif operation == "delete" and act:
        current = act.get("name")

    tf = {k: v for k, v in dict(task_fields or {}).items()
          if k in CREATE_TASK_FIELDS and v is not None}
    if operation == "create":
        if target_name and not tf.get("name"):
            tf["name"] = target_name
        elif proposed_value and not tf.get("name"):
            # Forgiving fallback for an agent that puts the new task name in
            # proposed_value rather than task_fields.name.
            tf["name"] = str(proposed_value)
        target_name = str(tf.get("name") or target_name or "New task")

    payload = {"parent_uid": parent_uid, "after_uid": after_uid,
               "task_fields": tf}
    if operation == "update":
        requested = "" if proposed_value is None else str(proposed_value)
    elif operation == "delete":
        requested = "<deleted>"
        field = field or "task"
    else:
        requested = json.dumps(tf, ensure_ascii=False, sort_keys=True)
        field = field or "task"

    pid = db.insert("proposals", {
        "project_id": project_id, "job_id": job_id,
        "target_type": target_type, "operation": operation,
        "proposal_group_id": proposal_group_id,
        "source_event_id": source_event_id,
        "target_uid": target_uid,
        "target_name": target_name or (act or {}).get("name"),
        "field": field, "payload_json": db.jdumps(payload),
        "current_value": None if current is None else str(current),
        "proposed_value": requested, "requested_value": requested,
        "reason": reason,
        "evidence_ids_json": db.jdumps(evidence_ids or []),
        "confidence": confidence, "provenance": provenance,
        "updated_at": db.now(),
    })
    audit.record(project_id, actor="agent", actor_type="agent",
                 action="proposal_created", job_id=job_id,
                 entity_type="proposal", entity_id=pid,
                 previous_value=current, new_value=requested,
                 result=reason[:200] if reason else None,
                 detail={"operation": operation, "field": field,
                         "target_uid": target_uid, "payload": payload})
    validate(pid)
    if proposal_group_id:
        _refresh_group(proposal_group_id, project_id=project_id,
                       source_event_id=source_event_id)
    return pid


def validate(proposal_id: str) -> dict:
    p = db.q1("SELECT * FROM proposals WHERE id=?", [proposal_id])
    if not p:
        raise KeyError("no such proposal")
    act = db.q1("SELECT * FROM activities WHERE project_id=? AND uid=?",
                [p["project_id"], p["target_uid"]])
    res = validators.validate_proposal(
        p, act, project_id=p["project_id"],
        approved=(p.get("approval_state") == "approved"),
        capabilities=horizun.capabilities())
    db.update("proposals", proposal_id, {
        "validation_state": "passed" if res["result"] != validators.FAIL else "failed",
        "validation_json": db.jdumps(res),
        "updated_at": db.now(),
    })
    return res


def _op_for(p: dict) -> dict:
    operation = str(p.get("operation") or "update").lower()
    if operation == "delete":
        return {"op": "delete", "uid": p.get("target_uid")}
    if operation == "create":
        payload = _proposal_payload(p)
        op: dict[str, Any] = {"op": "create"}
        if payload.get("parent_uid") is not None:
            op["parentUid"] = payload["parent_uid"]
        if payload.get("after_uid") is not None:
            op["afterUid"] = payload["after_uid"]
        for key, value in (payload.get("task_fields") or {}).items():
            if key in CREATE_TASK_FIELDS and value is not None:
                op[key] = value
        return op

    field = p.get("field")
    val: Any = p.get("proposed_value")
    if field == "percentComplete":
        try:
            val = float(str(val).replace("%", "").strip())
        except ValueError:
            pass
    return {"op": "update", "uid": p.get("target_uid"),
            FIELD_TO_OP[str(field)]: val}


def _group_rows(group_id: str) -> list[dict]:
    return db.q("SELECT * FROM proposals WHERE proposal_group_id=? "
                "ORDER BY created_at,id", [group_id])


def _aggregate_state(rows: list[dict], field: str, *, pending: str) -> str:
    states = [str(row.get(field) or pending) for row in rows]
    if not states:
        return pending
    if all(value == states[0] for value in states):
        return states[0]
    if "failed" in states or "rejected" in states:
        return "failed"
    return "partial"


def _refresh_group(group_id: str, *, project_id: str | None = None,
                   source_event_id: str | None = None) -> dict:
    rows = _group_rows(group_id)
    if not rows:
        raise KeyError("no such proposal group")
    project_id = project_id or rows[0]["project_id"]
    existing = db.q1("SELECT id FROM proposal_groups WHERE id=?", [group_id])
    values = {
        "project_id": project_id, "source_event_id": source_event_id or rows[0].get("source_event_id"),
        "purpose": "coupled schedule actuals",
        "member_count": len(rows),
        "validation_state": _aggregate_state(rows, "validation_state", pending="pending"),
        "dryrun_state": _aggregate_state(rows, "dryrun_state", pending="not_run"),
        "approval_state": _aggregate_state(rows, "approval_state", pending="pending"),
        "execution_state": _aggregate_state(rows, "execution_state", pending="not_executed"),
        "verification_state": _aggregate_state(rows, "verification_state", pending="not_verified"),
        "updated_at": db.now(),
    }
    if existing:
        db.update("proposal_groups", group_id, values)
    else:
        db.insert("proposal_groups", {"id": group_id, **values})
    return db.q1("SELECT * FROM proposal_groups WHERE id=?", [group_id]) or {}


def dry_run(proposal_id: str, job_id: str | None = None) -> dict:
    """Simulate through Horizun and store the real impact (spec 47).

    The simulation runs against a throwaway copy of the schedule, so neither the
    original upload nor any stored revision is touched.
    """
    p = db.q1("SELECT * FROM proposals WHERE id=?", [proposal_id])
    if not p:
        raise KeyError("no such proposal")
    project_id = p["project_id"]

    if p.get("validation_state") != "passed":
        res = validate(proposal_id)
        if res["result"] == validators.FAIL:
            db.update("proposals", proposal_id, {
                "dryrun_state": "failed",
                "dryrun_json": db.jdumps({"error": "validation failed",
                                          "validation": res}),
                "updated_at": db.now()})
            return {"ok": False, "error": "validation failed", "validation": res}

    operation = str(p.get("operation") or "update").lower()
    if operation == "update" and p.get("field") not in FIELD_TO_OP:
        return {"ok": False, "error": "field is not writable: " + str(p.get("field"))}

    if not horizun.capabilities().get("dry_run_simulation", True):
        db.update("proposals", proposal_id, {"dryrun_state": "failed",
                                             "dryrun_json": db.jdumps(
                                                 {"error": "dry run unsupported"})})
        return {"ok": False, "error": "Horizun reports dry-run is unavailable"}

    src = current_schedule_path(project_id)
    if not src:
        return {"ok": False, "error": "no schedule file is available"}
    tabular_guard = _tabular_write_block(src)
    if tabular_guard:
        return tabular_guard

    scratch = Path(config.project_dir(project_id)) / "revisions" / "_dryrun"
    scratch.mkdir(parents=True, exist_ok=True)
    tmp = scratch / ("dry_" + proposal_id + Path(src).suffix)
    shutil.copy2(src, tmp)

    try:
        handle = horizun.call("project_open",
                              {"path": str(tmp), "mode": "readwrite"},
                              project_id=project_id, job_id=job_id, timeout=300)["handle"]
        before = (_read_field(handle, p["target_uid"], p["field"], project_id, job_id)
                  if operation == "update" else p.get("current_value"))
        info_before = horizun.call("project_info", {"handle": handle},
                                   project_id=project_id, job_id=job_id, log=False)
        res = horizun.call("tasks_write",
                           {"handle": handle, "ops": [_op_for(p)], "dryRun": True},
                           project_id=project_id, job_id=job_id, timeout=300)
        horizun.try_call("project_save",
                         {"handle": handle, "op": "close", "discardChanges": True},
                         log=False)
    except McpError as exc:
        db.update("proposals", proposal_id, {
            "dryrun_state": "failed",
            "dryrun_json": db.jdumps({"error": str(exc)}),
            "updated_at": db.now()})
        audit.record(project_id, actor="system", actor_type="mcp",
                     action="proposal_dry_run", tool="Horizun/tasks_write",
                     job_id=job_id, entity_type="proposal", entity_id=proposal_id,
                     result="failed: " + str(exc)[:300])
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    impact = (res or {}).get("impact") or {}
    rejected = (res or {}).get("rejected") or []
    finish_before = (info_before or {}).get("finishDate")
    payload = {
        "operation": operation,
        "applied": (res or {}).get("applied"),
        "rejected": rejected,
        "impact": impact,
        "current_value": before,
        "finish_before": finish_before,
        "finish_after": impact.get("projectFinishAfter"),
        "notes": (res or {}).get("notes") or [],
    }
    ok = not rejected and (res or {}).get("applied", 0) > 0
    db.update("proposals", proposal_id, {
        "dryrun_state": "ok" if ok else "failed",
        "dryrun_json": db.jdumps(payload),
        "impact_tasks_moved": impact.get("tasksMoved"),
        "impact_finish_before": _day(finish_before),
        "impact_finish_after": _day(impact.get("projectFinishAfter")),
        "impact_critical_change": 1 if impact.get("criticalPathChanged") else 0,
        "impact_negative_float": impact.get("newNegativeFloat"),
        "current_value": None if before is None else str(before),
        "updated_at": db.now(),
    })
    audit.record(project_id, actor="system", actor_type="mcp",
                 action="proposal_dry_run", tool="Horizun/tasks_write",
                 job_id=job_id, entity_type="proposal", entity_id=proposal_id,
                 new_value=p["proposed_value"],
                 result="ok" if ok else "rejected",
                 detail={"impact": impact, "rejected": rejected})
    events.notify_ui(project_id, "proposals_changed", {"proposal_id": proposal_id})
    return {"ok": ok, **payload}


def _day(v: Any) -> str | None:
    if not v:
        return None
    return str(v).split("T")[0]


def _read_field(handle: str, uid: int, field: str, project_id: str,
                job_id: str | None) -> Any:
    res = horizun.call("tasks_query", {"handle": handle, "uids": [uid], "limit": 1},
                       project_id=project_id, job_id=job_id, log=False, timeout=120)
    rows = schedule_ops._rows(res)
    if not rows:
        return None
    return rows[0].get(TASK_QUERY_FIELD.get(field, field))


def approve(proposal_id: str, approved_by: str = "human",
            approve_it: bool = True, note: str | None = None) -> dict:
    p = db.q1("SELECT * FROM proposals WHERE id=?", [proposal_id])
    if not p:
        raise KeyError("no such proposal")
    if p.get("proposal_group_id"):
        return approve_group(p["proposal_group_id"], approved_by=approved_by,
                             approve_it=approve_it, note=note)
    state = "approved" if approve_it else "rejected"
    db.update("proposals", proposal_id, {
        "approval_state": state, "approved_by": approved_by,
        "approved_at": db.now(), "updated_at": db.now(),
    })
    audit.record(p["project_id"], actor=approved_by, actor_type="human",
                 action="proposal_" + state, source="website",
                 job_id=p.get("job_id"), entity_type="proposal",
                 entity_id=proposal_id, previous_value=p.get("current_value"),
                 new_value=p.get("proposed_value"), approval=approved_by,
                 result=note or state)
    validate(proposal_id)
    events.notify_ui(p["project_id"], "proposals_changed",
                     {"proposal_id": proposal_id})
    return db.q1("SELECT * FROM proposals WHERE id=?", [proposal_id]) or {}


def dry_run_group(group_id: str, job_id: str | None = None) -> dict:
    """Dry-run every member before the group can enter the approval gate."""
    rows = _group_rows(group_id)
    if not rows:
        raise KeyError("no such proposal group")
    results = [dry_run(row["id"], job_id=job_id) for row in rows]
    group = _refresh_group(group_id)
    ok = all(bool(result.get("ok")) for result in results)
    if not ok:
        db.update("proposal_groups", group_id, {"dryrun_state": "failed",
                                                "updated_at": db.now()})
    return {"ok": ok, "group_id": group_id, "members": results,
            "group": db.q1("SELECT * FROM proposal_groups WHERE id=?", [group_id]) or group}


def approve_group(group_id: str, *, approved_by: str = "human",
                  approve_it: bool = True, note: str | None = None) -> dict:
    """Approve/reject all members in one SQLite transaction.

    Approval fails closed when even one member has not passed validation and a
    real dry-run. No proposal is changed in that case.
    """
    rows = _group_rows(group_id)
    if not rows:
        raise KeyError("no such proposal group")
    if approve_it:
        blockers = [row["id"] for row in rows if row.get("validation_state") != "passed"
                    or row.get("dryrun_state") != "ok"]
        if blockers:
            raise ValueError("every proposal in the group must pass validation and dry-run before approval")
    state = "approved" if approve_it else "rejected"
    stamp = db.now()
    with db.transaction():
        for row in rows:
            db.update("proposals", row["id"], {
                "approval_state": state, "approved_by": approved_by,
                "approved_at": stamp, "updated_at": stamp})
        _refresh_group(group_id)
        db.update("proposal_groups", group_id, {
            "approval_state": state, "approved_by": approved_by,
            "approved_at": stamp, "decision_note": note,
            "updated_at": stamp})
        audit.record(rows[0]["project_id"], actor=approved_by, actor_type="human",
                     action="proposal_group_" + state, source="website",
                     entity_type="proposal_group", entity_id=group_id,
                     approval=approved_by, result=note or state,
                     detail={"proposal_ids": [row["id"] for row in rows],
                             "atomic": True})
    events.notify_ui(rows[0]["project_id"], "proposals_changed",
                     {"proposal_group_id": group_id})
    return {"ok": True, "group_id": group_id, "approval_state": state,
            "proposal_ids": [row["id"] for row in rows]}


def execute_group(group_id: str, job_id: str | None = None,
                  actor: str = "human") -> dict:
    """Apply a coupled update bundle to one revision, then verify every field.

    The new revision is saved only when all independent read-backs match. A
    rejection or mismatch closes the scratch handle without publishing a
    partial schedule revision.
    """
    rows = _group_rows(group_id)
    if not rows:
        raise KeyError("no such proposal group")
    project_id = rows[0]["project_id"]
    if any(row["project_id"] != project_id for row in rows):
        return {"ok": False, "error": "proposal group crosses project boundaries"}
    if any((row.get("operation") or "update") != "update" for row in rows):
        return {"ok": False, "error": "atomic group execution currently permits field updates only"}
    if any(row.get("approval_state") != "approved" for row in rows):
        return {"ok": False, "error": "the complete proposal group is not approved"}
    if any(row.get("validation_state") != "passed" or row.get("dryrun_state") != "ok"
           for row in rows):
        return {"ok": False, "error": "every group member must pass validation and dry-run"}
    if any(row.get("execution_state") == "executed" for row in rows):
        return {"ok": False, "error": "one or more group members were already executed"}

    src = current_schedule_path(project_id)
    if not src:
        return {"ok": False, "error": "no schedule file is available"}
    guard = _tabular_write_block(src)
    if guard:
        return guard
    from . import ingest
    dest = ingest.copy_for_edit(project_id, src, suffix="rev")
    handle = None
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    try:
        handle = horizun.call("project_open", {"path": dest, "mode": "readwrite"},
                              project_id=project_id, job_id=job_id,
                              timeout=300)["handle"]
        for row in rows:
            before[row["id"]] = _read_field(handle, row["target_uid"], row["field"],
                                             project_id, job_id)
        result = horizun.call("tasks_write", {
            "handle": handle, "ops": [_op_for(row) for row in rows], "dryRun": False},
            project_id=project_id, job_id=job_id, timeout=300)
        rejected = (result or {}).get("rejected") or []
        for row in rows:
            after[row["id"]] = _read_field(handle, row["target_uid"], row["field"],
                                            project_id, job_id)
        mismatches = [row["id"] for row in rows
                      if not _matches(row.get("proposed_value"), after[row["id"]], row["field"])]
        if rejected or mismatches:
            horizun.try_call("project_save", {"handle": handle, "op": "close",
                                               "discardChanges": True}, log=False)
            Path(dest).unlink(missing_ok=True)
            with db.transaction():
                db.update("proposal_groups", group_id, {
                    "execution_state": "failed", "verification_state": "failed",
                    "updated_at": db.now()})
            return {"ok": False, "error": "group read-back verification failed",
                    "rejected": rejected, "mismatches": mismatches}
        save = horizun.call("project_save", {
            "handle": handle, "op": "save_as", "path": dest,
            "format": "mspdi", "keepOpen": False},
            project_id=project_id, job_id=job_id, timeout=300)
    except Exception as exc:  # noqa: BLE001 - external write boundary must fail closed
        if handle:
            horizun.try_call("project_save", {"handle": handle, "op": "close",
                                               "discardChanges": True}, log=False)
        Path(dest).unlink(missing_ok=True)
        db.update("proposal_groups", group_id, {
            "execution_state": "failed", "verification_state": "failed",
            "updated_at": db.now()})
        return {"ok": False, "error": str(exc)}

    stamp = db.now()
    with db.transaction():
        for row in rows:
            db.update("proposals", row["id"], {
                "execution_state": "executed", "executed_at": stamp,
                "verification_state": "verified",
                "requested_value": str(row.get("proposed_value")),
                "resulting_value": None if after[row["id"]] is None else str(after[row["id"]]),
                "verified_fields_json": db.jdumps([row["field"]]),
                "rejected_fields_json": db.jdumps([]), "output_path": dest,
                "updated_at": stamp})
        db.update("proposal_groups", group_id, {
            "execution_state": "executed", "verification_state": "verified",
            "updated_at": stamp})
        db.insert("artifacts", {
            "project_id": project_id, "job_id": job_id, "kind": "schedule_revision",
            "title": Path(dest).name, "path": dest, "format": "mspdi",
            "size_bytes": os.path.getsize(dest) if os.path.exists(dest) else None,
            "description": "Atomic revision for proposal group " + group_id +
                           "; all fields passed independent read-back verification.",
            "provenance": "DERIVED"})
        audit.record(project_id, actor=actor, actor_type="agent",
                     action="proposal_group_execute", tool="Horizun/tasks_write",
                     source="proposal_group", entity_type="proposal_group",
                     entity_id=group_id, approval=rows[0].get("approved_by"),
                     verification="verified", result="written to " + Path(dest).name,
                     detail={"proposal_ids": [row["id"] for row in rows],
                             "before": before, "after": after, "save": save,
                             "atomic": True, "output_path": dest})
    schedule_ops.forget_handles()
    events.emit(events.SCHEDULE_CHANGED, project_id, {
        "proposal_group_id": group_id, "proposal_ids": [row["id"] for row in rows],
        "path": dest, "verification": "verified"}, source="proposal_group")
    return {"ok": True, "group_id": group_id, "verification": "verified",
            "output_path": dest, "proposal_ids": [row["id"] for row in rows]}


def _task_rows(handle: str, project_id: str, job_id: str | None) -> list[dict]:
    # Use the same cursor walker as schedule harvesting. A large project can
    # exceed a single tasks_query page, and create verification must not miss a
    # newly inserted task just because it landed beyond page one.
    return schedule_ops.page_all("tasks_query", {"handle": handle, "shape": "flat"},
                                 project_id=project_id, job_id=job_id,
                                 limit=500, cap=50000)


def _task_exists(handle: str, uid: int, project_id: str,
                 job_id: str | None) -> bool:
    res = horizun.call("tasks_query", {"handle": handle, "uids": [uid], "limit": 1},
                       project_id=project_id, job_id=job_id, log=False, timeout=120)
    return bool(schedule_ops._rows(res))


def execute(proposal_id: str, job_id: str | None = None,
            actor: str = "human") -> dict:
    """Apply an approved proposal to a revision copy, then verify it (spec 48)."""
    p = db.q1("SELECT * FROM proposals WHERE id=?", [proposal_id])
    if not p:
        raise KeyError("no such proposal")
    project_id = p["project_id"]

    if p.get("approval_state") != "approved":
        return {"ok": False, "error": "proposal is not approved"}
    if p.get("validation_state") != "passed":
        return {"ok": False, "error": "proposal has not passed validation"}
    if p.get("dryrun_state") != "ok":
        return {"ok": False, "error": "proposal has no successful dry-run"}
    if p.get("execution_state") == "executed":
        return {"ok": False, "error": "proposal has already been executed"}

    src = current_schedule_path(project_id)
    if not src:
        return {"ok": False, "error": "no schedule file is available"}
    tabular_guard = _tabular_write_block(src)
    if tabular_guard:
        return tabular_guard

    from . import ingest
    dest = ingest.copy_for_edit(project_id, src, suffix="rev")

    operation = str(p.get("operation") or "update").lower()
    requested = p.get("proposed_value")
    created_uid: int | None = None
    try:
        handle = horizun.call("project_open", {"path": dest, "mode": "readwrite"},
                              project_id=project_id, job_id=job_id,
                              timeout=300)["handle"]
        before_rows = _task_rows(handle, project_id, job_id) if operation == "create" else []
        if operation == "update":
            before = _read_field(handle, p["target_uid"], p["field"],
                                 project_id, job_id)
        elif operation == "delete":
            before = p.get("current_value") or p.get("target_name")
        else:
            before = None

        res = horizun.call("tasks_write",
                           {"handle": handle, "ops": [_op_for(p)], "dryRun": False},
                           project_id=project_id, job_id=job_id, timeout=300)

        # Independent re-read. Horizun verifies too; VEDA does not take its word.
        if operation == "update":
            after = _read_field(handle, p["target_uid"], p["field"],
                                project_id, job_id)
            verified = _matches(requested, after, p["field"])
            verified_fields = [p["field"]] if verified else []
        elif operation == "delete":
            exists = _task_exists(handle, int(p["target_uid"]), project_id, job_id)
            after = "<still present>" if exists else "<deleted>"
            verified = not exists
            verified_fields = ["delete"] if verified else []
        else:
            before_uids = {int(r["uid"]) for r in before_rows if r.get("uid") is not None}
            after_rows = _task_rows(handle, project_id, job_id)
            fresh = [r for r in after_rows if r.get("uid") is not None and
                     int(r["uid"]) not in before_uids]
            wanted_name = str((_proposal_payload(p).get("task_fields") or {}).get(
                "name") or p.get("target_name") or "New task")
            named = [r for r in fresh if str(r.get("name") or "").strip() == wanted_name.strip()]
            made = named[0] if len(named) == 1 else (fresh[0] if len(fresh) == 1 else None)
            created_uid = int(made["uid"]) if made and made.get("uid") is not None else None
            after = ("uid " + str(created_uid) + ": " + str(made.get("name"))) if made else None
            verified = bool(made and str(made.get("name") or "").strip() == wanted_name.strip())
            verified_fields = ["create"] if verified else []

        save = horizun.call("project_save",
                            {"handle": handle, "op": "save_as", "path": dest,
                             "format": "mspdi", "keepOpen": False},
                            project_id=project_id, job_id=job_id, timeout=300)
    except McpError as exc:
        db.update("proposals", proposal_id, {
            "execution_state": "failed", "verification_state": "failed",
            "resulting_value": None, "updated_at": db.now(),
            "rejected_fields_json": db.jdumps([{"field": p.get("field") or operation,
                                                "reason": str(exc)}])})
        audit.record(project_id, actor=actor, actor_type="agent",
                     action="proposal_execute", tool="Horizun/tasks_write",
                     job_id=job_id, entity_type="proposal", entity_id=proposal_id,
                     previous_value=p.get("current_value"), new_value=requested,
                     approval=p.get("approved_by"), verification="failed",
                     result="error: " + str(exc)[:300])
        return {"ok": False, "error": str(exc)}

    schedule_ops.forget_handles()
    rejected = (res or {}).get("rejected") or []
    if rejected:
        verification = "failed"
    elif verified:
        verification = "verified"
    else:
        verification = "partial"

    db.update("proposals", proposal_id, {
        "execution_state": "executed" if not rejected else "failed",
        "executed_at": db.now(),
        "verification_state": verification,
        "requested_value": str(requested),
        "resulting_value": None if after is None else str(after),
        "verified_fields_json": db.jdumps(verified_fields),
        "rejected_fields_json": db.jdumps(rejected),
        "output_path": dest,
        "updated_at": db.now(),
    })

    # A BIM/model identifier attached to a governed new-activity suggestion is
    # deliberately held against the proposal until the created task has been
    # found by the independent read-back above. Only then does it become an
    # activity identity signal for later field-to-schedule matching.
    if operation == "create" and created_uid is not None and verified and not rejected:
        db.ex("UPDATE bim_identifiers SET activity_uid=?, status='confirmed', updated_at=? "
              "WHERE project_id=? AND proposal_id=? AND activity_uid IS NULL",
              [created_uid, db.now(), project_id, proposal_id])
        db.ex("DELETE FROM retrieval_documents WHERE project_id=? AND activity_uid=?",
              [project_id, created_uid])

    db.insert("artifacts", {
        "project_id": project_id, "job_id": job_id, "kind": "schedule_revision",
        "title": Path(dest).name, "path": dest, "format": "mspdi",
        "size_bytes": os.path.getsize(dest) if os.path.exists(dest) else None,
        "description": ("Revision produced by applying proposal " + proposal_id +
                        " (" + operation + ": " + str(p.get("field") or "task") +
                        " on uid " + str(p.get("target_uid")) + "). The original upload is "
                        "unchanged."),
        "provenance": "DERIVED",
    })

    audit.record(project_id, actor=actor, actor_type="agent",
                 action="proposal_execute", tool="Horizun/tasks_write",
                 source="proposal", job_id=job_id, entity_type="proposal",
                 entity_id=proposal_id, previous_value=before,
                 new_value=requested, approval=p.get("approved_by"),
                 verification=verification,
                 result=("written to " + Path(dest).name),
                 detail={"operation": operation, "requested": requested,
                         "resulting": after, "created_uid": created_uid,
                         "rejected": rejected, "save": save,
                         "output_path": dest})

    events.emit(events.SCHEDULE_CHANGED, project_id, {
        "proposal_id": proposal_id, "path": dest,
        "verification": verification, "operation": operation,
        "field": p.get("field"), "target_uid": p.get("target_uid"),
        "created_uid": created_uid,
    }, source="proposal")

    return {"ok": not rejected, "verification": verification,
            "requested_value": requested, "resulting_value": after,
            "rejected": rejected, "output_path": dest}


def _matches(requested: Any, actual: Any, field: str) -> bool:
    if actual is None:
        return False
    if field == "percentComplete":
        try:
            return abs(float(str(requested).replace("%", "")) -
                       float(str(actual).replace("%", ""))) < 0.51
        except (TypeError, ValueError):
            return False
    if field in ("actualStart", "actualFinish", "start", "finish", "deadline",
                 "constraintDate"):
        return str(actual).split("T")[0] == str(requested).split("T")[0]
    if field in ("duration", "remainingDuration"):
        a = str(actual).strip().lower().rstrip("d")
        b = str(requested).strip().lower().rstrip("d")
        try:
            return abs(float(a) - float(b)) < 0.01
        except ValueError:
            return a == b
    return str(actual).strip() == str(requested).strip()


def shape(p: dict) -> dict:
    p = dict(p)
    p["evidence_ids"] = db.jloads(p.pop("evidence_ids_json", None), []) or []
    p["payload"] = db.jloads(p.pop("payload_json", None), {}) or {}
    p["operation"] = p.get("operation") or "update"
    p["validation"] = db.jloads(p.pop("validation_json", None), {}) or {}
    p["dryrun"] = db.jloads(p.pop("dryrun_json", None), {}) or {}
    p["verified_fields"] = db.jloads(p.pop("verified_fields_json", None), []) or []
    p["rejected_fields"] = db.jloads(p.pop("rejected_fields_json", None), []) or []
    return p
