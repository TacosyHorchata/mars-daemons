"""Unit tests for ``PATCH /agents/{name}/prompt`` (Story 6.4).

The endpoint forwards the admin-edit payload to the supervisor that
hosts the target session. Tests inject a ``session_locator`` and a
``httpx.AsyncClient`` backed by MockTransport so we never touch the
network.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mars_control.api.routes import create_control_app
from mars_control.sse.stream import SSEEventSink
from mars_control.store.events import EventStore

SECRET = "prompt-update-secret"


def _make_app(
    *,
    handler,
    locator=None,
) -> tuple[TestClient, list[dict]]:
    """Build a control-plane app wired to a MockTransport-backed
    httpx client. ``handler`` intercepts forwarded supervisor calls."""
    captured: list[dict] = []

    def _wrapped_handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "url": str(request.url),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(_wrapped_handler))

    store = EventStore(":memory:")
    store.init()
    app = create_control_app(
        store=store,
        event_secret=SECRET,
        sink=SSEEventSink(),
        session_locator=locator,
        http_client=http,
    )
    client = TestClient(app)
    return client, captured


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_patch_prompt_forwards_to_supervisor():
    def _handler(request):
        return httpx.Response(
            200,
            json={
                "session_id": "mars-sess-1",
                "status": "running",
                "pid": 12345,
                "prompt_bytes_written": 42,
            },
        )

    locator = (
        lambda agent, sid: "http://10.0.0.1:8080"
        if agent == "pr-reviewer"
        else None
    )
    client, captured = _make_app(handler=_handler, locator=locator)

    with client:
        resp = client.patch(
            "/agents/pr-reviewer/prompt",
            json={
                "session_id": "mars-sess-1",
                "content": "You are Pedro's PR reviewer. Be thorough.",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_name"] == "pr-reviewer"
    assert body["session_id"] == "mars-sess-1"
    assert body["supervisor"] == "http://10.0.0.1:8080"
    assert body["result"]["status"] == "running"

    # Exactly one forwarded call to the supervisor's reload-prompt endpoint
    assert len(captured) == 1
    call = captured[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://10.0.0.1:8080/sessions/mars-sess-1/reload-prompt"
    assert call["body"]["content"].startswith("You are Pedro's PR reviewer")


def test_patch_prompt_strips_trailing_slash_on_supervisor_url():
    def _handler(request):
        return httpx.Response(200, json={})

    locator = lambda *_: "http://10.0.0.1:8080/"
    client, captured = _make_app(handler=_handler, locator=locator)

    with client:
        client.patch(
            "/agents/x/prompt",
            json={"session_id": "s-1", "content": "hi"},
        )
    # No double slash
    assert "//sessions" not in captured[0]["url"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_patch_prompt_404_when_locator_returns_none():
    client, _captured = _make_app(
        handler=lambda r: httpx.Response(200, json={}),
        locator=lambda *_: None,
    )
    with client:
        resp = client.patch(
            "/agents/ghost/prompt",
            json={"session_id": "s-1", "content": "hi"},
        )
    assert resp.status_code == 404
    assert "no supervisor registered" in resp.json()["detail"]


def test_patch_prompt_502_when_supervisor_unreachable():
    def _handler(request):
        raise httpx.ConnectError("supervisor down")

    client, _ = _make_app(
        handler=_handler, locator=lambda *_: "http://10.0.0.1:8080"
    )
    with client:
        resp = client.patch(
            "/agents/x/prompt",
            json={"session_id": "s-1", "content": "hi"},
        )
    assert resp.status_code == 502
    assert "supervisor unreachable" in resp.json()["detail"]


def test_patch_prompt_502_when_supervisor_returns_5xx():
    def _handler(request):
        return httpx.Response(500, text="oops")

    client, _ = _make_app(
        handler=_handler, locator=lambda *_: "http://10.0.0.1:8080"
    )
    with client:
        resp = client.patch(
            "/agents/x/prompt",
            json={"session_id": "s-1", "content": "hi"},
        )
    assert resp.status_code == 502
    assert "500" in resp.json()["detail"]


def test_patch_prompt_rejects_empty_content():
    client, _ = _make_app(
        handler=lambda r: httpx.Response(200, json={}),
        locator=lambda *_: "http://10.0.0.1:8080",
    )
    with client:
        resp = client.patch(
            "/agents/x/prompt",
            json={"session_id": "s-1", "content": ""},
        )
    assert resp.status_code == 422


def test_patch_prompt_rejects_missing_session_id():
    client, _ = _make_app(
        handler=lambda r: httpx.Response(200, json={}),
        locator=lambda *_: "http://10.0.0.1:8080",
    )
    with client:
        resp = client.patch(
            "/agents/x/prompt",
            json={"content": "hi"},
        )
    assert resp.status_code == 422
