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
from typing import AsyncIterator, Callable

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from mars_control.events.ingest import create_ingest_router
from mars_control.sse.stream import SSEEventSink, sse_event_generator
from mars_control.store.events import EventStore

__all__ = [
    "PromptUpdatePayload",
    "SessionLocator",
    "create_control_app",
]


class PromptUpdatePayload(BaseModel):
    """Request body for ``PATCH /agents/{name}/prompt``."""

    session_id: str = Field(..., min_length=1, description="Session id on the target supervisor.")
    content: str = Field(..., min_length=1, max_length=256 * 1024)


#: Callable that maps (agent_name, session_id) → supervisor base URL.
#: v1 lookup is a tiny in-memory dict; Epic 5 replaces it with a
#: persisted session registry.
SessionLocator = Callable[[str, str], str | None]


def create_control_app(
    *,
    store: EventStore | None = None,
    event_secret: str | None = None,
    sink: SSEEventSink | None = None,
    session_locator: SessionLocator | None = None,
    http_client: httpx.AsyncClient | None = None,
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
    effective_locator: SessionLocator = session_locator or (lambda _a, _s: None)
    owned_http_client = http_client is None
    effective_http = http_client or httpx.AsyncClient(timeout=10.0)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        effective_store.init()
        try:
            yield
        finally:
            if owned_store:
                effective_store.close()
            if owned_http_client:
                await effective_http.aclose()

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

    @app.patch("/agents/{agent_name}/prompt")
    async def update_agent_prompt(
        agent_name: str, payload: PromptUpdatePayload
    ) -> dict[str, object]:
        """Admin edit flow (Story 6.4).

        Looks up the supervisor that hosts ``session_id`` for ``agent_name``
        via the injected locator, then forwards the prompt update to
        the supervisor's ``POST /sessions/{id}/reload-prompt`` endpoint.

        The locator is the seam between v1 (hardcoded mapping or env
        var) and Epic 5's persisted session registry — neither the
        admin UI nor the CLI needs to know about that implementation
        detail.
        """
        supervisor_url = effective_locator(agent_name, payload.session_id)
        if supervisor_url is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"no supervisor registered for agent {agent_name!r} "
                    f"session {payload.session_id!r}"
                ),
            )
        target = f"{supervisor_url.rstrip('/')}/sessions/{payload.session_id}/reload-prompt"
        try:
            resp = await effective_http.post(
                target, json={"content": payload.content}
            )
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"supervisor unreachable at {target}: {exc}",
            ) from exc
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"supervisor returned {resp.status_code} at {target}: "
                    f"{resp.text[:500]}"
                ),
            )
        return {
            "agent_name": agent_name,
            "session_id": payload.session_id,
            "supervisor": supervisor_url,
            "result": resp.json() if resp.content else {},
        }

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
