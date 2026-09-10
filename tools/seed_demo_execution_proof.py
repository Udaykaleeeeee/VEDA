"""Add explicitly labelled human-confirmed mappings to the synthetic demo only."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from veda import config, db  # noqa: E402
from veda.pipeline import proof_contract  # noqa: E402


def typed(row):
    raw = db.jloads(row.get("raw_json"), {}) or {}
    return raw.get("typed") if isinstance(raw.get("typed"), dict) else raw


def confirm(project_id: str, activity: dict, evidence: dict):
    if db.q1("SELECT id FROM evidence_links WHERE evidence_id=? AND activity_uid=? "
             "AND human_decision='accepted'", [evidence["id"], activity["uid"]]):
        return
    db.insert("evidence_links", {
        "project_id": project_id, "evidence_id": evidence["id"],
        "activity_uid": activity["uid"], "activity_name": activity["name"],
        "confidence": 1.0, "calibrated_probability": 1.0,
        "calibration_mode": "synthetic_demo_confirmation",
        "policy_decision": "human_confirmed", "recommended_uid": activity["uid"],
        "committed_uid": activity["uid"], "relation": "supporting",
        "supporting_signals": db.jdumps([
            "Synthetic demo fixture: planner confirmed this traceability mapping"]),
        "validator_result": "pass", "human_decision": "accepted",
        "decided_by": "synthetic.demo.planner", "decided_at": db.now(),
        "is_candidate": 0, "provenance": "HUMAN_INPUT",
    })
    db.update("evidence", evidence["id"], {"state": "confirmed"})


def main():
    data_dir = Path(config.DATA_DIR).resolve()
    if not data_dir.name.lower().startswith("demo-"):
        raise SystemExit("Refusing to seed a non-demo data directory: " + str(data_dir))
    db.init_db()
    project = db.q1("SELECT * FROM projects ORDER BY created_at DESC LIMIT 1")
    if not project:
        raise SystemExit("No demo project")
    activity = db.q1("SELECT * FROM activities WHERE project_id=? AND lower(name)=?",
                     [project["id"], "welding - spread a"])
    if not activity:
        raise SystemExit("Synthetic demo welding activity not found")
    weld_rows = db.q("SELECT * FROM evidence WHERE project_id=? AND document_type='WELDING_REGISTER'",
                     [project["id"]])
    ndt_rows = db.q("SELECT * FROM evidence WHERE project_id=? AND document_type='NDT_REGISTER'",
                    [project["id"]])
    weld_by_id = {str(typed(row).get("weld_no") or "").upper(): row for row in weld_rows}
    ndt_by_id = {}
    for row in ndt_rows:
        raw = typed(row)
        if re.search(r"\baccept(?:ed)?\b", str(raw.get("result") or ""), re.I):
            ndt_by_id[str(raw.get("weld_no") or "").upper()] = row
    shared = sorted(set(weld_by_id) & set(ndt_by_id))[:12]
    if not shared:
        raise SystemExit("Synthetic welding/NDT fixtures have no shared weld identities")
    dpr = db.q1("SELECT * FROM evidence WHERE project_id=? "
                "AND document_type IN ('DAILY_PROGRESS_REPORT','DAILY_CONSTRUCTION_REPORT') "
                "AND lower(COALESCE(discipline,''))='welding' "
                "AND lower(COALESCE(location,'')) LIKE '%spread a%' ORDER BY date LIMIT 1",
                [project["id"]])
    if not dpr:
        raise SystemExit("Synthetic demo DPR welding observation not found")
    confirm(project["id"], activity, dpr)
    for weld_id in shared:
        confirm(project["id"], activity, weld_by_id[weld_id])
        confirm(project["id"], activity, ndt_by_id[weld_id])
    custom = db.jloads(activity.get("custom_json"), {}) or {}
    custom.update({"total_welds": len(shared),
                   "demo_scope_note": "Synthetic verified subset for Execution Proof Contract demo"})
    db.update("activities", activity["id"], {"custom_json": json.dumps(custom)})
    certificate = proof_contract.certify(project["id"], int(activity["uid"]),
                                         created_by="synthetic.demo.planner")
    if certificate["certificate"].get("status") != "admissible":
        raise SystemExit("Synthetic demo certificate did not become admissible")
    print("Synthetic demo Execution Proof Certificate:", certificate["id"],
          "for", len(shared), "verified welds")
    db.close()


if __name__ == "__main__":
    main()
