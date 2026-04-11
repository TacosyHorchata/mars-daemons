"""Outbound HTTP event forwarder for the Mars runtime.

Flips the SSE topology vs. a naive "relay SSE from machine to control
plane" design: the machine is a *producer* that batches events and
POSTs them to the control plane's ingest endpoint over HTTP. The
control plane is the durable sink + browser SSE fanout. One SSE hop,
not two.

Lifts the basic shape of Camtom's ``HttpEventSink``
(``services/fastapi/src/products/agents/agent/sink.py:33-83``) and
extends it with the additional semantics the Mars epic requires:

* Batching: up to :data:`DEFAULT_MAX_BATCH` events or
  :data:`DEFAULT_FLUSH_INTERVAL_S` seconds, whichever comes first.
* Retry on network error with exponential backoff
  (:data:`DEFAULT_INITIAL_BACKOFF_S` doubling up to
  :data:`DEFAULT_MAX_BACKOFF_S`).
* In-memory buffer up to :data:`DEFAULT_BUFFER_LIMIT` events. When the
  buffer fills, the forwarder drops the **oldest ephemeral** event
  first and NEVER drops a durable event. If the buffer is
  all-durable-and-full it logs an error and keeps growing past the
  limit — the epic acknowledges this as an acceptable v1 failure mode
  because durable loss would be worse.
* ``X-Event-Secret`` header authentication — every POST carries the
  shared secret, and logs only a short sha256 prefix of the secret
  value so grep-on-logs can't leak the full key.
* Graceful shutdown: :meth:`stop` flushes the remaining buffer before
  closing the HTTP client.

The forwarder is agnostic of where its events come from. In v1 the
supervisor's ``_SessionPump`` (see ``supervisor.py``) will wire its
queue into :meth:`emit`; Epic 2 Story 2.4 proves that path end-to-end.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from typing import Any

import httpx

from events.types import MARS_EVENT_ADAPTER, MarsEventBase, is_ephemeral

__all__ = [
    "DEFAULT_BUFFER_LIMIT",
    "DEFAULT_FLUSH_INTERVAL_S",
    "DEFAULT_INITIAL_BACKOFF_S",
    "DEFAULT_MAX_BACKOFF_S",
    "DEFAULT_MAX_BATCH",
    "HttpEventForwarder",
]

_log = logging.getLogger(__name__)

DEFAULT_MAX_BATCH = 100
DEFAULT_FLUSH_INTERVAL_S = 0.5
DEFAULT_BUFFER_LIMIT = 1000
DEFAULT_INITIAL_BACKOFF_S = 0.5
DEFAULT_MAX_BACKOFF_S = 8.0


#: sha256 hex prefix length used by :func:`_secret_fingerprint`. 16 hex
#: chars = 64 bits of entropy — enough to avoid accidental collisions
#: when correlating many rotations in logs, without revealing enough of
#: the digest to enable any dictionary attack on the full secret.
_FINGERPRINT_HEX_LEN = 16


def _secret_fingerprint(secret: str) -> str:
    """Short hash prefix of the shared secret, safe to log."""
    if not secret:
        return "<empty>"
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:_FINGERPRINT_HEX_LEN]}"


class HttpEventForwarder:
    """Batches Mars events and POSTs them to the control plane.

    Lifecycle:

    1. Construct with url + secret (and optionally an injected
       :class:`httpx.AsyncClient` for tests).
    2. Call :meth:`start` — launches the background flush task.
    3. Call :meth:`emit` from whatever produces events. It is O(1),
       non-blocking, and never raises in the producer's hot path.
    4. Call :meth:`stop` on shutdown — flushes remaining buffer and
       closes the HTTP client (if owned).

    Async context-manager sugar is also provided:
    ``async with HttpEventForwarder(...) as fwd: ...``.
    """

    def __init__(
        self,
        *,
        url: str,
        secret: str,
        max_batch: int = DEFAULT_MAX_BATCH,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
        initial_backoff_s: float = DEFAULT_INITIAL_BACKOFF_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        client: httpx.AsyncClient | None = None,
    ):
        if max_batch <= 0:
            raise ValueError(f"max_batch must be positive, got {max_batch}")
        if buffer_limit <= 0:
            raise ValueError(f"buffer_limit must be positive, got {buffer_limit}")

        self._url = url
        self._secret = secret
        self._max_batch = max_batch
        self._flush_interval_s = flush_interval_s
        self._buffer_limit = buffer_limit
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._current_backoff_s = initial_backoff_s

        self._buffer: deque[MarsEventBase] = deque()
        self._flush_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._flush_pending = asyncio.Event()
        #: Serializes _flush_once across the background loop, the
        #: public flush(), and the stop() final drain. Without this
        #: lock a caller invoking flush() during a backoff sleep can
        #: reorder sends or bypass the backoff interval.
        self._flush_lock = asyncio.Lock()

        # Drops are surfaced via properties for tests + observability.
        self._dropped_ephemeral_count = 0
        self._overflow_durable_warning_count = 0
        self._sent_count = 0
        self._failed_post_count = 0

        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------
    # Properties for tests + observability
    # ------------------------------------------------------------------

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    @property
    def dropped_ephemeral_count(self) -> int:
        return self._dropped_ephemeral_count

    @property
    def sent_count(self) -> int:
        return self._sent_count

    @property
    def failed_post_count(self) -> int:
        return self._failed_post_count

    @property
    def is_running(self) -> bool:
        return self._flush_task is not None and not self._flush_task.done()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "HttpEventForwarder":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start the background flush task and create the HTTP client."""
        if self._flush_task is not None:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
            self._owns_client = True
        _log.info(
            "forwarder starting url=%s secret=%s",
            self._url,
            _secret_fingerprint(self._secret),
        )
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="mars-event-forwarder"
        )

    async def stop(self) -> None:
        """Flush remaining buffer and shut down the background task.

        Robust to a flush task that crashed with an exception — the
        exception is logged and shutdown proceeds, so a dead task
        cannot skip the final drain or leak the owned httpx client.
        """
        self._stop_event.set()
        self._flush_pending.set()
        if self._flush_task is not None:
            try:
                await asyncio.wait_for(self._flush_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._flush_task.cancel()
                try:
                    await self._flush_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    _log.exception("forwarder flush task raised on cancel")
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                _log.exception("forwarder flush task crashed; proceeding with drain")
            finally:
                self._flush_task = None

        # Best-effort final drain. Guarded by the same lock as the
        # background loop so a late-arriving public flush() can't race.
        if self._buffer and self._client is not None:
            try:
                await self._flush_once()
            except Exception:  # noqa: BLE001
                _log.exception(
                    "final flush failed; %d events lost", len(self._buffer)
                )

        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                _log.exception("httpx client close failed")
            self._client = None

    # ------------------------------------------------------------------
    # Producer API
    # ------------------------------------------------------------------

    async def emit(self, event: MarsEventBase) -> None:
        """Append an event to the buffer. O(1), never blocks.

        If the buffer is full, drops the oldest ephemeral event first.
        Durable events are never dropped — if the buffer is full of
        durables, we grow past the limit and log an error so the
        supervisor's alerting can catch it.
        """
        if len(self._buffer) >= self._buffer_limit:
            if not self._drop_oldest_ephemeral():
                self._overflow_durable_warning_count += 1
                if self._overflow_durable_warning_count == 1 or (
                    self._overflow_durable_warning_count % 100 == 0
                ):
                    _log.error(
                        "forwarder buffer at limit %d with no ephemeral events "
                        "to drop; durable events are accumulating (count=%d).",
                        self._buffer_limit,
                        self._overflow_durable_warning_count,
                    )
        self._buffer.append(event)
        if len(self._buffer) >= self._max_batch:
            self._flush_pending.set()

    def _drop_oldest_ephemeral(self) -> bool:
        """Remove the oldest ephemeral event from the buffer.

        Returns ``True`` if one was dropped, ``False`` if the buffer
        contains no ephemerals.
        """
        for idx, ev in enumerate(self._buffer):
            if is_ephemeral(ev.type):
                del self._buffer[idx]
                self._dropped_ephemeral_count += 1
                return True
        return False

    async def flush(self) -> None:
        """Trigger an immediate flush. Awaits completion of one batch."""
        if not self._buffer:
            return
        await self._flush_once()

    # ------------------------------------------------------------------
    # Internal flush loop
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        while not self._stop_event.is_set():
            # Sleep until either the flush interval elapses or emit()
            # wakes us because the buffer hit max_batch.
            try:
                await asyncio.wait_for(
                    self._flush_pending.wait(),
                    timeout=self._flush_interval_s,
                )
            except asyncio.TimeoutError:
                pass
            self._flush_pending.clear()
            if self._buffer:
                try:
                    await self._flush_once()
                except Exception:  # noqa: BLE001
                    _log.exception("forwarder flush loop iteration failed")
            if self._stop_event.is_set():
                return

    async def _flush_once(self) -> None:
        """Take up to ``max_batch`` events from the front of the buffer
        and POST them.

        Serialized by :attr:`_flush_lock` so concurrent calls from the
        background loop, :meth:`flush`, and :meth:`stop` cannot reorder
        sends or bypass the backoff interval.

        On any recoverable failure the batch is re-queued to the front
        of the buffer (preserving FIFO for later emits). Only explicit
        4xx responses drop the batch: a 4xx means a contract bug
        (malformed payload, wrong secret) and retrying a malformed
        request wastes the buffer and masks the bug from alerting.
        """
        async with self._flush_lock:
            if self._client is None:
                return
            batch: list[MarsEventBase] = []
            while self._buffer and len(batch) < self._max_batch:
                batch.append(self._buffer.popleft())
            if not batch:
                return

            try:
                payload = {
                    "events": [
                        MARS_EVENT_ADAPTER.dump_python(ev, mode="json")
                        for ev in batch
                    ]
                }
            except Exception:  # noqa: BLE001
                # Serialization itself failed — re-queue so we don't
                # silently lose events, then log. This is a code bug
                # if it ever fires.
                self._requeue(batch)
                self._failed_post_count += 1
                _log.exception("forwarder payload serialization failed")
                return

            headers = {"X-Event-Secret": self._secret}

            try:
                resp = await self._client.post(
                    self._url, json=payload, headers=headers
                )
            except httpx.RequestError as exc:
                _log.warning(
                    "forwarder POST failed (transport): %s — re-queuing %d events",
                    exc,
                    len(batch),
                )
                self._requeue(batch)
                self._failed_post_count += 1
                await self._apply_backoff()
                return
            except Exception as exc:  # noqa: BLE001
                # Any other error after the batch was popped (e.g.
                # closed client, unexpected RuntimeError) must NOT
                # silently drop the batch. Re-queue and log loudly.
                _log.exception(
                    "forwarder POST raised unexpected %s — re-queuing %d events",
                    type(exc).__name__,
                    len(batch),
                )
                self._requeue(batch)
                self._failed_post_count += 1
                await self._apply_backoff()
                return

            if resp.status_code >= 500:
                _log.warning(
                    "forwarder POST failed (5xx status=%d) — re-queuing %d events",
                    resp.status_code,
                    len(batch),
                )
                self._requeue(batch)
                self._failed_post_count += 1
                await self._apply_backoff()
                return

            if resp.status_code >= 400:
                _log.error(
                    "forwarder POST rejected with %d — dropping batch of %d events: %s",
                    resp.status_code,
                    len(batch),
                    resp.text[:500],
                )
                self._failed_post_count += 1
                return

            self._sent_count += len(batch)
            self._reset_backoff()

    def _requeue(self, batch: list[MarsEventBase]) -> None:
        """Push a failed batch back to the front of the buffer in order."""
        for ev in reversed(batch):
            self._buffer.appendleft(ev)

    async def _apply_backoff(self) -> None:
        await asyncio.sleep(self._current_backoff_s)
        self._current_backoff_s = min(
            self._current_backoff_s * 2, self._max_backoff_s
        )

    def _reset_backoff(self) -> None:
        self._current_backoff_s = self._initial_backoff_s

    # ------------------------------------------------------------------
    # Debug / introspection helper
    # ------------------------------------------------------------------

    def debug_state(self) -> dict[str, Any]:
        return {
            "url": self._url,
            "secret_fingerprint": _secret_fingerprint(self._secret),
            "buffered": self.buffered,
            "dropped_ephemeral_count": self._dropped_ephemeral_count,
            "overflow_durable_warning_count": self._overflow_durable_warning_count,
            "sent_count": self._sent_count,
            "failed_post_count": self._failed_post_count,
            "current_backoff_s": self._current_backoff_s,
        }
