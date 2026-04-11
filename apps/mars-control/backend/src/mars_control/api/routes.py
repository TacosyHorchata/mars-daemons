"""FastAPI app factory for the Mars control plane.

v1 scope (Story 2.2):

* ``GET /health`` — cheap liveness probe for Fly health checks.
* ``POST /internal/events`` — event ingest from the runtime
  forwarder (see :mod:`mars_control.events.ingest`).

Upcoming stories:

* Story 2.3 — ``GET /sessions/{id}/stream`` — browser SSE fanout.
* Epic 4 — magic-link auth + workspace CRUD.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from mars_control.events.ingest import create_ingest_router
from mars_control.sse.stream import SSEEventSink, sse_event_generator
from mars_control.store.events import EventStore

__all__ = ["create_control_app"]


def create_control_app(
    *,
    store: EventStore | None = None,
    event_secret: str | None = None,
    sink: SSEEventSink | None = None,
) -> FastAPI:
    """Build a FastAPI app for the Mars control plane.

    Args:
        store: Optional pre-built :class:`EventStore`. Tests inject an
            ``:memory:`` store; production leaves this ``None`` and the
            factory builds one from ``MARS_CONTROL_DB_PATH``.
        event_secret: Optional shared secret for ``X-Event-Secret``
            validation. Defaults to ``MARS_EVENT_SECRET`` env var.
            An empty string here causes ingest requests to fail with
            500 — intentional, misconfiguration should not silently
            accept forged events.
        sink: Optional :class:`SSEEventSink` instance. Tests inject a
            fresh sink per app so subscribers do not leak between
            tests. Production leaves it ``None`` and the factory
            creates one.
    """

    owned_store = store is None
    effective_store = store or EventStore(
        path=os.environ.get("MARS_CONTROL_DB_PATH", "mars_control.db")
    )
    effective_secret = (
        event_secret
        if event_secret is not None
        else os.environ.get("MARS_EVENT_SECRET", "")
    )
    effective_sink = sink or SSEEventSink()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        effective_store.init()
        try:
            yield
        finally:
            if owned_store:
                effective_store.close()

    app = FastAPI(
        title="Mars Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.event_store = effective_store
    app.state.sse_sink = effective_sink

    app.include_router(
        create_ingest_router(effective_store, effective_secret, effective_sink)
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/sessions/{session_id}/stream")
    async def session_stream(session_id: str, request: Request) -> StreamingResponse:
        """Browser SSE endpoint. Lifts Camtom's streaming response
        shape: ``text/event-stream`` with ``no-cache``, streamed via
        the event generator in :mod:`mars_control.sse.stream`."""
        if not session_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session_id required",
            )
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        }
        return StreamingResponse(
            sse_event_generator(session_id, effective_sink, request),
            media_type="text/event-stream",
            headers=headers,
        )

    return app
