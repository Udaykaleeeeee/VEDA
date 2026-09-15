"""Bridge VEDA to the already-running Antigravity desktop agent."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import unquote, urlparse

from .. import config, db
from .base import AgentEvent, AgentProvider, AgentSession

# How long to wait once the IDE agent has claimed a job (seconds).
INBOX_TIMEOUT = int(config.AGENT_TIMEOUT)
# How long an unclaimed inbox item may block the single VEDA worker.  The IDE
# bridge is useful only when something is actively polling /api/agent/inbox;
# without this guard one missing watcher stalls every project for 15 minutes.
CLAIM_TIMEOUT = max(1, min(INBOX_TIMEOUT, int(config.AGENT_CLAIM_TIMEOUT)))

# The bridge is now signal-driven: the HTTP handlers that claim a job and post a
# result wake this waiter directly, so a finished job is observed in roughly the
# time one SQLite read takes rather than up to a full poll interval.  Polling
# remains only as a safety net for a result written by another process, and it
# backs off from a fast first check to a quiet steady state.
POLL_MIN = 0.05
POLL_MAX = 1.0
POLL_GROWTH = 1.6

CONSUMER_TTL = max(5.0, float(CLAIM_TIMEOUT) * 3.0)
_CONSUMER_LAST_SEEN = 0.0
DIRECT_CLAIM_TIMEOUT = max(30, CLAIM_TIMEOUT)
_RUNTIME_CACHE: tuple[float, dict] | None = None
_RUNTIME_LOCK = threading.Lock()
_RUNTIME_TTL = 15.0

# inbox_id -> Event.  Set by notify() from the API layer.
_SIGNALS: dict = {}
_SIGNAL_LOCK = threading.Lock()


def consumer_seen() -> None:
    global _CONSUMER_LAST_SEEN
    _CONSUMER_LAST_SEEN = time.monotonic()


def consumer_connected() -> bool:
    return (time.monotonic() - _CONSUMER_LAST_SEEN) <= CONSUMER_TTL


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _standard_agentapi_executable() -> str | None:
    explicit = str(os.environ.get("ANTIGRAVITY_AGENTAPI_EXE") or "").strip().strip('"')
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(
            Path(local) / "Programs" / "Antigravity" / "resources" / "bin" /
            "language_server.exe")
    candidates.append(
        Path.home() / "AppData" / "Local" / "Programs" / "Antigravity" /
        "resources" / "bin" / "language_server.exe")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            continue
    return shutil.which("language_server") or shutil.which("language_server.exe")


def _agentapi_project_id(cwd: str) -> str:
    requested = Path(cwd).resolve()
    project_dir = Path.home() / ".gemini" / "config" / "projects"
    matches: list[tuple[int, str]] = []
    try:
        descriptors = list(project_dir.glob("*.json"))
    except OSError:
        descriptors = []
    for descriptor in descriptors:
        try:
            data = json.loads(descriptor.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        project_id = str(data.get("id") or "").strip()
        resources = ((data.get("projectResources") or {}).get("resources") or [])
        for resource in resources:
            uri = str((resource or {}).get("folderUri") or "")
            if not project_id or not uri.lower().startswith("file:"):
                continue
            parsed = urlparse(uri)
            raw_path = unquote(parsed.path or "")
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", raw_path):
                raw_path = raw_path[1:]
            try:
                root = Path(raw_path).resolve()
                requested.relative_to(root)
            except (OSError, ValueError):
                continue
            matches.append((len(root.parts), project_id))
    if matches:
        return max(matches, key=lambda item: item[0])[1]
    return str(requested)


def _agentapi_call(runtime: dict, args: list[str], *, cwd: str | None = None,
                   timeout: float = 20.0) -> dict:
    env = dict(os.environ)
    env["ANTIGRAVITY_AGENTAPI_EXE"] = runtime["exe"]
    env["ANTIGRAVITY_LS_ADDRESS"] = runtime["address"]
    env["ANTIGRAVITY_CSRF_TOKEN"] = runtime["token"]
    if cwd:
        env["ANTIGRAVITY_PROJECT_ID"] = _agentapi_project_id(cwd)
    proc = subprocess.run(
        [runtime["exe"], "agentapi", *args],
        cwd=cwd or str(config.ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
        creationflags=_creation_flags())
    raw = (proc.stdout or "").strip()
    if not raw:
        detail = (proc.stderr or "").strip()
        raise RuntimeError(detail[:600] or "Antigravity Agent API returned no response")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Antigravity Agent API returned invalid JSON: " +
                           raw[:400]) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Antigravity Agent API returned an invalid response")
    return payload


def _injected_runtime() -> dict | None:
    exe = _standard_agentapi_executable()
    address = str(os.environ.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
    token = str(os.environ.get("ANTIGRAVITY_CSRF_TOKEN") or "").strip()
    if exe and address and token:
        return {"exe": exe, "address": address, "token": token}
    return None


def _tail_text(path: Path, limit: int = 1_000_000) -> str:
    """Read only the recent portion of a desktop log used for discovery."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit), os.SEEK_SET)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _logged_runtime_candidates() -> list[dict]:
    """Discover the current desktop endpoint without privileged process APIs.

    Managed Windows sessions can deny Win32_Process and TCP ownership queries
    even for a process owned by the signed-in user.  Antigravity records the
    random local ports and CSRF token in its own per-user startup logs, so those
    logs are a safe, least-privilege fallback.  Every candidate is still probed
    by ``_runtime_works`` before it can be cached or used.
    """
    if os.name != "nt":
        return []
    roaming = os.environ.get("APPDATA")
    roots = []
    if roaming:
        roots.append(Path(roaming) / "Antigravity" / "logs")
    roots.append(Path.home() / "AppData" / "Roaming" / "Antigravity" / "logs")
    exe = _standard_agentapi_executable()
    if not exe:
        return []

    candidates: list[dict] = []
    seen_roots: set[str] = set()
    for logs in roots:
        key = str(logs).lower()
        if key in seen_roots:
            continue
        seen_roots.add(key)
        main_log = _tail_text(logs / "main.log")
        server_log = _tail_text(logs / "language_server.log")
        tokens = re.findall(
            r"--csrf_token(?:=|\s+)(?:\"([^\"]+)\"|(\S+))", main_log)
        token = next((quoted or plain for quoted, plain in reversed(tokens)
                      if quoted or plain), "")
        if not token:
            continue

        # The HTTP listener is the preferred Agent API endpoint.  Keep HTTPS
        # and the desktop's displayed local URL as verified fallbacks because
        # Antigravity has changed its log wording between releases.
        http_ports = re.findall(r"port at (\d+) for HTTP\b", server_log)
        https_ports = re.findall(r"port at (\d+) for HTTPS\b", server_log)
        local_ports = re.findall(r"Local:\s+https?://127\.0\.0\.1:(\d+)", main_log)
        ports = list(reversed(http_ports)) + list(reversed(https_ports)) + list(reversed(local_ports))
        seen_ports: set[str] = set()
        for port in ports:
            if port in seen_ports:
                continue
            seen_ports.add(port)
            candidates.append({"exe": exe, "address": "127.0.0.1:" + port,
                               "token": token})
    return candidates


def _windows_runtime_candidates() -> list[dict]:
    if os.name != "nt":
        return []
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return []
    script = r'''
$rows = foreach ($p in Get-CimInstance Win32_Process -Filter "Name = 'language_server.exe'") {
  $m = [regex]::Match($p.CommandLine, '--csrf_token(?:=|\s+)(?:"(?<q>[^"]+)"|(?<u>\S+))')
  if (-not $m.Success) { continue }
  $token = if ($m.Groups['q'].Success) { $m.Groups['q'].Value } else { $m.Groups['u'].Value }
  $ports = @(Get-NetTCPConnection -OwningProcess $p.ProcessId -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') } |
    Sort-Object LocalPort | Select-Object -ExpandProperty LocalPort -Unique)
  if ($ports.Count -gt 0) {
    [pscustomobject]@{ exe = $p.ExecutablePath; token = $token; ports = $ports }
  }
}
ConvertTo-Json -InputObject @($rows) -Compress -Depth 4
'''
    rows = []
    try:
        proc = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False, creationflags=_creation_flags())
        rows = json.loads((proc.stdout or "[]").strip() or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    if isinstance(rows, dict):
        rows = [rows]
    candidates: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        exe = str((row or {}).get("exe") or _standard_agentapi_executable() or "")
        token = str((row or {}).get("token") or "")
        ports = (row or {}).get("ports") or []
        if isinstance(ports, (str, int)):
            ports = [ports]
        if not exe or not token:
            continue
        for port in reversed(ports):
            candidates.append({"exe": exe, "address": "127.0.0.1:" + str(port),
                               "token": token})
    candidates.extend(_logged_runtime_candidates())
    return candidates


def _runtime_works(runtime: dict) -> bool:
    try:
        payload = _agentapi_call(
            runtime, ["get-conversation-metadata", "veda-health-" + uuid.uuid4().hex],
            timeout=8.0)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return False
    error = str(payload.get("error") or "").lower()
    return not error or "trajectory not found" in error


def _discover_runtime(*, force: bool = False) -> dict | None:
    global _RUNTIME_CACHE
    with _RUNTIME_LOCK:
        now = time.monotonic()
        if (not force and _RUNTIME_CACHE is not None and
                now - _RUNTIME_CACHE[0] <= _RUNTIME_TTL):
            return dict(_RUNTIME_CACHE[1])
        injected = _injected_runtime()
        candidates = ([injected] if injected else []) + _windows_runtime_candidates()
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            if not candidate:
                continue
            key = (candidate["exe"], candidate["address"])
            if key in seen:
                continue
            seen.add(key)
            if _runtime_works(candidate):
                _RUNTIME_CACHE = (now, dict(candidate))
                return dict(candidate)
        _RUNTIME_CACHE = None
        return None


def _callback_prompt(inbox_id: str) -> str:
    request_path, response_path = _bridge_paths(inbox_id)
    inbox = db.q1(
        "SELECT i.*, j.kind FROM agent_inbox i JOIN jobs j ON j.id=i.job_id "
        "WHERE i.id=?", [inbox_id]) or {}
    inline_request = ""
    if inbox.get("kind") == "field_language":
        try:
            raw = request_path.read_text(encoding="utf-8")
            if len(raw) <= 12_000:
                inline_request = ("The complete request is included below as data; "
                                  "do not spend a tool call reading it.\n"
                                  "<VEDA_REQUEST_JSON>\n" + raw +
                                  "\n</VEDA_REQUEST_JSON>\n")
        except OSError:
            pass
    request_instruction = (inline_request or
                           "Read this exact JSON request file with your file-reading tool:\n"
                           + str(request_path) + "\n")
    return f"""You are the local reasoning worker for VEDA.

This is a single bounded structured-extraction task. Execute it immediately;
do not create a plan, artifact, walkthrough, or research task.

{request_instruction}

Follow its system_prompt and prompt and return the supplied schema. Then write
one JSON object to this exact response file with your file-writing tool:
{response_path}

Use this exact envelope:
{{"inbox_id":"{inbox_id}","result":<your final JSON>,"events":[]}}

Do not fetch localhost URLs. Do not edit project source files. The response file
is the only file you may create or change. Do not only reply in the desktop
conversation; the job is complete only after that response file is written."""


def _bridge_paths(inbox_id: str) -> tuple[Path, Path]:
    root = config.DATA_DIR / "runtime" / "antigravity_bridge"
    return (root / (inbox_id + ".request.json"),
            root / (inbox_id + ".response.json"))


def _prepare_file_request(inbox_id: str, *, project_id: str, job_id: str,
                          prompt: str, system: str,
                          schema: dict | None) -> None:
    request_path, response_path = _bridge_paths(inbox_id)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        response_path.unlink(missing_ok=True)
    except OSError:
        pass
    payload = {
        "inbox_id": inbox_id, "project_id": project_id, "job_id": job_id,
        "system_prompt": system or "", "prompt": prompt,
        "schema": schema, "created_at": db.now(),
    }
    temporary = request_path.with_suffix(".request.json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    os.replace(temporary, request_path)


def _file_result(inbox_id: str) -> dict | None:
    _request_path, response_path = _bridge_paths(inbox_id)
    try:
        if not response_path.is_file() or response_path.stat().st_size > 2_000_000:
            return None
        payload = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("inbox_id") != inbox_id:
        return None
    if "result" not in payload and not payload.get("error"):
        return None
    return payload


def _persist_file_result(inbox_id: str, payload: dict) -> dict:
    inbox = db.q1("SELECT * FROM agent_inbox WHERE id=?", [inbox_id]) or {}
    existing = db.q1("SELECT * FROM agent_outbox WHERE inbox_id=? "
                     "ORDER BY created_at DESC LIMIT 1", [inbox_id])
    if existing:
        return existing
    outbox_id = db.insert("agent_outbox", {
        "inbox_id": inbox_id, "project_id": inbox.get("project_id") or "",
        "job_id": inbox.get("job_id") or "",
        "result_json": (db.jdumps(payload.get("result"))
                        if "result" in payload else None),
        "error": str(payload.get("error") or "")[:500] or None,
        "events_json": (db.jdumps(payload.get("events"))
                        if payload.get("events") else None),
    })
    db.update("agent_inbox", inbox_id, {
        "status": "done", "finished_at": db.now()})
    return db.q1("SELECT * FROM agent_outbox WHERE id=?", [outbox_id]) or {}


def _cleanup_file_bridge(inbox_id: str) -> None:
    for path in _bridge_paths(inbox_id):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _prune_file_bridge() -> None:
    """Remove completed bridge envelopes while preserving active work."""
    root = config.DATA_DIR / "runtime" / "antigravity_bridge"
    try:
        files = list(root.glob("*.json")) + list(root.glob("*.tmp"))
    except OSError:
        return
    for path in files:
        inbox_id = path.name.split(".", 1)[0]
        inbox = db.q1("SELECT status FROM agent_inbox WHERE id=?", [inbox_id])
        if inbox and inbox.get("status") in {"pending", "claimed"}:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _reasoning_mode(prompt: str, config_hint: dict | None = None) -> str:
    hinted = str((config_hint or {}).get("veda_reasoning_mode") or "").lower()
    if hinted in ("fast", "deep"):
        return hinted
    if "[VEDA_REASONING_MODE:FAST]" in str(prompt or ""):
        return "fast"
    return "deep"


def _desktop_model(mode: str) -> str:
    requested = (config.LOCAL_ANTIGRAVITY_FAST_MODEL if mode == "fast" else
                 config.LOCAL_ANTIGRAVITY_DEEP_MODEL)
    if requested in ("flash_lite", "flash", "pro"):
        return requested
    return "flash_lite" if mode == "fast" else "pro"


def _signal_for(inbox_id: str) -> threading.Event:
    with _SIGNAL_LOCK:
        ev = _SIGNALS.get(inbox_id)
        if ev is None:
            ev = _SIGNALS[inbox_id] = threading.Event()
        return ev


def _release_signal(inbox_id: str) -> None:
    with _SIGNAL_LOCK:
        _SIGNALS.pop(inbox_id, None)


def notify(inbox_id: str | None) -> None:
    """Wake the waiter for one inbox item.

    Called by the agent-bridge HTTP routes the moment an item is claimed or a
    result is posted.  Safe to call for an unknown id.
    """
    if not inbox_id:
        return
    with _SIGNAL_LOCK:
        ev = _SIGNALS.get(inbox_id)
    if ev is not None:
        ev.set()


class LocalAntigravityProvider(AgentProvider):
    """Bridges VEDA's job system to the running Antigravity IDE agent.

    The lifecycle is:
      1. start_session() writes the prompt + schema to agent_inbox
      2. stream_events() polls agent_outbox until a result appears
      3. The Antigravity agent (polling /api/agent/inbox) picks up
         the job, reasons, and POSTs to /api/agent/result
      4. stream_events() finds the result in the outbox and yields it
    """
    name = "local_antigravity"
    model = "adaptive"

    def __init__(self):
        self._sessions: dict = {}
        self._queues: dict = {}
        self._cancel: dict = {}

    async def health(self) -> dict:
        await asyncio.to_thread(_prune_file_bridge)
        runtime = await asyncio.to_thread(_discover_runtime)
        direct = runtime is not None
        manual = consumer_connected()
        connected = direct or manual
        return {
            "ok": connected,
            "provider": self.name,
            "model": ("Adaptive: " + config.LOCAL_ANTIGRAVITY_FAST_MODEL +
                      " chat / " + config.LOCAL_ANTIGRAVITY_DEEP_MODEL +
                      " project"),
            "error": None if connected else
                     "The running desktop reasoning agent could not be reached.",
            "connection": ("desktop_agentapi" if direct else
                           "manual_inbox" if manual else None),
            "note": ("Connected directly to the running desktop agent."
                     if direct else
                     "A manual inbox consumer is connected."
                     if manual else
                     "Open the desktop reasoning app and keep it running, then retry."),
        }

    async def start_session(self, *, project_id: str, job_id: str, prompt: str,
                            system: str = "", schema: dict | None = None,
                            mcp_config: dict | None = None,
                            allowed_tools: list | None = None,
                            workspace: str | None = None) -> AgentSession:
        sid = db.new_id("la_")
        mode = _reasoning_mode(prompt, mcp_config)
        selected_model = _desktop_model(mode)
        session = AgentSession(
            session_id=sid, external_id=sid, provider=self.name,
            model=selected_model,
            meta={"project_id": project_id, "job_id": job_id,
                  "workspace": workspace or str(config.ROOT),
                  "reasoning_mode": mode,
                  "desktop_model": selected_model})

        # Write to inbox so the Antigravity agent can pick it up
        inbox_id = db.insert("agent_inbox", {
            "project_id": project_id,
            "job_id": job_id,
            "prompt": prompt,
            "system_prompt": system or "",
            "schema_json": db.jdumps(schema) if schema else None,
            "status": "pending",
        })
        _prepare_file_request(
            inbox_id, project_id=project_id, job_id=job_id, prompt=prompt,
            system=system, schema=schema)
        session.meta["inbox_id"] = inbox_id
        self._sessions[sid] = session
        _signal_for(inbox_id)

        # Start a background thread that waits for the bridge to answer.
        q: queue.Queue = queue.Queue()
        self._queues[sid] = q
        self._cancel[sid] = False
        threading.Thread(target=self._wait_for_result,
                         args=(session, inbox_id, q, False), daemon=True).start()
        return session

    async def resume_session(self, session: AgentSession, prompt: str, *,
                             schema: dict | None = None) -> AgentSession:
        self._sessions.setdefault(session.session_id, session)
        meta = session.meta
        mode = _reasoning_mode(prompt)
        selected_model = _desktop_model(mode)
        meta["reasoning_mode"] = mode
        meta["desktop_model"] = selected_model
        session.model = selected_model
        inbox_id = db.insert("agent_inbox", {
            "project_id": meta.get("project_id", ""),
            "job_id": meta.get("job_id", ""),
            "prompt": prompt,
            "system_prompt": "",
            "schema_json": db.jdumps(schema) if schema else None,
            "status": "pending",
        })
        _prepare_file_request(
            inbox_id, project_id=str(meta.get("project_id") or ""),
            job_id=str(meta.get("job_id") or ""), prompt=prompt,
            system="", schema=schema)
        meta["inbox_id"] = inbox_id
        _signal_for(inbox_id)
        q: queue.Queue = queue.Queue()
        self._queues[session.session_id] = q
        self._cancel[session.session_id] = False
        threading.Thread(target=self._wait_for_result,
                         args=(session, inbox_id, q, True), daemon=True).start()
        return session

    async def submit_event(self, session: AgentSession, event: dict) -> None:
        await self.resume_session(
            session, event.get("prompt") or json.dumps(event, default=str))

    async def cancel(self, session: AgentSession) -> None:
        self._cancel[session.session_id] = True
        inbox_id = session.meta.get("inbox_id")
        if inbox_id:
            db.update("agent_inbox", inbox_id, {
                "status": "cancelled", "finished_at": db.now()})
            notify(inbox_id)

    def _dispatch(self, session: AgentSession, inbox_id: str,
                  resume: bool) -> str:
        runtime = _discover_runtime()
        if runtime is None:
            raise RuntimeError("The running desktop Agent API is unavailable")
        workspace = str(session.meta.get("workspace") or config.ROOT)
        if not Path(workspace).is_dir():
            workspace = str(config.ROOT)
        message = _callback_prompt(inbox_id)
        selected_model = str(session.meta.get("desktop_model") or
                             _desktop_model("deep"))

        def call(selected: dict) -> dict:
            return _agentapi_call(
                selected,
                ["new-conversation", "--model=" + selected_model,
                 "--title=" + ("VEDA follow-up" if resume else
                               "VEDA project reasoning"), message], cwd=workspace)

        payload = call(runtime)
        if payload.get("error"):
            runtime = _discover_runtime(force=True)
            if runtime is None:
                raise RuntimeError(str(payload.get("error"))[:500])
            payload = call(runtime)
        if payload.get("error"):
            raise RuntimeError(str(payload.get("error"))[:500])
        response = payload.get("response") or {}
        created = response.get("newConversation") or {}
        conversation_id = str(created.get("conversationId") or "").strip()
        if not conversation_id:
            raise RuntimeError("Desktop agent did not return a conversation id")
        session.external_id = conversation_id
        db.update("agent_inbox", inbox_id, {
            "status": "claimed", "claimed_at": db.now()})
        notify(inbox_id)
        return conversation_id

    def _wait_for_result(self, session: AgentSession, inbox_id: str,
                         q: queue.Queue, resume: bool) -> None:
        """Dispatch to the desktop agent, then wait for its local callback."""
        direct = False
        try:
            conversation_id = self._dispatch(session, inbox_id, resume)
            direct = True
            q.put(AgentEvent(
                "status", step="agent_started",
                label="Project context sent to reasoning service",
                data={"session_id": session.session_id,
                      "inbox_id": inbox_id,
                      "model": session.model,
                      "external_id": conversation_id}))
        except Exception as exc:  # noqa: BLE001
            if consumer_connected():
                q.put(AgentEvent(
                    "status", step="agent_started",
                    label="Project context queued for reasoning service",
                    data={"session_id": session.session_id,
                          "inbox_id": inbox_id, "model": session.model}))
            else:
                db.update("agent_inbox", inbox_id, {
                    "status": "timeout", "finished_at": db.now()})
                q.put(AgentEvent(
                    "error", label="Desktop reasoning dispatch failed: " +
                    str(exc)[:500]))
                _release_signal(inbox_id)
                q.put(None)
                return
        started = time.time()
        claim_deadline = started + (DIRECT_CLAIM_TIMEOUT if direct else CLAIM_TIMEOUT)
        result_deadline = started + INBOX_TIMEOUT
        claimed = False
        processing_announced = False
        wake = _signal_for(inbox_id)
        backoff = POLL_MIN
        try:
            while time.time() < result_deadline:
                if self._cancel.get(session.session_id):
                    q.put(AgentEvent("error", label="cancelled"))
                    break

                # Check first: a very fast agent can claim and post between polls.
                outbox = db.q1(
                    "SELECT * FROM agent_outbox WHERE inbox_id=? "
                    "ORDER BY created_at DESC LIMIT 1", [inbox_id])
                if not outbox:
                    file_payload = _file_result(inbox_id)
                    if file_payload is not None:
                        outbox = _persist_file_result(inbox_id, file_payload)
                if outbox:
                    error = outbox.get("error")
                    if error:
                        q.put(AgentEvent("error", label=error[:400]))
                    else:
                        result_text = outbox.get("result_json") or ""
                        # Replay any events the agent recorded
                        events_raw = db.jloads(outbox.get("events_json"), [])
                        for ev_dict in (events_raw or []):
                            q.put(AgentEvent(
                                kind=ev_dict.get("kind", "status"),
                                step=ev_dict.get("step", ""),
                                label=ev_dict.get("label", ""),
                                data=ev_dict.get("data", {})))
                        q.put(AgentEvent(
                            "result", step="agent_finished",
                            label="Agent finished",
                            data={"text": result_text,
                                  "structured": None,
                                  "is_error": False, "turns": 1,
                                  "cost_usd": 0.0,
                                  "external_id": session.external_id}))
                    db.update("agent_inbox", inbox_id, {
                        "status": "done", "finished_at": db.now()})
                    break

                inbox = db.q1("SELECT status FROM agent_inbox WHERE id=?",
                              [inbox_id])
                state = (inbox or {}).get("status")
                if state == "claimed":
                    claimed = True
                    if not processing_announced:
                        q.put(AgentEvent("status", step="agent_processing",
                                         label="Agent is processing..."))
                        processing_announced = True
                elif state in ("cancelled", "timeout"):
                    q.put(AgentEvent("error",
                                     label="Antigravity inbox was " + str(state)))
                    break

                # The important guard: an IDE being open is not proof that an
                # agent is actually consuming VEDA's inbox. Fail fast so the
                # deterministic fallback can finish this job and release the
                # next queued project.
                if not claimed and time.time() >= claim_deadline:
                    claim_limit = DIRECT_CLAIM_TIMEOUT if direct else CLAIM_TIMEOUT
                    q.put(AgentEvent(
                        "error",
                        label=("Reasoning service did not claim this job within "
                               + str(claim_limit) +
                               "s; releasing the VEDA worker.")))
                    db.update("agent_inbox", inbox_id, {
                        "status": "timeout", "finished_at": db.now()})
                    break

                # Sleep until the bridge signals, the backoff elapses, or the
                # relevant deadline arrives -- whichever comes first.
                deadline = claim_deadline if not claimed else result_deadline
                budget = max(0.0, min(deadline, result_deadline) - time.time())
                if wake.wait(timeout=min(backoff, budget) if budget else 0.0):
                    wake.clear()
                    backoff = POLL_MIN
                else:
                    backoff = min(POLL_MAX, backoff * POLL_GROWTH)
            else:
                q.put(AgentEvent(
                    "error",
                    label=("Timeout: Antigravity agent did not respond within " +
                           str(INBOX_TIMEOUT) + "s after the job was queued.")))
                db.update("agent_inbox", inbox_id, {
                    "status": "timeout", "finished_at": db.now()})
        except Exception as exc:  # noqa: BLE001
            q.put(AgentEvent("error",
                             label=type(exc).__name__ + ": " + str(exc)))
        finally:
            _cleanup_file_bridge(inbox_id)
            # Some desktop file tools finalize an atomic rename just after the
            # file first becomes readable.  A delayed second pass prevents a
            # completed response from being left in runtime storage.
            delayed_cleanup = threading.Timer(
                2.0, _cleanup_file_bridge, args=(inbox_id,))
            delayed_cleanup.daemon = True
            delayed_cleanup.start()
            _release_signal(inbox_id)
            q.put(None)

    async def stream_events(self, session: AgentSession) -> AsyncIterator[AgentEvent]:
        q = self._queues.get(session.session_id)
        if q is None:
            yield AgentEvent("error", label="session was never started")
            return
        loop = asyncio.get_event_loop()
        while True:
            item = await loop.run_in_executor(None, lambda: _get(q))
            if item is None:
                return
            if item is _EMPTY:
                continue
            yield item


_EMPTY = object()


def _get(q: queue.Queue):
    try:
        return q.get(timeout=1.0)
    except queue.Empty:
        return _EMPTY
