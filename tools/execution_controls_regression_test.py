"""Focused offline regression for timeline, controls, context and BIM governance."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="veda_controls_")
os.environ["VEDA_DATA_DIR"] = TMP.name
sys.path.insert(0, str(ROOT))

from veda import db  # noqa: E402
from veda.pipeline import controls  # noqa: E402
from veda.retrieval import engine  # noqa: E402
from veda.resolution import reality_graph  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main() -> None:
    try:
        db.init_db()
        pid = db.insert("projects", {"name": "Execution Controls", "status": "active",
                                     "updated_at": db.now()})
        snap = db.insert("schedule_snapshots", {
            "project_id": pid, "revision": 1, "project_name": "Controls schedule",
            "data_date": "2026-09-14", "status_date": "2026-09-14",
            "planned_start": "2026-09-01", "planned_finish": "2026-12-31",
            "is_current": 1, "task_count": 2,
        })
        for uid, display, name, start, finish in (
                (101, "PIPE-101", "Install pipeline section A", "2026-09-20", "2026-10-05"),
                (102, "PIPE-102", "Install pipeline section B", "2026-10-06", "2026-10-20")):
            db.insert("activities", {
                "project_id": pid, "snapshot_id": snap, "uid": uid,
                "display_id": display, "name": name, "wbs": "PIPE.CONSTRUCTION",
                "status": "not_started", "start": start, "finish": finish,
                "baseline_start": start, "baseline_finish": finish,
                "percent_complete": 0, "critical": 1 if uid == 101 else 0,
            })
        db.insert("relationships", {"project_id": pid, "snapshot_id": snap,
                                     "pred_uid": 101, "succ_uid": 102,
                                     "pred_name": "Install pipeline section A",
                                     "succ_name": "Install pipeline section B",
                                     "type": "FS", "driving": 1})

        context = controls.create_site_context(pid, {
            "date": "2026-09-14", "location": "Spread A", "activity_uids": [101],
            "weather": "Heavy rain 14:00-16:30",
            "manpower": "Welders: 8\nFitters: 12",
            "equipment": "Crane: 1", "notes": "Access road soft after rain",
        }, actor="regression")
        check(len(context["evidence_ids"]) == 5, context)
        check((db.q1("SELECT percent_complete FROM activities WHERE project_id=? AND uid=101",
                     [pid]) or {}).get("percent_complete") == 0,
              "site context changed official progress")

        hindrance = controls.create_hindrance(pid, {
            "title": "Access road waterlogged", "description": "Crane could not enter",
            "category": "access", "started_on": "2026-09-14", "activity_uids": [101],
            "owner": "Civil contractor", "schedule_impact_days": 1.5,
            "create_readiness_blocker": True, "constraint_type": "access",
            "required_by": "2026-09-19",
        }, actor="regression")
        look = controls.lookahead(pid, days=42)
        first = next(item for item in look["activities"] if item["uid"] == 101)
        check(first["readiness"] == "blocked" and first["open_constraint_count"] == 1, first)

        event_id = db.insert("execution_events", {
            "project_id": pid, "canonical_key": "scope:valve-v220",
            "action_type": "installation", "event_state": "start",
            "event_date": "2026-09-16", "state": "observed", "confidence": .82,
        })
        evidence_id = db.insert("evidence", {
            "project_id": pid, "source_file": "DPR-14.csv", "date": "2026-09-16",
            "description": "Additional valve V-220 installation started",
            "state": "confirmed", "observation_type": "activity_progress",
            "provenance": "SOURCE_FILE",
        })
        db.insert("execution_event_sources", {"project_id": pid,
                                               "execution_event_id": event_id,
                                               "evidence_id": evidence_id})
        reality_graph.persist_relation(pid, event_id, {
            "relation": reality_graph.REL_NEW_SCOPE, "uids": [],
            "reason": "identifier absent from current schedule",
        })
        noise_event_id = db.insert("execution_events", {
            "project_id": pid, "canonical_key": "scope:reference-noise",
            "action_type": "observation", "event_state": "observation",
            "event_date": "2026-09-17", "state": "observed", "confidence": .2,
        })
        noise_evidence_id = db.insert("evidence", {
            "project_id": pid, "source_file": "challenge_difficulty_matrix.csv",
            "date": "2026-09-17", "description": "Handling change orders that create new activities",
            "state": "needs_review", "observation_type": "activity_progress",
            "provenance": "SOURCE_FILE",
        })
        db.insert("execution_event_sources", {"project_id": pid,
                                                "execution_event_id": noise_event_id,
                                                "evidence_id": noise_evidence_id})
        reality_graph.persist_relation(pid, noise_event_id, {
            "relation": reality_graph.REL_NEW_SCOPE, "uids": [],
            "reason": "no schedule candidate",
        })
        waiting = controls.new_scope_events(pid)
        check(len(waiting) == 1 and waiting[0]["execution_event_id"] == event_id, waiting)
        suggestion = controls.suggest_new_activity(pid, {
            "name": "Install additional valve V-220", "duration": 2,
            "parent_uid": 101, "source_event_id": event_id,
            "evidence_ids": [evidence_id], "identifier_type": "IFC_GUID",
            "identifier_value": "3hF8A2-V220-GUID", "model_name": "Pipeline Model",
            "reason": "Field-confirmed scope is absent from the current schedule.",
        }, actor="regression")
        check(suggestion["operation"] == "create" and
              suggestion["approval_state"] == "pending" and
              suggestion["validation_state"] == "passed", suggestion)
        check(not db.q1("SELECT id FROM activities WHERE project_id=? AND name=?",
                        [pid, "Install additional valve V-220"]),
              "suggestion bypassed governed schedule write")
        check(not controls.new_scope_events(pid), "proposed new scope stayed in waiting queue")

        linked = controls.create_bim_identifier(pid, {
            "activity_uid": 101, "identifier_type": "IFC_GUID",
            "identifier_value": "2KJ9-PIPE-A-101", "model_name": "Pipeline Model",
            "element_type": "IfcPipeSegment",
        }, actor="regression")
        activity = db.q1("SELECT * FROM activities WHERE project_id=? AND uid=101", [pid])
        search_text, metadata = engine.build_activity_document(activity or {})
        check(linked["activity_uid"] == 101 and "2KJ9-PIPE-A-101" in search_text,
              "confirmed BIM identifier did not enter retrieval document")
        check(any(item.get("type") == "bim" for item in metadata["asset_tags"]), metadata)
        try:
            controls.create_bim_identifier(pid, {
                "activity_uid": 102, "identifier_type": "IFC_GUID",
                "identifier_value": "2KJ9-PIPE-A-101", "model_name": "Pipeline Model",
            }, actor="regression")
        except ValueError:
            pass
        else:
            raise AssertionError("conflicting BIM identity was silently duplicated")
        check(bool(db.q1("SELECT id FROM conflicts WHERE project_id=? AND kind='bim_identifier_conflict'",
                         [pid])), "BIM identity conflict was not durable")

        confirmed_event = db.insert("execution_events", {
            "project_id": pid, "canonical_key": "verified:start:101",
            "activity_uid": 101, "action_type": "installation", "event_state": "start",
            "event_date": "2026-09-18", "state": "confirmed", "confidence": 1.0,
        })
        check(bool(confirmed_event), "verified execution event missing")
        timeline = controls.timeline(pid, window="42")
        first = next(item for item in timeline["activities"] if item["uid"] == 101)
        check(first["verified_actual_start"] == "2026-09-18", first)
        check(first["hindrance_count"] == 1 and first["constraint_count"] == 1, first)
        check(first["bim_identifier_count"] == 1 and first["evidence_count"] >= 5, first)

        controls.update_hindrance(pid, hindrance["id"], {"status": "cleared",
                                                          "cleared_on": "2026-09-16"},
                                  actor="regression")
        ready = controls.lookahead(pid, days=42)
        first = next(item for item in ready["activities"] if item["uid"] == 101)
        check(first["readiness"] == "ready", first)
        print("execution controls regression: PASS")
    finally:
        db.close()
        TMP.cleanup()


if __name__ == "__main__":
    main()
