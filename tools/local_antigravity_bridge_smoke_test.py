"""Offline regression checks for the Antigravity desktop bridge."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veda.agent import local_antigravity as bridge


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="veda_local_ag_") as raw:
        temp = Path(raw)
        home = temp / "home"
        workspace = temp / "VEDA-main"
        nested = workspace / "data"
        nested.mkdir(parents=True)
        descriptors = home / ".gemini" / "config" / "projects"
        descriptors.mkdir(parents=True)
        project_id = "11111111-2222-3333-4444-555555555555"
        (descriptors / (project_id + ".json")).write_text(json.dumps({
            "id": project_id,
            "projectResources": {
                "resources": [{"folderUri": workspace.as_uri()}],
            },
        }), encoding="utf-8")

        with mock.patch.object(bridge.Path, "home", return_value=home):
            actual = bridge._agentapi_project_id(str(nested))
        assert actual == project_id, (actual, project_id)

    with mock.patch.object(
            bridge, "_bridge_paths",
            return_value=(Path("C:/veda/request.json"),
                          Path("C:/veda/response.json"))):
        prompt = bridge._callback_prompt("inbox_test")
    assert "C:\\veda\\request.json" in prompt
    assert "C:\\veda\\response.json" in prompt
    assert '"inbox_id":"inbox_test"' in prompt
    assert "Do not fetch localhost URLs" in prompt

    with (mock.patch.object(bridge.config, "LOCAL_ANTIGRAVITY_FAST_MODEL",
                            "flash_lite"),
          mock.patch.object(bridge.config, "LOCAL_ANTIGRAVITY_DEEP_MODEL", "pro")):
        assert bridge._reasoning_mode("[VEDA_REASONING_MODE:FAST]\nhello") == "fast"
        assert bridge._reasoning_mode("analyse this schedule") == "deep"
        assert bridge._desktop_model("fast") == "flash_lite"
        assert bridge._desktop_model("deep") == "pro"

    with tempfile.TemporaryDirectory(prefix="veda_local_ag_logs_") as raw:
        temp = Path(raw)
        logs = temp / "Antigravity" / "logs"
        logs.mkdir(parents=True)
        (logs / "main.log").write_text(
            "Spawning: language_server.exe --csrf_token safe-test-token\n"
            "Local:       https://127.0.0.1:55101/\n", encoding="utf-8")
        (logs / "language_server.log").write_text(
            "Language server listening on random port at 55101 for HTTPS (gRPC)\n"
            "Language server listening on random port at 55102 for HTTP\n",
            encoding="utf-8")
        with (mock.patch.dict(bridge.os.environ, {"APPDATA": str(temp)}, clear=False),
              mock.patch.object(bridge.Path, "home", return_value=temp),
              mock.patch.object(bridge, "_standard_agentapi_executable",
                                return_value="C:\\Antigravity\\language_server.exe")):
            logged = bridge._logged_runtime_candidates()
        assert logged[0] == {
            "exe": "C:\\Antigravity\\language_server.exe",
            "address": "127.0.0.1:55102", "token": "safe-test-token",
        }, logged

    print("local Antigravity desktop bridge smoke test: PASS")


if __name__ == "__main__":
    main()
