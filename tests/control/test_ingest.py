"""Unit tests for the Mars control-plane ingest + EventStore (Story 2.2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mars_control.api.routes import create_control_app
from mars_control.store.events import EventStore

SECRET = "control-plane-secret-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_started(session_id: str = "s-1") -> dict:
    return {
        "session_id": session_id,
        "type": "session_started",
        "timestamp": "2026-04-11T00:00:00+00:00",
        "model": "claude-opus-4-6",
        "cwd": "/workspace",
        "claude_code_version": "2.1.101",
        "tools_available": ["Bash"],
    }


def _assistant_text(session_id: str = "s-1", text: str = "hi") -> dict:
    return {
        "session_id": session_id,
        "type": "assistant_text",
        "timestamp": "2026-04-11T00:00:01+00:00",
        "text": text,
    }


def _assistant_chunk(session_id: str = "s-1", delta: str = "x") -> dict:
    return {
        "session_id": session_id,
        "type": "assistant_chunk",
        "timestamp": "2026-04-11T00:00:02+00:00",
        "delta": delta,
    }


def _tool_call(session_id: str = "s-1", tool_use_id: str = "tu-1") -> dict:
    return {
        "session_id": session_id,
        "type": "tool_call",
        "timestamp": "2026-04-11T00:00:03+00:00",
        "tool_use_id": tool_use_id,
        "tool_name": "Bash",
        "input": {"command": "echo hi"},
    }


@pytest.fixture
def store():
    s = EventStore(":memory:")
    s.init()
    yield s
    s.close()


@pytest.fixture
def client(store):
    app = create_control_app(store=store, event_secret=SECRET)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Secret validation
# ---------------------------------------------------------------------------


def test_ingest_rejects_missing_secret_header(client):
    resp = client.post("/internal/events", json={"events": []})
    assert resp.status_code == 401


def test_ingest_rejects_wrong_secret(client):
    resp = client.post(
        "/internal/events",
        json={"events": []},
        headers={"X-Event-Secret": "wrong-secret"},
    )
    assert resp.status_code == 401


def test_ingest_accepts_correct_secret(client):
    resp = client.post(
        "/internal/events",
        json={"events": []},
        headers={"X-Event-Secret": SECRET},
    )
    assert resp.status_code == 202
    assert resp.json() == {"received": 0, "persisted": 0}


def test_server_without_secret_configured_returns_500(store):
    """Misconfiguration must fail loudly — an empty server secret
    cannot silently accept forged events."""
    app = create_control_app(store=store, event_secret="")
    with TestClient(app) as c:
        resp = c.post(
            "/internal/events",
            json={"events": []},
            headers={"X-Event-Secret": "anything"},
        )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Persistence + ephemeral filtering
# ---------------------------------------------------------------------------


def test_ingest_persists_durable_events(client, store):
    batch = {"events": [_session_started(), _assistant_text(text="hello")]}
    resp = client.post(
        "/internal/events", json=batch, headers={"X-Event-Secret": SECRET}
    )
    assert resp.status_code == 202
    assert resp.json() == {"received": 2, "persisted": 2}

    rows = asyncio.run(store.get_session_events("s-1"))
    assert len(rows) == 2
    assert [r["type"] for r in rows] == ["session_started", "assistant_text"]
    assert rows[0]["data"]["model"] == "claude-opus-4-6"
    assert rows[1]["data"]["text"] == "hello"


def test_ingest_skips_ephemeral_events(client, store):
    """assistant_chunk / tool_started are ephemeral and must never
    reach the durable log — they flow through SSE fanout instead."""
    batch = {
        "events": [
            _session_started(),
            _assistant_chunk(delta="h"),
            _assistant_chunk(delta="i"),
            _tool_call(),
        ]
    }
    resp = client.post(
        "/internal/events", json=batch, headers={"X-Event-Secret": SECRET}
    )
    assert resp.status_code == 202
    assert resp.json() == {"received": 4, "persisted": 2}

    rows = asyncio.run(store.get_session_events("s-1"))
    assert len(rows) == 2
    types = [r["type"] for r in rows]
    assert "session_started" in types
    assert "tool_call" in types
    assert "assistant_chunk" not in types


def test_ingest_isolates_events_by_session_id(client, store):
    batch_a = {"events": [_session_started("sess-A")]}
    batch_b = {"events": [_session_started("sess-B")]}
    client.post(
        "/internal/events", json=batch_a, headers={"X-Event-Secret": SECRET}
    )
    client.post(
        "/internal/events", json=batch_b, headers={"X-Event-Secret": SECRET}
    )
    rows_a = asyncio.run(store.get_session_events("sess-A"))
    rows_b = asyncio.run(store.get_session_events("sess-B"))
    assert len(rows_a) == 1 and rows_a[0]["session_id"] == "sess-A"
    assert len(rows_b) == 1 and rows_b[0]["session_id"] == "sess-B"


def test_missing_events_key_returns_422(client):
    """`events` is required — tolerating a missing key would let a
    buggy producer silently drop data."""
    resp = client.post(
        "/internal/events",
        json={"not_the_events_key": []},
        headers={"X-Event-Secret": SECRET},
    )
    assert resp.status_code == 422


def test_wrong_type_for_events_field_returns_422(client):
    resp = client.post(
        "/internal/events",
        json={"events": "not-a-list"},
        headers={"X-Event-Secret": SECRET},
    )
    assert resp.status_code == 422


def test_oversized_batch_returns_422(client):
    """MAX_INGEST_BATCH_SIZE caps request memory."""
    from mars_control.events.ingest import MAX_INGEST_BATCH_SIZE

    huge = [_assistant_text(text=f"msg-{i}") for i in range(MAX_INGEST_BATCH_SIZE + 1)]
    resp = client.post(
        "/internal/events",
        json={"events": huge},
        headers={"X-Event-Secret": SECRET},
    )
    assert resp.status_code == 422


def test_forged_event_missing_required_fields_returns_422(client):
    """A forged session_started missing cwd/model/version must NOT be
    silently persisted. The ingest validates each event via
    MARS_EVENT_ADAPTER before writing."""
    bad = {
        "session_id": "s-1",
        "type": "session_started",
        "timestamp": "2026-04-11T00:00:00+00:00",
        # missing model, cwd, claude_code_version
    }
    resp = client.post(
        "/internal/events",
        json={"events": [bad]},
        headers={"X-Event-Secret": SECRET},
    )
    assert resp.status_code == 422


def test_forged_event_with_unknown_type_returns_422(client):
    """The discriminated union rejects unknown types."""
    bad = {
        "session_id": "s-1",
        "type": "super_sneaky",
        "timestamp": "2026-04-11T00:00:00+00:00",
    }
    resp = client.post(
        "/internal/events",
        json={"events": [bad]},
        headers={"X-Event-Secret": SECRET},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# EventStore unit tests (bypass HTTP)
# ---------------------------------------------------------------------------


def test_store_write_batch_filters_ephemerals():
    async def _run() -> int:
        s = EventStore(":memory:")
        s.init()
        try:
            return await s.write_batch(
                [
                    _session_started(),
                    _assistant_chunk(),
                    _assistant_chunk(),
                    _assistant_text(text="x"),
                ]
            )
        finally:
            s.close()

    persisted = asyncio.run(_run())
    assert persisted == 2


def test_store_get_session_events_orders_by_insertion():
    async def _run():
        s = EventStore(":memory:")
        s.init()
        try:
            await s.write_batch([_session_started()])
            await s.write_batch([_assistant_text(text="first")])
            await s.write_batch([_assistant_text(text="second")])
            return await s.get_session_events("s-1")
        finally:
            s.close()

    rows = asyncio.run(_run())
    texts = [r["data"].get("text") for r in rows if r["type"] == "assistant_text"]
    assert texts == ["first", "second"]


def test_store_since_id_cursor_replays_only_new_events():
    async def _run():
        s = EventStore(":memory:")
        s.init()
        try:
            await s.write_batch([_session_started()])
            await s.write_batch([_assistant_text(text="a")])
            mid = await s.get_session_events("s-1")
            last_id = mid[-1]["id"]
            await s.write_batch([_assistant_text(text="b")])
            return await s.get_session_events("s-1", since_id=last_id)
        finally:
            s.close()

    rows = asyncio.run(_run())
    assert len(rows) == 1
    assert rows[0]["data"]["text"] == "b"


def test_store_count_matches_insertions():
    async def _run():
        s = EventStore(":memory:")
        s.init()
        try:
            await s.write_batch([_session_started(), _assistant_text(text="x")])
            return await s.count()
        finally:
            s.close()

    assert asyncio.run(_run()) == 2


def test_store_init_and_close_are_idempotent():
    s = EventStore(":memory:")
    assert s.is_initialized is False
    s.init()
    assert s.is_initialized is True
    s.init()  # no-op
    s.close()
    assert s.is_initialized is False
    s.close()  # no-op


def test_store_file_backed_enables_wal_mode(tmp_path: Path):
    db_path = tmp_path / "mars-events.db"
    s = EventStore(db_path)
    s.init()
    try:
        cur = s._conn.execute("PRAGMA journal_mode")  # type: ignore[union-attr]
        mode = cur.fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        s.close()


def test_store_rejects_usage_before_init():
    s = EventStore(":memory:")
    with pytest.raises(RuntimeError, match="before init"):
        asyncio.run(s.get_session_events("s-1"))
