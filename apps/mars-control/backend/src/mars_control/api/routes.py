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

from fastapi import FastAPI

from mars_control.events.ingest import create_ingest_router
from mars_control.store.events import EventStore

__all__ = ["create_control_app"]


def create_control_app(
    *,
    store: EventStore | None = None,
    event_secret: str | None = None,
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

    app.include_router(create_ingest_router(effective_store, effective_secret))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
