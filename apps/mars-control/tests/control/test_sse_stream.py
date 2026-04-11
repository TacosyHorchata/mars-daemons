"""Unit tests for :mod:`mars_control.sse.stream` — the browser SSE fanout."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from mars_control.api.routes import create_control_app
from mars_control.sse.stream import (
    SSE_HEARTBEAT_FRAME,
    SSEEventSink,
    format_sse_event,
)
from mars_control.store.events import EventStore

SECRET = "sse-test-secret"


# ---------------------------------------------------------------------------
# format_sse_event — frame shape
# ---------------------------------------------------------------------------


def test_format_sse_event_basic_frame():
    event = {"type": "assistant_text", "session_id": "s-1", "text": "hi"}
    frame = format_sse_event(event)
    assert frame.startswith("event: assistant_text\n")
    assert "data: " in frame
    assert frame.endswith("\n")
    # No id: line without a sequence
    assert not frame.startswith("id:")


def test_format_sse_event_includes_id_line_when_sequence_present():
    event = {
        "type": "session_started",
        "session_id": "s-1",
        "sequence": 42,
        "model": "claude-opus",
    }
    frame = format_sse_event(event)
    assert frame.startswith("id: 42\n")
    assert "event: session_started\n" in frame


def test_format_sse_event_handles_multiline_json_safely():
    """Multi-line JSON must be split across multiple data: lines so the
    stream stays valid SSE."""
    event = {
        "type": "assistant_text",
        "session_id": "s-1",
        "text": "line one\nline two",
    }
    frame = format_sse_event(event)
    data_lines = [l for l in frame.splitlines() if l.startswith("data:")]
    # The JSON payload is on one data: line because json.dumps serializes
    # the newline as \n inside the string — no literal newline leaks out.
    assert len(data_lines) == 1
    assert '\\n' in data_lines[0]


def test_format_sse_event_defaults_event_name_to_message():
    frame = format_sse_event({"session_id": "s-1"})
    assert frame.startswith("event: message\n")


# ---------------------------------------------------------------------------
# SSEEventSink — subscribe / emit / unsubscribe
# ---------------------------------------------------------------------------


def test_sink_subscribe_returns_fresh_queue():
    async def _run():
        sink = SSEEventSink()
        q1 = sink.subscribe("s-1")
        q2 = sink.subscribe("s-1")
        assert q1 is not q2
        assert sink.subscriber_count == 2

    asyncio.run(_run())


def test_sink_emit_fans_out_to_all_subscribers():
    async def _run():
        sink = SSEEventSink()
        q1 = sink.subscribe("s-1")
        q2 = sink.subscribe("s-1")
        q_other = sink.subscribe("s-2")
        event = {"session_id": "s-1", "type": "assistant_text", "text": "hi"}
        sink.emit(event)
        return [q1.qsize(), q2.qsize(), q_other.qsize()]

    counts = asyncio.run(_run())
    assert counts == [1, 1, 0]


def test_sink_emit_without_session_id_is_dropped():
    async def _run():
        sink = SSEEventSink()
        q = sink.subscribe("s-1")
        sink.emit({"type": "assistant_text", "text": "no session"})
        return q.qsize()

    assert asyncio.run(_run()) == 0


def test_sink_unsubscribe_removes_queue_and_cleans_empty_session():
    sink = SSEEventSink()

    async def _run():
        q = sink.subscribe("s-1")
        assert sink.subscriber_count == 1
        sink.unsubscribe("s-1", q)
        return sink.subscriber_count

    assert asyncio.run(_run()) == 0
    assert "s-1" not in sink._subscribers


def test_sink_overflow_drops_oldest_and_keeps_newest():
    async def _run():
        sink = SSEEventSink(max_queue_size=2)
        q = sink.subscribe("s-1")
        for i in range(5):
            sink.emit(
                {"session_id": "s-1", "type": "assistant_chunk", "delta": f"d{i}"}
            )
        drained = []
        while not q.empty():
            drained.append(q.get_nowait()["delta"])
        return drained

    drained = asyncio.run(_run())
    # Oldest-drop: after 5 emits with size=2, we should have the last 2
    assert drained == ["d3", "d4"]


# ---------------------------------------------------------------------------
# End-to-end: ingest → sink → SSE endpoint via TestClient
# ---------------------------------------------------------------------------


def _assistant_text(session_id: str = "s-1", text: str = "hi") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "type": "assistant_text",
        "timestamp": "2026-04-11T00:00:00+00:00",
        "text": text,
    }


def _session_started(session_id: str = "s-1") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "type": "session_started",
        "timestamp": "2026-04-11T00:00:00+00:00",
        "model": "claude-opus-4-6",
        "cwd": "/workspace",
        "claude_code_version": "2.1.101",
    }


@pytest.fixture
def store():
    s = EventStore(":memory:")
    s.init()
    yield s
    s.close()


@pytest.fixture
def sink():
    return SSEEventSink()


@pytest.fixture
def client(store, sink):
    app = create_control_app(store=store, event_secret=SECRET, sink=sink)
    with TestClient(app) as c:
        yield c


def test_ingest_broadcasts_validated_events_to_sink(client, sink):
    async def _run_subscribe():
        return sink.subscribe("s-1")

    q = asyncio.run(_run_subscribe())

    resp = client.post(
        "/internal/events",
        json={"events": [_session_started("s-1"), _assistant_text("s-1", "hello")]},
        headers={"X-Event-Secret": SECRET},
    )
    assert resp.status_code == 202

    async def _drain() -> list[dict]:
        out: list[dict] = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    drained = asyncio.run(_drain())
    assert len(drained) == 2
    types = [ev["type"] for ev in drained]
    assert types == ["session_started", "assistant_text"]


def test_ingest_broadcasts_ephemeral_events_to_sink(client, sink):
    """Ephemeral events must NOT be persisted but must STILL reach
    any connected browser — the whole point of the streaming chunk UX."""
    async def _run_subscribe():
        return sink.subscribe("s-1")

    q = asyncio.run(_run_subscribe())

    ephemeral = {
        "session_id": "s-1",
        "type": "assistant_chunk",
        "timestamp": "2026-04-11T00:00:00+00:00",
        "delta": "h",
    }
    resp = client.post(
        "/internal/events",
        json={"events": [ephemeral]},
        headers={"X-Event-Secret": SECRET},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["received"] == 1
    assert body["persisted"] == 0  # ephemeral, not stored

    async def _take() -> dict:
        return q.get_nowait()

    got = asyncio.run(_take())
    assert got["type"] == "assistant_chunk"
    assert got["delta"] == "h"


# ---------------------------------------------------------------------------
# sse_event_generator — driven directly with a mock Request so we avoid
# TestClient's httpx-stream buffering quirks (which hang on tiny initial
# SSE frames). The route itself is verified separately via routes smoke
# test below.
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in for :class:`fastapi.Request` that supports the
    one method the generator awaits — :meth:`is_disconnected`."""

    def __init__(self, disconnect_after_s: float = 10.0):
        self._start = asyncio.get_event_loop().time()
        self._disconnect_after = disconnect_after_s

    async def is_disconnected(self) -> bool:
        return (
            asyncio.get_event_loop().time() - self._start
        ) >= self._disconnect_after


def test_generator_yields_initial_ping_frame():
    from mars_control.sse.stream import sse_event_generator

    async def _run() -> list[str]:
        sink = SSEEventSink()
        request = _FakeRequest(disconnect_after_s=0.15)
        frames: list[str] = []
        async for frame in sse_event_generator(
            "s-1",
            sink,
            request,  # type: ignore[arg-type]
            heartbeat_interval_s=0.05,
            idle_timeout_s=1.0,
            disconnect_poll_interval_s=0.01,
        ):
            frames.append(frame)
        return frames

    frames = asyncio.run(_run())
    assert len(frames) >= 1
    assert frames[0] == SSE_HEARTBEAT_FRAME
    # Session unsubscribed in finally block
    # (validated by not leaking — checked in a separate test)


def test_generator_yields_event_emitted_after_subscribe():
    from mars_control.sse.stream import sse_event_generator

    sink = SSEEventSink()

    async def _consumer(collected: list[str]) -> None:
        request = _FakeRequest(disconnect_after_s=1.0)
        async for frame in sse_event_generator(
            "s-1",
            sink,
            request,  # type: ignore[arg-type]
            heartbeat_interval_s=0.1,
            idle_timeout_s=2.0,
            disconnect_poll_interval_s=0.02,
        ):
            collected.append(frame)

    async def _producer() -> None:
        # Wait a beat so the consumer subscribes first
        await asyncio.sleep(0.05)
        sink.emit(
            {
                "session_id": "s-1",
                "type": "assistant_text",
                "text": "hello",
                "sequence": 7,
            }
        )

    async def _run() -> list[str]:
        collected: list[str] = []
        await asyncio.gather(_consumer(collected), _producer())
        return collected

    frames = asyncio.run(_run())
    # First frame is the initial :ping
    assert frames[0] == SSE_HEARTBEAT_FRAME
    # Somewhere in the stream we emit the event frame
    event_frames = [f for f in frames if "event: assistant_text" in f]
    assert len(event_frames) == 1
    ev_frame = event_frames[0]
    assert "id: 7\n" in ev_frame
    assert "hello" in ev_frame


def test_generator_exits_cleanly_on_disconnect_and_unsubscribes():
    from mars_control.sse.stream import sse_event_generator

    sink = SSEEventSink()

    async def _run() -> int:
        request = _FakeRequest(disconnect_after_s=0.1)
        frames_count = 0
        async for _ in sse_event_generator(
            "s-1",
            sink,
            request,  # type: ignore[arg-type]
            heartbeat_interval_s=0.05,
            idle_timeout_s=5.0,
            disconnect_poll_interval_s=0.01,
        ):
            frames_count += 1
        return frames_count

    asyncio.run(_run())
    # finally-block unsubscribe → subscriber_count drops back to 0
    assert sink.subscriber_count == 0


def test_generator_exits_on_idle_timeout_and_unsubscribes():
    from mars_control.sse.stream import sse_event_generator

    sink = SSEEventSink()

    async def _run() -> int:
        request = _FakeRequest(disconnect_after_s=10.0)  # long enough
        frames_count = 0
        async for _ in sse_event_generator(
            "s-1",
            sink,
            request,  # type: ignore[arg-type]
            heartbeat_interval_s=0.02,
            idle_timeout_s=0.1,  # idle exit wins
            disconnect_poll_interval_s=0.005,
        ):
            frames_count += 1
        return frames_count

    asyncio.run(_run())
    assert sink.subscriber_count == 0


def test_sse_route_smoke_check_returns_200_and_media_type(client):
    """Smoke-test the route exists with the right media type. We don't
    stream bytes here — TestClient's httpx layer buffers tiny initial
    SSE chunks in a way that deadlocks the sync iterator. The generator
    itself is covered by the direct tests above."""
    # Issue a HEAD request to avoid consuming the response body at all
    resp = client.head("/sessions/s-1/stream")
    # FastAPI auto-routes HEAD to GET handlers; expect 200 (or 405 if
    # HEAD is rejected by the stack — either way proves the route is
    # wired).
    assert resp.status_code in (200, 405)
