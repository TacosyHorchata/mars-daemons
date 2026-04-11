"""Unit tests for :mod:`supervisor` — the FastAPI control API.

Uses a stub ``spawn_fn`` that launches a tiny Python subprocess which
emits a canonical ``system.init`` event on startup, reads lines from
stdin, and echoes them back as assistant messages. This exercises the
real FastAPI + SessionManager + claude_code_stream code paths without
spending Claude Max quota.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from schema.agent import AgentConfig
from session.manager import SessionManager
from supervisor import create_app

# ---------------------------------------------------------------------------
# Stub subprocess — a Python script that imitates enough of claude's
# stream-json output for the supervisor to feel real.
# ---------------------------------------------------------------------------

_STUB_SCRIPT = r"""
import json, sys

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

# Session anchor
emit({
    "type": "system",
    "subtype": "init",
    "cwd": "/workspace",
    "session_id": "stub-runtime-1",
    "tools": ["Bash"],
    "model": "stub-model",
    "claude_code_version": "2.1.101",
})

# Echo user messages from stdin back as assistant text
for line in sys.stdin:
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get("type") != "user":
        continue
    content_blocks = ev.get("message", {}).get("content", [])
    user_text = "".join(
        b.get("text", "") for b in content_blocks if b.get("type") == "text"
    )
    emit({
        "type": "assistant",
        "message": {
            "id": "msg_stub_1",
            "content": [{"type": "text", "text": f"echo: {user_text}"}],
        },
    })
emit({
    "type": "result",
    "subtype": "success",
    "result": "ok",
    "stop_reason": "end_turn",
    "duration_ms": 1,
    "num_turns": 1,
    "total_cost_usd": 0.0,
    "permission_denials": [],
})
"""


async def _stub_spawn(config: AgentConfig, session_id: str):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        _STUB_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def _agent_payload(name: str = "stub-agent") -> dict:
    return {
        "name": name,
        "description": f"stub agent {name}",
        "runtime": "claude-code",
        "system_prompt_path": "CLAUDE.md",
        "tools": [],
        "env": [],
        "workdir": "/tmp",
    }


@pytest.fixture
def client():
    mgr = SessionManager(spawn_fn=_stub_spawn)
    app = create_app(manager=mgr)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Health + empty state
# ---------------------------------------------------------------------------


def test_health_returns_ok_with_zero_sessions(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "active_sessions": 0}


def test_list_sessions_empty(client):
    resp = client.get("/sessions")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": []}


# ---------------------------------------------------------------------------
# POST /sessions — spawn
# ---------------------------------------------------------------------------


def test_post_sessions_spawns_and_returns_handle(client):
    resp = client.post("/sessions", json=_agent_payload("pr-reviewer"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "pr-reviewer"
    assert body["description"] == "stub agent pr-reviewer"
    assert body["status"] == "running"
    assert body["is_alive"] is True
    assert body["session_id"].startswith("mars-")
    assert isinstance(body["pid"], int) and body["pid"] > 0

    # Appears in list
    list_resp = client.get("/sessions")
    sessions = list_resp.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == body["session_id"]

    client.delete(f"/sessions/{body['session_id']}")


def test_post_sessions_rejects_empty_body(client):
    resp = client.post("/sessions", content=b"")
    assert resp.status_code == 400


def test_post_sessions_rejects_non_object_json(client):
    resp = client.post("/sessions", json=[1, 2, 3])
    assert resp.status_code == 400


def test_post_sessions_rejects_invalid_agent_config(client):
    # Missing required fields
    resp = client.post("/sessions", json={"name": "x"})
    assert resp.status_code == 422


def test_post_sessions_rejects_malformed_json(client):
    resp = client.post(
        "/sessions",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_post_sessions_rejects_oversized_body(client):
    """Memory-DoS guard: the 64KB body cap must reject huge payloads
    before yaml.safe_load / json.loads ever touches them."""
    oversized = b'{"name": "' + b"x" * (128 * 1024) + b'"}'
    resp = client.post(
        "/sessions",
        content=oversized,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413


def test_post_sessions_accepts_yaml_payload(client):
    yaml_body = (
        "name: yaml-agent\n"
        "description: yaml stub\n"
        "runtime: claude-code\n"
        "system_prompt_path: CLAUDE.md\n"
        "workdir: /tmp\n"
    )
    resp = client.post(
        "/sessions",
        content=yaml_body.encode(),
        headers={"content-type": "application/x-yaml"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "yaml-agent"
    client.delete(f"/sessions/{body['session_id']}")


# ---------------------------------------------------------------------------
# GET /sessions/{id} — fetch
# ---------------------------------------------------------------------------


def test_get_session_returns_404_for_unknown(client):
    resp = client.get("/sessions/mars-does-not-exist")
    assert resp.status_code == 404


def test_get_session_returns_serialized_handle(client):
    created = client.post("/sessions", json=_agent_payload()).json()
    sid = created["session_id"]
    resp = client.get(f"/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["session_id"] == sid
    client.delete(f"/sessions/{sid}")


# ---------------------------------------------------------------------------
# DELETE /sessions/{id} — kill
# ---------------------------------------------------------------------------


def test_delete_session_kills_and_unregisters(client):
    created = client.post("/sessions", json=_agent_payload()).json()
    sid = created["session_id"]
    resp = client.delete(f"/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json() == {"session_id": sid, "killed": True}

    # No longer in list
    assert client.get("/sessions").json() == {"sessions": []}
    # 404 on subsequent fetch
    assert client.get(f"/sessions/{sid}").status_code == 404


def test_delete_unknown_session_returns_404(client):
    assert client.delete("/sessions/mars-does-not-exist").status_code == 404


# ---------------------------------------------------------------------------
# POST /sessions/{id}/input — stream-json stdin injection
# ---------------------------------------------------------------------------


def _wait_for_events(
    client, sid: str, expected_types: list[str], timeout_s: float = 3.0
) -> list[dict]:
    """Poll /events until every expected type has been observed, or
    timeout. Returns the full accumulated event list so callers can
    inspect payloads — ``GET /sessions/{id}/events`` drains the queue,
    so callers must rely on this accumulator rather than re-querying."""
    collected: list[dict] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        batch = client.get(f"/sessions/{sid}/events").json()["events"]
        collected.extend(batch)
        seen_types = {ev["type"] for ev in collected}
        if all(t in seen_types for t in expected_types):
            return collected
        time.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for {expected_types}; saw {[ev['type'] for ev in collected]}"
    )


def test_post_input_writes_stream_json_event_to_stdin(client):
    created = client.post("/sessions", json=_agent_payload()).json()
    sid = created["session_id"]

    # Wait for the stub to emit session_started before injecting input
    _wait_for_events(client, sid, ["session_started"])

    resp = client.post(f"/sessions/{sid}/input", json={"text": "hello mars"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"session_id": sid, "accepted": True}

    # Stub echoes stdin user messages back as assistant_text events
    events = _wait_for_events(client, sid, ["assistant_text"])
    text_events = [ev for ev in events if ev["type"] == "assistant_text"]
    assert any("hello mars" in ev.get("text", "") for ev in text_events), text_events

    client.delete(f"/sessions/{sid}")


def test_post_input_rejects_empty_text(client):
    created = client.post("/sessions", json=_agent_payload()).json()
    sid = created["session_id"]
    resp = client.post(f"/sessions/{sid}/input", json={"text": ""})
    assert resp.status_code == 422
    client.delete(f"/sessions/{sid}")


def test_post_input_on_unknown_session_returns_404(client):
    resp = client.post("/sessions/mars-missing/input", json={"text": "x"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /sessions/{id}/events — drain queue
# ---------------------------------------------------------------------------


def test_get_events_returns_parsed_events(client):
    created = client.post("/sessions", json=_agent_payload()).json()
    sid = created["session_id"]

    events = _wait_for_events(client, sid, ["session_started"])
    session_started = next(ev for ev in events if ev["type"] == "session_started")
    assert session_started["claude_code_version"] == "2.1.101"
    assert session_started["session_id"] == sid
    assert session_started["cwd"] == "/workspace"

    client.delete(f"/sessions/{sid}")


def test_get_events_for_unknown_session_returns_404(client):
    assert client.get("/sessions/mars-missing/events").status_code == 404


# ---------------------------------------------------------------------------
# POST /sessions/{id}/permission-response — deferred to v1.1
# ---------------------------------------------------------------------------


def test_permission_response_returns_501(client):
    created = client.post("/sessions", json=_agent_payload()).json()
    sid = created["session_id"]
    resp = client.post(
        f"/sessions/{sid}/permission-response",
        json={"tool_use_id": "tu-1", "approved": True},
    )
    assert resp.status_code == 501
    client.delete(f"/sessions/{sid}")


# ---------------------------------------------------------------------------
# Pump-triggered kill on stdout EOF (from codex review)
# ---------------------------------------------------------------------------


# A stub that emits the canonical events and exits immediately, never
# reading stdin. The pump should see EOF and schedule mgr.kill().
_EOF_STUB_SCRIPT = r"""
import json, sys
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
emit({
    "type": "system", "subtype": "init",
    "cwd": "/workspace", "session_id": "eof-stub",
    "tools": [], "model": "stub", "claude_code_version": "2.1.101",
})
emit({
    "type": "result", "subtype": "success",
    "result": "done", "stop_reason": "end_turn",
    "duration_ms": 1, "num_turns": 0,
    "total_cost_usd": 0.0, "permission_denials": [],
})
"""


async def _eof_stub_spawn(config: AgentConfig, session_id: str):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        _EOF_STUB_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def test_pump_triggers_kill_when_stdout_hits_eof():
    """When the subprocess finishes naturally, the pump's finally
    block should schedule mgr.kill, transitioning the session out of
    'running' state without an explicit DELETE."""
    mgr = SessionManager(spawn_fn=_eof_stub_spawn)
    app = create_app(manager=mgr)
    with TestClient(app) as c:
        created = c.post("/sessions", json=_agent_payload("eof-test")).json()
        sid = created["session_id"]

        # Poll: session should leave the manager's active list once the
        # pump's finally-kill fires.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            listed = c.get("/sessions").json()["sessions"]
            if not any(s["session_id"] == sid for s in listed):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "pump did not transition session out of running state within 3s"
            )

        # Subsequent GET returns 404 — session is gone from the manager.
        assert c.get(f"/sessions/{sid}").status_code == 404
