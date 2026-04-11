"""Browser SSE fan-out for the Mars control plane.

Lifts Camtom's in-process ``SSEEventSink`` pattern and the shared
``_format_sse_event`` / heartbeat helpers from
``services/fastapi/src/products/agents/agent/sink.py:85-140`` and
``services/fastapi/src/products/agents/router.py:322-329, 52-53, 999-1128``.

Architecture
------------

The machine forwarder (Epic 2 Story 2.1) POSTs events to
``/internal/events``. The ingest handler validates + persists the
durable subset and *broadcasts every validated event* to an in-process
:class:`SSEEventSink`, which fans them out to every browser currently
subscribed on ``GET /sessions/{id}/stream``.

Single SSE hop, not two. Machines are ephemeral producers, the
control plane is the durable sink + fanout point.

Important trade-offs
--------------------

* One process, one sink. Multi-node control plane is v2 — at that
  point swap the sink for a Redis-backed one.
* Per-subscriber bounded :class:`asyncio.Queue`. On overflow we drop
  the subscriber's oldest queued event rather than block the ingest
  producer. Durable events survive on disk via the store and can be
  re-fetched through a dedicated history endpoint — the SSE edge
  accepts loss so one slow browser cannot stall the pipeline.
* The heartbeat / idle-timeout / disconnect-poll constants match
  Camtom verbatim so operators who know Camtom SSE get the same
  intuitions here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import Request

__all__ = [
    "SSE_DISCONNECT_POLL_INTERVAL_S",
    "SSE_HEARTBEAT_FRAME",
    "SSE_HEARTBEAT_INTERVAL_S",
    "SSE_IDLE_TIMEOUT_S",
    "SSEEventSink",
    "format_sse_event",
    "sse_event_generator",
]

_log = logging.getLogger("mars.control.sse")

#: 30s between ``:ping`` comments. Matches Camtom's ``_SSE_HEARTBEAT_INTERVAL``.
SSE_HEARTBEAT_INTERVAL_S: float = 30.0

#: 5 minutes of no events before the stream closes. Matches Camtom's
#: ``_SSE_IDLE_TIMEOUT``.
SSE_IDLE_TIMEOUT_S: float = 300.0

#: How often the generator polls :meth:`Request.is_disconnected`.
SSE_DISCONNECT_POLL_INTERVAL_S: float = 0.25

#: The wire frame Camtom uses to keep SSE connections warm. An SSE
#: comment (``:`` prefix) is ignored by browsers but counts as traffic
#: for intermediaries.
SSE_HEARTBEAT_FRAME: str = ":ping\n\n"


def format_sse_event(event: dict[str, Any]) -> str:
    """Render one event as an SSE-formatted frame.

    Mirrors Camtom's ``_format_sse_event``: the ``type`` field becomes
    the ``event:`` line so listeners can dispatch by type, and the full
    JSON payload is chunked onto ``data:`` lines so multi-line JSON
    stays valid SSE.

    **v1 resume status.** An ``id:`` line is emitted only when the
    event carries a non-null ``sequence`` field. In practice, v1's
    runtime supervisor does NOT assign sequence numbers yet, so this
    branch is dormant and the SSE endpoint ignores the browser's
    ``Last-Event-ID`` header. Browsers that reconnect get a fresh
    stream from "now" onward, and the durable history can be replayed
    via a separate endpoint (TBD, Epic 4). Keeping the ``id:``
    emission dormant (rather than wiring it to the SQLite row id) is
    deliberate — mixing two cursor spaces (``sequence`` vs row ``id``)
    would silently skip or duplicate events on resume.
    """
    event_type = str(event.get("type", "message"))
    sequence = event.get("sequence")
    payload = json.dumps(event, default=str)
    data_lines = payload.splitlines() or [payload]
    data = "".join(f"data: {line}\n" for line in data_lines)
    id_line = f"id: {sequence}\n" if sequence is not None else ""
    return f"{id_line}event: {event_type}\n{data}\n"


class SSEEventSink:
    """In-process fan-out from ingest → browser SSE subscribers.

    ``subscribe(session_id)`` returns a fresh bounded
    :class:`asyncio.Queue`; the caller is responsible for passing it
    back to :meth:`unsubscribe` on connection teardown (the SSE
    generator below does this in a ``finally`` block).

    Call :meth:`emit` from the ingest handler after an event has been
    validated and persisted. It is non-blocking, O(n) in the number of
    subscribers for one session, and never raises.
    """

    def __init__(self, max_queue_size: int = 100) -> None:
        self._max_queue_size = max_queue_size
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    @property
    def subscriber_count(self) -> int:
        return sum(len(v) for v in self._subscribers.values())

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Create a new bounded queue and register it as a subscriber."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=self._max_queue_size
        )
        self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(
        self, session_id: str, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        """Remove a subscriber queue and clean up empty session lists."""
        subs = self._subscribers.get(session_id)
        if subs is None:
            return
        try:
            subs.remove(queue)
        except ValueError:
            pass
        if not subs:
            self._subscribers.pop(session_id, None)

    def emit(self, event: dict[str, Any]) -> None:
        """Push ``event`` to every subscriber of its ``session_id``.

        Drop-oldest-on-overflow per subscriber — a slow browser cannot
        stall the ingest path. Never raises; a broken subscriber is
        logged and skipped.
        """
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        subs = self._subscribers.get(session_id)
        if not subs:
            return
        for queue in list(subs):
            try:
                queue.put_nowait(event)
                continue
            except asyncio.QueueFull:
                pass
            # Make room by dropping the oldest buffered event, then retry.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _log.warning(
                    "sse subscriber for session %s is stuck; dropping event",
                    session_id,
                )


async def sse_event_generator(
    session_id: str,
    sink: SSEEventSink,
    request: Request,
    *,
    heartbeat_interval_s: float = SSE_HEARTBEAT_INTERVAL_S,
    idle_timeout_s: float = SSE_IDLE_TIMEOUT_S,
    disconnect_poll_interval_s: float = SSE_DISCONNECT_POLL_INTERVAL_S,
) -> AsyncIterator[str]:
    """Yield SSE frames for one browser connection.

    1. Subscribes to ``sink`` for ``session_id``.
    2. Emits an initial ``:ping`` frame so the client's
       :class:`EventSource` fires ``onopen`` before any real events.
    3. Waits up to ``heartbeat_interval_s`` for the next queued event.
       On timeout, emits another ``:ping`` and resumes.
    4. Exits cleanly when:
       * The client disconnects (``request.is_disconnected()``).
       * No events arrive for ``idle_timeout_s`` seconds.

    The ``finally`` branch always unsubscribes the queue so a dropped
    connection cannot leak a slot in the sink.
    """
    queue = sink.subscribe(session_id)
    try:
        # Initial connect flush. Camtom ships the same comment frame so
        # nginx / Fly proxies see traffic before the first real event.
        yield SSE_HEARTBEAT_FRAME

        loop = asyncio.get_event_loop()
        last_event_at = loop.time()

        while True:
            if await request.is_disconnected():
                _log.debug("sse client for %s disconnected", session_id)
                return

            now = loop.time()
            if now - last_event_at > idle_timeout_s:
                _log.debug(
                    "sse idle timeout (%.1fs) for %s",
                    idle_timeout_s,
                    session_id,
                )
                return

            try:
                event = await asyncio.wait_for(
                    queue.get(),
                    timeout=min(heartbeat_interval_s, disconnect_poll_interval_s * 4),
                )
            except asyncio.TimeoutError:
                yield SSE_HEARTBEAT_FRAME
                continue

            yield format_sse_event(event)
            last_event_at = loop.time()
    finally:
        sink.unsubscribe(session_id, queue)
