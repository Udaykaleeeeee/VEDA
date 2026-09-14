"""Focused offline checks for the planner workstation, voice correction and RAG pack."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="veda_review_voice_rag_")
os.environ["VEDA_DATA_DIR"] = TMP.name
sys.path.insert(0, str(ROOT))

from veda import db  # noqa: E402
from veda.api import routes  # noqa: E402
from veda.mcpc import veda_server  # noqa: E402
from veda.pipeline import field_capture  # noqa: E402


def check(value, message):
    if not value:
        raise AssertionError(message)


def main() -> None:
    try:
        db.init_db()
        project_id = db.insert("projects", {"name": "Reviewer UX regression"})
        for uid, display_id, name in (
                (501, "PIPE-WELD-01", "Mainline welding Spread A"),
                (502, "PIPE-WELD-02", "Mainline welding Spread B")):
            db.insert("activities", {
                "project_id": project_id, "uid": uid, "display_id": display_id,
                "name": name, "wbs": "Pipeline.Construction.Welding",
                "status": "in_progress", "is_summary": 0,
            })

        hinglish = field_capture.interpret(project_id, {
            "text": "Spread A me 4 joints welding complete ho gaya, 100%",
            "occurred_at": "2026-08-03T10:00",
        })
        check(hinglish["draft"]["event_state"] == "finish", hinglish)
        typo = field_capture.interpret(project_id, {
            "text": "Spread A 4 joints welding complet ho gya, 100%",
            "occurred_at": "2026-08-03T10:00",
        })
        check(typo["draft"]["event_state"] == "finish", typo)
        remaining = field_capture.interpret(project_id, {
            "text": "PIPE-WELD-01 par kaam shuru, 2 din baki",
            "occurred_at": "2026-08-03T10:00",
        })
        check(remaining["draft"]["event_state"] == "start", remaining)
        check(remaining["draft"]["remaining_days"] == 2.0, remaining)

        evidence_id = db.insert("evidence", {
            "project_id": project_id, "source_file": "DPR-2026-08-03.xlsx",
            "locator": "Welding!R18", "date": "2026-08-03",
            "description": "Spread A me 4 joints welding complete ho gaya",
            "activity_description": "Mainline welding Spread A",
            "document_type": "dpr", "observation_type": "activity_progress",
            "extraction_method": "typed_register", "extraction_confidence": 0.94,
            "security_state": "clean", "state": "needs_review",
        })
        review = {"kind": "clarification", "options": ["Spread A", "Spread B"],
                  "context": {"option_uids": {"Spread A": 501, "Spread B": 502}}}
        component_features = {
            "rerank": .91, "dense": .82, "sparse": .88, "asset_exact": 1.0,
            "action": .95, "phase": .9, "location": 1.0, "wbs": .86,
            "discipline": 1.0, "temporal": .8, "date_corroboration": .75,
            "graph": .7, "driving_pred_ready": .8, "relationship_consistency": .7,
            "source_trust": .8, "historical_sequence": .72,
        }
        for uid, probability, score in ((501, .87, .90), (502, .72, .76)):
            db.insert("evidence_links", {
                "project_id": project_id, "evidence_id": evidence_id,
                "activity_uid": uid, "activity_name": "candidate",
                "rank_score": score, "calibrated_probability": probability,
                "calibration_is_empirical": 1, "feature_json": db.jdumps(component_features),
                "supporting_signals": db.jdumps(["location agrees"]),
                "conflicting_signals": db.jdumps([]), "is_candidate": 1,
            })
            db.insert("evidence_links", {
                "project_id": project_id, "evidence_id": evidence_id,
                "activity_uid": uid, "activity_name": "confirmed multi-activity scope",
                "confidence": .91, "relation": "part_of", "is_candidate": 0,
                "human_decision": "accepted",
            })
        db.insert("execution_events", {
            "project_id": project_id, "activity_uid": 501,
            "event_state": "finish", "event_date": "2026-08-03",
            "observed_progress": 100, "quantity": 4, "unit": "joints",
            "confidence": .94, "source_count": 1, "state": "observed",
        })
        db.insert("relationships", {
            "project_id": project_id, "pred_uid": 501, "succ_uid": 502,
            "pred_name": "Mainline welding Spread A",
            "succ_name": "Mainline welding Spread B", "type": "FS",
            "lag_days": 0, "driving": 1,
        })
        explained = routes._review_candidate_explanations(project_id, review, [evidence_id])
        check(len(explained) == 2 and len(explained[0]["score_components"]) == 8, explained)
        check(explained[0]["review_band"] == "strong" and not explained[0]["ambiguous"], explained)
        db.ex("UPDATE evidence_links SET calibrated_probability=? "
              "WHERE evidence_id=? AND activity_uid=? AND is_candidate=1", [.81, evidence_id, 501])
        db.ex("UPDATE evidence_links SET calibrated_probability=? "
              "WHERE evidence_id=? AND activity_uid=? AND is_candidate=1", [.75, evidence_id, 502])
        ambiguous = routes._review_candidate_explanations(project_id, review, [evidence_id])
        check(ambiguous[0]["review_band"] == "review" and ambiguous[0]["ambiguous"], ambiguous)

        fake_candidates = [{
            "activity": db.q1("SELECT * FROM activities WHERE project_id=? AND uid=501",
                              [project_id]),
            "score": .9, "features": component_features,
            "supporting": ["exact activity identifier"], "conflicting": [],
        }]
        with mock.patch("veda.retrieval.engine.hybrid_search", return_value={
                "candidates": fake_candidates, "diagnostics": {}}):
            packed = veda_server.t_grounded_search({
                "query": "What field evidence supports PIPE-WELD-01?", "limit": 8,
                "expand_graph": True,
            }, PROJECT_ID=project_id)
        body = json.loads(packed["content"][0]["text"])
        citations = {item["citation"] for item in body["context_items"]}
        check("activity:501" in citations and "evidence:" + evidence_id in citations, body)
        check(any(item["kind"] == "canonical_execution_event"
                  for item in body["context_items"]), body)
        check(any(item["kind"] == "schedule_relationship"
                  for item in body["context_items"]), body)
        evidence_item = next(item for item in body["context_items"]
                             if item["citation"] == "evidence:" + evidence_id)
        check(evidence_item["activity_uids"] == [501, 502], evidence_item)
        check(body["mode"] == "execution_evidence", body)

        views = (ROOT / "veda" / "web" / "views.js").read_text(encoding="utf-8")
        capture = (ROOT / "veda" / "web" / "field-capture.js").read_text(encoding="utf-8")
        check("review-queue-shell" in views and "score-components" in views, "review workstation missing")
        check("const text = el('capture-confirmed').value.trim();" in capture,
              "voice extraction must use the corrected transcript")
        check("invalidateExtraction('Re-extract corrected transcript')" in capture,
              "transcript edits must invalidate stale extraction")
        print("review / voice correction / grounded retrieval regression: PASS")
    finally:
        db.close()
        TMP.cleanup()


if __name__ == "__main__":
    main()
