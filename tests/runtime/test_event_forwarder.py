"""Unit tests for :class:`events.forwarder.HttpEventForwarder`.

Uses :class:`httpx.MockTransport` to intercept POSTs so the tests never
touch the network. Covers the batching, retry/backoff, and buffer
overflow behaviors required by Epic 2 Story 2.1.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from events.forwarder import (
    DEFAULT_BUFFER_LIMIT,
    HttpEventForwarder,
    _secret_fingerprint,
)
from events.types import (
    AssistantChunk,
    AssistantText,
    SessionEnded,
    SessionStarted,
    ToolCall,
    ToolResult,
)

URL = "https://mars-control.example/internal/events"
SECRET = "s3cret-seed-42"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient backed by a MockTransport."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def _started(session_id: str = "s-1") -> SessionStarted:
    return SessionStarted(
        session_id=session_id,
        model="claude-opus-4-6",
        cwd="/workspace",
        claude_code_version="2.1.101",
    )


def _chunk(session_id: str = "s-1", delta: str = "x") -> AssistantChunk:
    return AssistantChunk(session_id=session_id, delta=delta)


def _text(session_id: str = "s-1", text: str = "hi") -> AssistantText:
    return AssistantText(session_id=session_id, text=text)


# ---------------------------------------------------------------------------
# Secret fingerprinting
# ---------------------------------------------------------------------------


def test_secret_fingerprint_is_short_and_not_the_full_secret():
    fp = _secret_fingerprint(SECRET)
    assert fp.startswith("sha256:")
    # 16 hex chars = 64 bits of entropy, enough to avoid accidental
    # collisions when correlating key rotations in logs.
    assert len(fp) == len("sha256:") + 16
    assert SECRET not in fp


def test_secret_fingerprint_empty_secret():
    assert _secret_fingerprint("") == "<empty>"


# ---------------------------------------------------------------------------
# Happy path — batching + POST
# ---------------------------------------------------------------------------


def test_emit_and_flush_posts_batch_with_secret_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async def _run():
        async with HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            flush_interval_s=60.0,  # effectively disable auto-flush
        ) as fwd:
            for i in range(3):
                await fwd.emit(_text(text=f"msg-{i}"))
            await fwd.flush()

    asyncio.run(_run())

    assert len(seen) == 1
    req = seen[0]
    assert req.url == httpx.URL(URL)
    assert req.headers.get("X-Event-Secret") == SECRET
    body = json.loads(req.content)
    assert isinstance(body, dict)
    assert "events" in body and len(body["events"]) == 3
    for ev in body["events"]:
        assert ev["type"] == "assistant_text"


def test_max_batch_triggers_immediate_flush_without_waiting_for_interval():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    async def _run():
        async with HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            max_batch=5,
            flush_interval_s=60.0,
        ) as fwd:
            for i in range(5):
                await fwd.emit(_text(text=f"msg-{i}"))
            # Give the flush loop a tick to pick up the wake event
            await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert len(seen) >= 1
    total_events = sum(
        len(json.loads(r.content)["events"]) for r in seen
    )
    assert total_events == 5


def test_empty_flush_is_a_noop():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(204)

    async def _run():
        async with HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            flush_interval_s=60.0,
        ) as fwd:
            await fwd.flush()

    asyncio.run(_run())
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Overflow / drop policy
# ---------------------------------------------------------------------------


def test_buffer_full_drops_oldest_ephemeral_first():
    async def _run():
        # No handler needed — we only exercise emit() / the buffer
        fwd = HttpEventForwarder(
            url=URL,
            secret=SECRET,
            buffer_limit=3,
            flush_interval_s=60.0,
            client=_mock_client(lambda r: httpx.Response(204)),
        )
        # Two durables + one ephemeral, then add another durable
        await fwd.emit(_started())  # durable
        await fwd.emit(_chunk(delta="a"))  # ephemeral ← will be dropped
        await fwd.emit(_text(text="hi"))  # durable
        # Buffer now at 3 (limit). Adding another should drop the chunk.
        await fwd.emit(_text(text="another"))
        return fwd

    fwd = asyncio.run(_run())
    assert fwd.buffered == 3
    assert fwd.dropped_ephemeral_count == 1
    types = [type(ev).__name__ for ev in list(fwd._buffer)]
    assert "AssistantChunk" not in types
    assert types.count("AssistantText") == 2
    assert types.count("SessionStarted") == 1


def test_buffer_full_of_durables_logs_but_does_not_drop_durable():
    async def _run():
        fwd = HttpEventForwarder(
            url=URL,
            secret=SECRET,
            buffer_limit=2,
            flush_interval_s=60.0,
            client=_mock_client(lambda r: httpx.Response(204)),
        )
        await fwd.emit(_started())
        await fwd.emit(_text(text="a"))
        # Buffer full, no ephemerals to drop. Next emit grows past limit.
        await fwd.emit(_text(text="b"))
        return fwd

    fwd = asyncio.run(_run())
    assert fwd.buffered == 3  # limit was 2 but durable added anyway
    assert fwd.dropped_ephemeral_count == 0
    types = [type(ev).__name__ for ev in list(fwd._buffer)]
    assert types.count("SessionStarted") == 1
    assert types.count("AssistantText") == 2


# ---------------------------------------------------------------------------
# Retry / backoff on transport + 5xx
# ---------------------------------------------------------------------------


def test_transport_error_requeues_events_in_order():
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        raise httpx.ConnectError("control plane down")

    async def _run():
        fwd = HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            flush_interval_s=60.0,
            initial_backoff_s=0.0,  # avoid real sleep
            max_backoff_s=0.0,
        )
        await fwd.emit(_started())
        await fwd.emit(_text(text="a"))
        await fwd._flush_once()
        return fwd

    fwd = asyncio.run(_run())
    assert fwd.buffered == 2
    assert fwd.sent_count == 0
    assert fwd.failed_post_count == 1
    assert call_count["n"] == 1
    # Order preserved
    types = [type(ev).__name__ for ev in list(fwd._buffer)]
    assert types == ["SessionStarted", "AssistantText"]


def test_5xx_response_requeues_events():
    def handler(request):
        return httpx.Response(502, text="bad gateway")

    async def _run():
        fwd = HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            flush_interval_s=60.0,
            initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )
        await fwd.emit(_started())
        await fwd._flush_once()
        return fwd

    fwd = asyncio.run(_run())
    assert fwd.buffered == 1
    assert fwd.failed_post_count == 1


def test_4xx_response_drops_batch_and_logs_error(caplog):
    """4xx means bad request — retrying wastes buffer and masks a bug."""

    def handler(request):
        return httpx.Response(401, text="bad secret")

    async def _run():
        fwd = HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            flush_interval_s=60.0,
        )
        await fwd.emit(_started())
        with caplog.at_level("ERROR", logger="events.forwarder"):
            await fwd._flush_once()
        return fwd

    fwd = asyncio.run(_run())
    assert fwd.buffered == 0  # dropped
    assert fwd.failed_post_count == 1
    assert any("rejected with 401" in rec.message for rec in caplog.records)


def test_successful_post_resets_backoff():
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("transient")
        return httpx.Response(204)

    async def _run():
        fwd = HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            flush_interval_s=60.0,
            initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )
        await fwd.emit(_started())
        await fwd._flush_once()  # fails → backoff doubled
        await fwd.emit(_text(text="recovery"))
        await fwd._flush_once()  # succeeds
        return fwd

    fwd = asyncio.run(_run())
    assert fwd.buffered == 0
    assert fwd.sent_count == 2
    assert fwd.failed_post_count == 1


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_invalid_max_batch_rejected():
    with pytest.raises(ValueError):
        HttpEventForwarder(url=URL, secret=SECRET, max_batch=0)


def test_invalid_buffer_limit_rejected():
    with pytest.raises(ValueError):
        HttpEventForwarder(url=URL, secret=SECRET, buffer_limit=0)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


def test_stop_flushes_remaining_buffer():
    seen: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(204)

    async def _run():
        fwd = HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            flush_interval_s=60.0,
        )
        await fwd.start()
        await fwd.emit(_started())
        await fwd.emit(_text(text="bye"))
        await fwd.stop()

    asyncio.run(_run())
    total = sum(len(b["events"]) for b in seen)
    assert total == 2


def test_debug_state_exposes_metrics():
    fwd = HttpEventForwarder(url=URL, secret=SECRET)
    state = fwd.debug_state()
    assert state["url"] == URL
    assert state["secret_fingerprint"].startswith("sha256:")
    assert state["buffered"] == 0
    assert state["sent_count"] == 0
    assert state["failed_post_count"] == 0


# ---------------------------------------------------------------------------
# Concurrent flush lock (from codex review)
# ---------------------------------------------------------------------------


def test_concurrent_flushes_are_serialized_by_lock():
    """Two coroutines racing to flush the same forwarder must not
    reorder sends or duplicate a single batch."""
    seen: list[list[str]] = []

    def handler(request):
        body = json.loads(request.content)
        seen.append([ev["type"] for ev in body["events"]])
        return httpx.Response(204)

    async def _run():
        fwd = HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            flush_interval_s=60.0,
            max_batch=100,
        )
        await fwd.emit(_started())
        await fwd.emit(_text(text="a"))
        await asyncio.gather(fwd.flush(), fwd.flush())
        return fwd

    fwd = asyncio.run(_run())
    # Exactly one POST: the second flush() saw an empty buffer
    # because the first flush() had already drained it under the lock.
    assert len(seen) == 1
    assert seen[0] == ["session_started", "assistant_text"]
    assert fwd.sent_count == 2


def test_unexpected_exception_requeues_batch_instead_of_dropping():
    """Any error after the batch is popped must put the events back;
    dropping them silently is the main silent-failure vector."""
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("some unexpected transport-layer bug")
        return httpx.Response(204)

    async def _run():
        fwd = HttpEventForwarder(
            url=URL,
            secret=SECRET,
            client=_mock_client(handler),
            flush_interval_s=60.0,
            initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )
        await fwd.emit(_started())
        await fwd._flush_once()  # first call raises -> requeued
        assert fwd.buffered == 1
        assert fwd.sent_count == 0
        await fwd._flush_once()  # second call succeeds
        return fwd

    fwd = asyncio.run(_run())
    assert fwd.buffered == 0
    assert fwd.sent_count == 1
    assert fwd.failed_post_count == 1
