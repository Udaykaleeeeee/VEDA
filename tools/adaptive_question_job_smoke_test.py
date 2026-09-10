"""End-to-end offline checks for Ask VEDA lane execution."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VEDA_DATA_DIR"] = tempfile.mkdtemp(prefix="veda_adaptive_job_")

from veda import db, jobs  # noqa: E402
from veda.agent import local_chat, schemas  # noqa: E402


def _job(project_id: str, question: str) -> str:
    job_id = jobs.create_job(project_id, "question", payload={"question": question})
    db.update("jobs", job_id, {"status": "running", "started_at": db.now()})
    return job_id


def main() -> None:
    db.init_db()
    project_id = db.insert("projects", {"name": "Adaptive Test", "status": "active"})

    instant_id = _job(project_id, "hi")
    instant = jobs._run_question(instant_id, project_id, {"question": "hi"})
    assert instant["reasoning_mode"] == "instant"
    assert instant["provider"] == "veda_local"

    fast_id = _job(project_id, "tell me a joke")
    fake_local = local_chat.LocalChatResult("A short local joke.", "qwen2.5:7b", 9)
    with (mock.patch.object(jobs.config, "LOCAL_CHAT_ENABLED", True),
          mock.patch.object(local_chat, "reply", return_value=fake_local)):
        fast = jobs._run_question(
            fast_id, project_id, {"question": "tell me a joke"})
    assert fast["reasoning_mode"] == "fast"
    assert fast["provider"] == "local_chat"

    hosted_fast_id = _job(project_id, "give me a productivity tip")
    with (mock.patch.object(jobs.config, "LOCAL_CHAT_ENABLED", False),
          mock.patch.object(
              jobs, "_invoke_agent",
              return_value=(schemas.AgentResult(summary="A quick tip."),
                            "local_antigravity")) as fast_invoke):
        hosted_fast = jobs._run_question(
            hosted_fast_id, project_id,
            {"question": "give me a productivity tip"})
    assert hosted_fast["reasoning_mode"] == "fast"
    assert hosted_fast["provider"] == "local_antigravity"
    assert fast_invoke.call_args.kwargs["reasoning_mode"] == "fast"

    deep_id = _job(project_id, "Which task is late?")
    with mock.patch.object(
            jobs, "_invoke_agent",
            return_value=(schemas.AgentResult(summary="Grounded."),
                          "local_antigravity")) as invoke:
        deep = jobs._run_question(
            deep_id, project_id, {"question": "Which task is late?"})
    assert deep["reasoning_mode"] == "deep"
    assert invoke.call_args.kwargs["reasoning_mode"] == "deep"

    print("adaptive Ask VEDA job execution smoke test: PASS")


if __name__ == "__main__":
    main()
