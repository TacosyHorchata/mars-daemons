"""End-to-end integration test for the Mars event pipeline.

Connects the mars-runtime forwarder to the mars-control app in a single
process via :class:`httpx.ASGITransport`, exercising the full path:

    HttpEventForwarder
        → httpx POST /internal/events
            → create_control_app ingest handler
                → MARS_EVENT_ADAPTER validation
                → EventStore.write_batch (durable persist)
                → SSEEventSink.emit (in-process fanout)

The ASGI transport keeps the test in one process — no real sockets,
no uvicorn, no test-server management. Lifespan is skipped because
the store is explicitly injected (already initialized) and no
production init work needs to run.

Covers Epic 2 Story 2.4's two required scenarios:

1. **Full round-trip** — forwarder emits a canonical session sequence,
   events land in the store (durables only) AND reach a subscribed
   sink queue (all events).
2. **Control-plane restart** — a file-backed store is written to,
   closed, reopened, and the durable events survive. This proves the
   SQLite WAL + commit semantics are correct and simulates a Fly
   machine restart of the control plane.

Out of scope here (covered elsewhere):

* Control-plane outage + runtime buffering + recovery — the retry /
  re-queue behavior is unit-tested in
  ``tests/runtime/test_event_forwarder.py`` via ``httpx.MockTransport``.
* Real browser SSE streaming through TestClient — the fanout
  generator is unit-tested directly in
  ``tests/control/test_sse_stream.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from events.forwarder import HttpEventForwarder
from events.types import (
    AssistantChunk,
    AssistantText,
    SessionEnded,
    SessionStarted,
    ToolCall,
    ToolResult,
)
from mars_control.api.routes import create_control_app
from mars_control.sse.stream import SSEEventSink
from mars_control.store.events import EventStore

SECRET = "integration-test-secret"
SESSION_ID = "s-e2e-1"


def _make_canonical_session() -> list:
    """The canonical Mars event sequence emitted by one daemon turn."""
    return [
        SessionStarted(
            session_id=SESSION_ID,
            model="claude-opus-4-6",
            cwd="/workspace",
            claude_code_version="2.1.101",
            tools_available=["Bash"],
        ),
        ToolCall(
            session_id=SESSION_ID,
            tool_use_id="tu-1",
            tool_name="Bash",
            input={"command": "echo hi"},
            message_id="msg_1",
            block_index=0,
        ),
        ToolResult(
            session_id=SESSION_ID,
            tool_use_id="tu-1",
            content="hi\n",
            is_error=False,
            message_id="msg_2",
            block_index=0,
        ),
        AssistantChunk(session_id=SESSION_ID, delta="It "),  # ephemeral
        AssistantChunk(session_id=SESSION_ID, delta="printed hi."),  # ephemeral
        AssistantText(
            session_id=SESSION_ID,
            text="It printed hi.",
            message_id="msg_3",
            block_index=0,
        ),
        SessionEnded(
            session_id=SESSION_ID,
            result="It printed hi.",
            stop_reason="end_turn",
            duration_ms=1234,
            num_turns=1,
            total_cost_usd=0.01,
            permission_denials=[],
        ),
    ]


def _build_app_and_forwarder(
    store: EventStore, sink: SSEEventSink
) -> tuple[object, HttpEventForwarder]:
    """Build the control-plane app and wire the forwarder to it via
    :class:`httpx.ASGITransport`. Returns ``(app, forwarder)``."""
    app = create_control_app(store=store, event_secret=SECRET, sink=sink)
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://mars-control")
    forwarder = HttpEventForwarder(
        url="http://mars-control/internal/events",
        secret=SECRET,
        client=http,
        flush_interval_s=0.05,
        max_batch=50,
    )
    return app, forwarder


# ---------------------------------------------------------------------------
# Scenario 1 — full round-trip
# ---------------------------------------------------------------------------


def test_end_to_end_forwarder_to_store_and_sink():
    """Full pipeline: runtime forwarder → control plane ingest → store
    + sink, in a single process via ASGITransport."""

    async def _run() -> tuple[list[str], list[str]]:
        store = EventStore(":memory:")
        store.init()
        sink = SSEEventSink()

        # Browser equivalent — a subscribed queue reading from the sink
        subscriber = sink.subscribe(SESSION_ID)

        app, fwd = _build_app_and_forwarder(store, sink)
        try:
            await fwd.start()
            for ev in _make_canonical_session():
                await fwd.emit(ev)
            await fwd.flush()
            # Give the flush loop a tick in case any residuals remain
            await asyncio.sleep(0.05)
            await fwd.flush()
        finally:
            await fwd.stop()

        # Drain the sink (browser view — every event durable + ephemeral)
        sink_types: list[str] = []
        while not subscriber.empty():
            sink_types.append(subscriber.get_nowait()["type"])

        # Query the store (durable-only persistence)
        persisted = await store.get_session_events(SESSION_ID)
        store_types = [r["type"] for r in persisted]

        store.close()
        return store_types, sink_types

    store_types, sink_types = asyncio.run(_run())

    # Store holds only durable events, in forwarder order
    assert store_types == [
        "session_started",
        "tool_call",
        "tool_result",
        "assistant_text",
        "session_ended",
    ]

    # Sink fans out everything — durable + ephemeral chunks
    assert "session_started" in sink_types
    assert "assistant_chunk" in sink_types
    assert sink_types.count("assistant_chunk") == 2
    assert sink_types[-1] == "session_ended"


def test_end_to_end_persists_expected_payload_fields():
    """Spot-check that the store round-trips enough of each event
    payload for replay to be meaningful."""

    async def _run() -> list[dict]:
        store = EventStore(":memory:")
        store.init()
        sink = SSEEventSink()
        app, fwd = _build_app_and_forwarder(store, sink)
        try:
            await fwd.start()
            for ev in _make_canonical_session():
                await fwd.emit(ev)
            await fwd.flush()
            await asyncio.sleep(0.05)
        finally:
            await fwd.stop()
        rows = await store.get_session_events(SESSION_ID)
        store.close()
        return rows

    rows = asyncio.run(_run())
    started = next(r for r in rows if r["type"] == "session_started")
    assert started["data"]["claude_code_version"] == "2.1.101"
    assert started["data"]["cwd"] == "/workspace"

    call = next(r for r in rows if r["type"] == "tool_call")
    assert call["data"]["tool_name"] == "Bash"
    assert call["data"]["input"] == {"command": "echo hi"}
    assert call["data"]["message_id"] == "msg_1"

    result = next(r for r in rows if r["type"] == "tool_result")
    assert result["data"]["tool_use_id"] == call["data"]["tool_use_id"]
    assert result["data"]["is_error"] is False
    assert "hi" in result["data"]["content"]

    text = next(r for r in rows if r["type"] == "assistant_text")
    assert text["data"]["text"] == "It printed hi."

    ended = next(r for r in rows if r["type"] == "session_ended")
    assert ended["data"]["stop_reason"] == "end_turn"
    assert ended["data"]["num_turns"] == 1


# ---------------------------------------------------------------------------
# Scenario 2 — control-plane restart preserves durable events
# ---------------------------------------------------------------------------


def test_control_plane_restart_preserves_durable_events(tmp_path: Path):
    """The epic calls out ``Kill control plane → ... → verify no event
    loss on durables``. We simulate a restart by closing the store
    after the first batch, reopening it, and confirming the rows
    survive the cycle."""

    db_path = tmp_path / "mars-control.db"

    async def _phase_one() -> int:
        store = EventStore(db_path)
        store.init()
        sink = SSEEventSink()
        app, fwd = _build_app_and_forwarder(store, sink)
        try:
            await fwd.start()
            for ev in _make_canonical_session():
                await fwd.emit(ev)
            await fwd.flush()
            await asyncio.sleep(0.05)
        finally:
            await fwd.stop()
        count = await store.count()
        store.close()  # "kill" the control plane
        return count

    count_before_restart = asyncio.run(_phase_one())
    # 5 durables (session_started, tool_call, tool_result, assistant_text,
    # session_ended); 2 ephemeral chunks were dropped at the store layer.
    assert count_before_restart == 5

    async def _phase_two() -> list[dict]:
        # Reopen the same file — "restart" of the control plane process
        store = EventStore(db_path)
        store.init()
        try:
            return await store.get_session_events(SESSION_ID)
        finally:
            store.close()

    rows = asyncio.run(_phase_two())
    assert len(rows) == 5
    assert [r["type"] for r in rows] == [
        "session_started",
        "tool_call",
        "tool_result",
        "assistant_text",
        "session_ended",
    ]


def test_control_plane_restart_supports_since_id_resume(tmp_path: Path):
    """A browser that reconnects after a control-plane restart can
    pick up only the new durable events by passing its ``since_id``
    cursor to the store."""

    db_path = tmp_path / "resume.db"

    async def _run() -> list[str]:
        # Phase 1: write the first half
        store = EventStore(db_path)
        store.init()
        app, fwd = _build_app_and_forwarder(store, SSEEventSink())
        try:
            await fwd.start()
            await fwd.emit(_make_canonical_session()[0])  # session_started
            await fwd.emit(_make_canonical_session()[1])  # tool_call
            await fwd.flush()
            await asyncio.sleep(0.05)
        finally:
            await fwd.stop()
        mid = await store.get_session_events(SESSION_ID)
        cursor = mid[-1]["id"]
        store.close()

        # Phase 2: "restart" + write the second half
        store = EventStore(db_path)
        store.init()
        app, fwd = _build_app_and_forwarder(store, SSEEventSink())
        try:
            await fwd.start()
            await fwd.emit(_make_canonical_session()[2])  # tool_result
            await fwd.emit(_make_canonical_session()[5])  # assistant_text
            await fwd.emit(_make_canonical_session()[6])  # session_ended
            await fwd.flush()
            await asyncio.sleep(0.05)
        finally:
            await fwd.stop()

        try:
            new_rows = await store.get_session_events(
                SESSION_ID, since_id=cursor
            )
        finally:
            store.close()
        return [r["type"] for r in new_rows]

    new_types = asyncio.run(_run())
    assert new_types == ["tool_result", "assistant_text", "session_ended"]
