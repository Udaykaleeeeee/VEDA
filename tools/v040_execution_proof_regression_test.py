"""Offline regression for VEDA 0.4 execution-proof and governance contracts."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="veda_v040_")
os.environ["VEDA_DATA_DIR"] = TMP.name
sys.path.insert(0, str(ROOT))

from veda import config, db  # noqa: E402
from veda.integrations import primavera  # noqa: E402
from veda.pipeline import conflicts, extract, field_capture, ingest, proof_contract, proposals  # noqa: E402
from veda.resolution import reality_graph  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def add_activity(project_id: str, uid: int = 1001):
    db.insert("activities", {
        "project_id": project_id, "uid": uid, "display_id": "PIPE-WELD-01",
        "name": "Mainline welding Spread A", "wbs": "Pipeline.Construction.Welding",
        "status": "in_progress", "percent_complete": 0,
        "custom_json": db.jdumps({"total_welds": 1}),
    })


def source_schema_checks(project_id: str):
    samples = [
        (ROOT / "sample_data" / "DPR_June_2025.csv", "DAILY_PROGRESS_REPORT", "DPR"),
        (ROOT / "sample_data" / "Welding_Register.csv", "WELDING_REGISTER", "WELDING_REGISTER"),
        (ROOT / "sample_data" / "demo" / "NDT_Radiography_Report_W27.csv", "NDT_REGISTER", "NDT_REGISTER"),
        (ROOT / "sample_data" / "QAQC_NCR_Log.csv", "NCR_REGISTER", "NCR_REGISTER"),
        (ROOT / "sample_data" / "demo" / "Material_Receiving_Report.csv", "MATERIAL_REGISTER", "MATERIAL_REGISTER"),
    ]
    for path, document_type, schema_name in samples:
        stored = ingest.store_upload(project_id, path.name, path.read_bytes())
        stored_row = db.q1("SELECT * FROM files WHERE id=?", [stored["id"]])
        rows = extract.extract_evidence(project_id, stored_row)
        current = db.q1("SELECT * FROM files WHERE id=?", [stored["id"]])
        check(bool(rows), path.name + " produced no typed rows")
        check(current["document_type"] == document_type,
              path.name + " classified as " + str(current["document_type"]))
        check(current["schema_name"] == schema_name, path.name + " schema missing")
        check(current["relevance_state"] == "evidence", path.name + " relevance incorrect")
        check(all(row.get("document_type") == document_type for row in rows[:5]),
              path.name + " row type drift")


def linked_evidence(project_id: str, uid: int, *, doc_type: str, obs_type: str,
                    description: str, raw: dict, day: str):
    evidence_id = db.insert("evidence", {
        "project_id": project_id, "source_file": doc_type + ".csv",
        "locator": "row 2", "date": day, "description": description,
        "document_type": doc_type, "observation_type": obs_type,
        "raw_json": db.jdumps({"typed": raw}), "state": "linked",
        "security_state": "clean", "provenance": "SOURCE_FILE",
    })
    db.insert("evidence_links", {
        "project_id": project_id, "evidence_id": evidence_id,
        "activity_uid": uid, "committed_uid": uid, "activity_name": "Mainline welding Spread A",
        "relation": "supporting", "is_candidate": 0, "validator_result": "pass",
        "provenance": "DETERMINISTIC_CALCULATION",
    })
    return evidence_id


def proof_checks(project_id: str, uid: int):
    ids = [
        linked_evidence(project_id, uid, doc_type="DAILY_PROGRESS_REPORT",
                        obs_type="activity_progress", description="Welding started Spread A",
                        raw={"description": "Welding started", "quantity": 1, "unit": "joints"},
                        day="2026-08-01"),
        linked_evidence(project_id, uid, doc_type="WELDING_REGISTER",
                        obs_type="quality_gate", description="Welding joint W-TEST-001 accepted",
                        raw={"weld_no": "W-TEST-001", "status": "Accepted"},
                        day="2026-08-02"),
        linked_evidence(project_id, uid, doc_type="NDT_REGISTER",
                        obs_type="quality_gate", description="RT accepted W-TEST-001",
                        raw={"weld_no": "W-TEST-001", "result": "Accept", "ndt_method": "RT"},
                        day="2026-08-03"),
    ]
    cert = proof_contract.certify(project_id, uid, created_by="regression")
    payload = cert["certificate"]
    check(payload["status"] == "admissible", payload)
    check(payload["recommended_actuals"].get("percentComplete") == 100.0, payload)
    check(payload["recommended_actuals"].get("actualFinish") == "2026-08-03", payload)
    same = proof_contract.certify(project_id, uid, created_by="regression")
    check(same["id"] == cert["id"], "unchanged proof created a duplicate certificate")

    linked_evidence(project_id, uid, doc_type="NCR_REGISTER", obs_type="issue",
                    description="NCR-TEST open", raw={"ncr_no": "NCR-TEST", "status": "Open"},
                    day="2026-08-03")
    blocked = proof_contract.certify(project_id, uid, created_by="regression")
    check(blocked["certificate"]["status"] == "blocked", blocked)
    check(bool(conflicts.list_for_project(project_id, "open")), "blocked proof conflict was not durable")
    return ids


def reality_and_conflict_checks(project_id: str):
    event_id = db.insert("execution_events", {
        "project_id": project_id, "canonical_key": "new-scope-test",
        "action_type": "welding", "event_state": "progress", "state": "observed",
    })
    relation = {"relation": reality_graph.REL_AGGREGATES, "uids": [1001, 1002],
                "confidence": .8, "reason": "one DPR covers two spreads"}
    reality_graph.persist_relation(project_id, event_id, relation)
    reality_graph.persist_relation(project_id, event_id, relation)
    rows = db.q("SELECT * FROM execution_event_links WHERE execution_event_id=?", [event_id])
    check(len(rows) == 2, "set-valued reality relation was not idempotent")
    first = conflicts.upsert(project_id, kind="test", detail="first", entity_type="event",
                             entity_id=event_id, field="state")
    second = conflicts.upsert(project_id, kind="test", detail="updated", entity_type="event",
                              entity_id=event_id, field="state")
    check(first == second, "durable conflict was duplicated")


def atomic_approval_checks(project_id: str):
    group_id = "group_atomic_test"
    ids = []
    for field, value in (("actualFinish", "2026-08-03"), ("percentComplete", "100")):
        ids.append(db.insert("proposals", {
            "project_id": project_id, "target_uid": 1001, "target_type": "activity",
            "operation": "update", "field": field, "proposed_value": value,
            "proposal_group_id": group_id, "validation_state": "passed",
            "dryrun_state": "ok", "approval_state": "pending",
            "execution_state": "not_executed", "verification_state": "not_verified",
            "updated_at": db.now(),
        }))
    proposals._refresh_group(group_id, project_id=project_id)
    proposals.approve_group(group_id, approved_by="regression", approve_it=True)
    rows = db.q("SELECT approval_state,approved_at FROM proposals WHERE proposal_group_id=?",
                [group_id])
    check(len(rows) == 2 and all(row["approval_state"] == "approved" for row in rows),
          "proposal bundle was not approved atomically")
    check(len({row["approved_at"] for row in rows}) == 1, "bundle approval timestamps differ")


def p6_readback_check(project_id: str):
    db.ex("UPDATE activities SET custom_json=? WHERE project_id=? AND uid=?",
          [db.jdumps({"total_welds": 1, "ObjectId": 7001}), project_id, 1001])
    proposal_id = db.insert("proposals", {
        "project_id": project_id, "target_uid": 1001, "target_type": "activity",
        "operation": "update", "field": "percentComplete", "proposed_value": "55",
        "approval_state": "approved", "approved_by": "regression",
        "validation_state": "passed", "dryrun_state": "ok", "updated_at": db.now(),
    })

    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.content = b"{}"
        def raise_for_status(self):
            return None
        def json(self):
            return self.payload

    class Client:
        value = 10.0
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, *args, **kwargs):
            return Response([{"ObjectId": 7001, "ProjectObjectId": 9001,
                              "PercentCompleteType": "Duration",
                              "DurationPercentComplete": self.value}])
        def put(self, *args, **kwargs):
            Client.value = float(kwargs["json"][0]["DurationPercentComplete"])
            self.value = Client.value
            return Response({"success": True})

    settings = {
        "P6_ENVIRONMENT": "sandbox", "P6_WRITE_ENABLED": True,
        "P6_ALLOWED_PROJECT_IDS": {"9001"}, "P6_UID_IS_OBJECT_ID": False,
        "P6_BASE_URL": "https://p6.invalid", "P6_TOKEN_URL": "https://auth.invalid",
        "P6_CLIENT_ID": "client", "P6_CLIENT_SECRET": "secret",
    }
    patches = [mock.patch.object(config, key, value) for key, value in settings.items()]
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
            mock.patch.object(primavera, "_token", return_value="token"), \
            mock.patch.object(primavera.httpx, "Client", Client):
        result = primavera.write_approved(proposal_id, p6_project_object_id="9001",
                                          actor="regression")
    check(result["ok"] and result["verification"] == "verified", result)
    stored = db.q1("SELECT verification_state,resulting_value FROM proposals WHERE id=?",
                   [proposal_id])
    check(stored["verification_state"] == "verified" and float(stored["resulting_value"]) == 55,
          "P6 read-after-write result was not persisted")


def main():
    try:
        db.init_db()
        project_id = db.insert("projects", {"name": "v0.4 regression", "status": "active",
                                             "updated_at": db.now()})
        add_activity(project_id)
        db.insert("activities", {"project_id": project_id, "uid": 1002,
                                  "display_id": "PIPE-WELD-02", "name": "Welding Spread B"})
        source_schema_checks(project_id)
        proof_checks(project_id, 1001)
        reality_and_conflict_checks(project_id)
        atomic_approval_checks(project_id)
        p6_readback_check(project_id)
        interpreted = field_capture.interpret(project_id, {
            "text": "Completed 1 weld W-TEST-009 at Spread A, 100%",
            "occurred_at": "2026-08-03T10:00"})
        check(interpreted["draft"]["event_state"] in {"progress", "finish"}, interpreted)
        print("VEDA 0.4 execution-proof regression: PASS")
    finally:
        db.close()
        TMP.cleanup()


if __name__ == "__main__":
    main()
