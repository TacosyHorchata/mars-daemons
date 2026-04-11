"""Unit tests for the local-Fly-emulation session proxy.

``create_control_app`` exposes two endpoints that proxy to a single
default supervisor (pointed at by ``default_supervisor_url`` or the
``MARS_DEFAULT_SUPERVISOR_URL`` env var):

* ``GET  /sessions``                         → supervisor ``/sessions``
* ``POST /sessions/{session_id}/input``      → supervisor ``/sessions/{id}/input``

These are the hooks the browser dashboard + chat view call when no
session registry exists yet (Epic 5). The tests use httpx MockTransport
to intercept the outbound call and assert on the forwarded request.
"""

from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from mars_control.api.routes import create_control_app
from mars_control.sse.stream import SSEEventSink
from mars_control.store.events import EventStore

SUPERVISOR = "http://runtime-local:8090"
SECRET = "test-event-secret"


def _make_app(
    *,
    handler: Callable[[httpx.Request], httpx.Response] | None,
    default_supervisor_url: str | None = SUPERVISOR,
    cors_allow_origins: list[str] | None = None,
) -> tuple[TestClient, list[dict]]:
    captured: list[dict] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "url": str(request.url),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        assert handler is not None
        return handler(request)

    http = (
        httpx.AsyncClient(transport=httpx.MockTransport(_wrapped))
        if handler is not None
        else httpx.AsyncClient()
    )

    store = EventStore(":memory:")
    store.init()
    app = create_control_app(
        store=store,
        event_secret=SECRET,
        sink=SSEEventSink(),
        http_client=http,
        default_supervisor_url=default_supervisor_url,
        cors_allow_origins=cors_allow_origins,
    )
    return TestClient(app), captured


# ---------------------------------------------------------------------------
# GET /sessions
# ---------------------------------------------------------------------------


def test_list_sessions_proxies_to_default_supervisor() -> None:
    payload = {
        "sessions": [
            {
                "session_id": "s-001",
                "agent_name": "orion-ops",
                "status": "running",
                "description": "local",
            }
        ]
    }
    client, captured = _make_app(
        handler=lambda _req: httpx.Response(200, json=payload),
    )
    resp = client.get("/sessions")
    assert resp.status_code == 200
    assert resp.json() == payload
    assert len(captured) == 1
    assert captured[0]["method"] == "GET"
    assert captured[0]["url"] == f"{SUPERVISOR}/sessions"


def test_list_sessions_503_when_no_default_supervisor() -> None:
    client, _ = _make_app(handler=None, default_supervisor_url="")
    resp = client.get("/sessions")
    assert resp.status_code == 503
    body = resp.json()
    assert "MARS_DEFAULT_SUPERVISOR_URL" in body["detail"]


def test_list_sessions_502_on_supervisor_network_error() -> None:
    def _boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client, captured = _make_app(handler=_boom)
    resp = client.get("/sessions")
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["detail"]
    assert captured and captured[0]["method"] == "GET"


def test_list_sessions_502_on_supervisor_4xx() -> None:
    client, _ = _make_app(
        handler=lambda _req: httpx.Response(500, text="runtime boom"),
    )
    resp = client.get("/sessions")
    assert resp.status_code == 502
    assert "500" in resp.json()["detail"]
    assert "runtime boom" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /sessions/{id}/input
# ---------------------------------------------------------------------------


def test_session_input_proxies_text_payload() -> None:
    client, captured = _make_app(
        handler=lambda _req: httpx.Response(
            200, json={"session_id": "s-42", "accepted": True}
        )
    )
    resp = client.post("/sessions/s-42/input", json={"text": "hello daemon"})
    assert resp.status_code == 200
    assert resp.json() == {"session_id": "s-42", "accepted": True}
    assert len(captured) == 1
    assert captured[0]["method"] == "POST"
    assert captured[0]["url"] == f"{SUPERVISOR}/sessions/s-42/input"
    assert captured[0]["body"] == {"text": "hello daemon"}


def test_session_input_requires_non_empty_text() -> None:
    client, _ = _make_app(handler=None, default_supervisor_url=None)
    # 503 short-circuit is fine — we just want to confirm the validator fires first
    resp = client.post("/sessions/s-1/input", json={"text": ""})
    assert resp.status_code == 422


def test_session_input_503_when_no_default_supervisor() -> None:
    client, _ = _make_app(handler=None, default_supervisor_url=None)
    resp = client.post("/sessions/s-1/input", json={"text": "hi"})
    assert resp.status_code == 503


def test_session_input_propagates_supervisor_error_status() -> None:
    client, _ = _make_app(
        handler=lambda _req: httpx.Response(
            410, text="session stdin closed"
        )
    )
    resp = client.post("/sessions/s-1/input", json={"text": "hi"})
    # Supervisor's 410 comes through as-is (unlike /sessions which masks
    # with 502) — this is the right call because 410 is a meaningful
    # signal to the browser: the daemon exited, stop trying to chat.
    assert resp.status_code == 410


# ---------------------------------------------------------------------------
# CORS wiring
# ---------------------------------------------------------------------------


def test_cors_allow_origins_middleware_allows_frontend() -> None:
    client, _ = _make_app(
        handler=lambda _req: httpx.Response(200, json={"sessions": []}),
        cors_allow_origins=["http://localhost:3000"],
    )
    resp = client.get(
        "/sessions",
        headers={"Origin": "http://localhost:3000"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert resp.headers.get("access-control-allow-credentials") == "true"


def test_cors_not_added_when_no_origins_configured() -> None:
    client, _ = _make_app(
        handler=lambda _req: httpx.Response(200, json={"sessions": []}),
        cors_allow_origins=None,
    )
    resp = client.get("/sessions", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 200
    # No middleware means no CORS header echoed back
    assert resp.headers.get("access-control-allow-origin") is None
