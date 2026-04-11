"""HTTP ingest endpoint — receives Mars events from the runtime forwarder.

Exposes a single route, ``POST /internal/events``, that:

1. Validates the ``X-Event-Secret`` header in constant time
   (:func:`hmac.compare_digest`) so attackers cannot distinguish
   "missing secret" from "wrong secret" via timing.
2. Parses the request body as a :class:`EventBatch` (a list of
   MarsEvent dicts).
3. Persists durable events via :meth:`EventStore.write_batch`. Ephemeral
   events are counted for the response but not written — they flow to
   browsers via Story 2.3's SSE fanout instead.
4. Returns ``202 Accepted`` with ``{"received": N, "persisted": M}``.

Failure modes:

* Missing / wrong secret → ``401 Unauthorized``
* Server has no secret configured → ``500 Internal Server Error``
  (misconfiguration; the runtime will retry and operators get a loud
  signal in logs).
* Malformed batch body → FastAPI returns ``422 Unprocessable Entity``
  with Pydantic validation details.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from events.types import MARS_EVENT_ADAPTER
from mars_control.sse.stream import SSEEventSink
from mars_control.store.events import EventStore

__all__ = ["MAX_INGEST_BATCH_SIZE", "EventBatch", "create_ingest_router"]

_log = logging.getLogger("mars.control.ingest")

#: Upper bound on events per POST. The runtime forwarder flushes at
#: 100 per batch; this gives us 2x headroom while still capping memory
#: at ~a few MB per request (each event is a few KB).
MAX_INGEST_BATCH_SIZE = 200


class EventBatch(BaseModel):
    """Request body for ``POST /internal/events``.

    ``events`` is *required* — tolerating a missing key would let a
    buggy producer silently drop data. The list is capped at
    :data:`MAX_INGEST_BATCH_SIZE` to bound request memory.
    """

    events: list[dict[str, Any]] = Field(
        ...,
        min_length=0,
        max_length=MAX_INGEST_BATCH_SIZE,
        description="List of MarsEvent payloads, JSON-serialized.",
    )


def _check_secret(header: str | None, expected: str) -> None:
    """Constant-time comparison of the ``X-Event-Secret`` header."""
    if not expected:
        # Server misconfiguration — the operator forgot to set the
        # env var. Better to fail loudly than silently accept forged
        # events.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="control plane has no event secret configured",
        )
    provided = header or ""
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid X-Event-Secret",
        )


def create_ingest_router(
    store: EventStore,
    expected_secret: str,
    sink: SSEEventSink | None = None,
) -> APIRouter:
    """Build an ingest router wired to a specific store + secret.

    If ``sink`` is provided, every validated event is broadcast to
    its subscribers *after* durable persistence. This is the in-process
    fan-out path that feeds browser SSE subscribers — see
    :mod:`mars_control.sse.stream`.
    """

    router = APIRouter(prefix="/internal", tags=["ingest"])

    @router.post("/events", status_code=status.HTTP_202_ACCEPTED)
    async def ingest_events(
        batch: EventBatch,
        x_event_secret: str | None = Header(default=None),
    ) -> dict[str, int]:
        _check_secret(x_event_secret, expected_secret)
        # Validate every event against the MarsEvent discriminated
        # union BEFORE persisting. A forged / malformed payload that
        # merely has a durable `type` string would otherwise poison
        # downstream replay + SSE consumers.
        validated: list[dict[str, Any]] = []
        for idx, raw in enumerate(batch.events):
            try:
                ev = MARS_EVENT_ADAPTER.validate_python(raw)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "message": f"malformed event at index {idx}",
                        "errors": exc.errors()[:5],
                    },
                ) from exc
            # Re-dump to a normalized JSON-safe dict. Downstream
            # consumers read this shape, not the raw wire payload.
            validated.append(MARS_EVENT_ADAPTER.dump_python(ev, mode="json"))

        received = len(validated)
        persisted = await store.write_batch(validated)
        if received and received != persisted:
            _log.info(
                "ingest: received=%d persisted=%d ephemeral_dropped=%d",
                received,
                persisted,
                received - persisted,
            )
        # Fan out ALL validated events (durable + ephemeral) to any
        # browser SSE subscribers. Ephemerals do not survive restart
        # but they DO reach a connected browser — that is the whole
        # point of the chunk stream.
        if sink is not None:
            for ev in validated:
                sink.emit(ev)
        return {"received": received, "persisted": persisted}

    return router
