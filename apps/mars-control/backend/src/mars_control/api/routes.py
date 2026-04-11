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
from pathlib import Path
from typing import AsyncIterator, Callable

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field

from mars_control.auth.email import (
    EmailSender,
    EmailSendError,
    InMemoryEmailSender,
    ResendEmailSender,
)
from mars_control.auth.magic_link import (
    MagicLinkError,
    MagicLinkService,
    MagicLinkToken,
)
from mars_control.auth.middleware import make_current_user_dependency
from mars_control.auth.rate_limit import RateLimiter
from mars_control.auth.session import (
    DEFAULT_SESSION_COOKIE_NAME,
    SessionCookieService,
    SessionUser,
)
from mars_control.events.ingest import create_ingest_router
from mars_control.sse.stream import SSEEventSink, sse_event_generator
from mars_control.store.events import EventStore
from mars_control.templates import (
    DEFAULT_TEMPLATE_DIR,
    TemplateDirMissingError,
    discover_templates,
)

__all__ = [
    "MagicLinkRequestPayload",
    "MagicLinkVerifyPayload",
    "PromptUpdatePayload",
    "SessionLocator",
    "create_control_app",
]


class MagicLinkRequestPayload(BaseModel):
    """Body for ``POST /auth/magic-link``."""

    email: EmailStr


class MagicLinkVerifyPayload(BaseModel):
    """Body for ``POST /auth/magic-link/verify``."""

    token: str = Field(..., min_length=1)


class PromptUpdatePayload(BaseModel):
    """Request body for ``PATCH /agents/{name}/prompt``."""

    session_id: str = Field(..., min_length=1, description="Session id on the target supervisor.")
    content: str = Field(..., min_length=1, max_length=256 * 1024)


class SessionInputPayload(BaseModel):
    """Request body for ``POST /sessions/{id}/input`` — user message to a daemon."""

    text: str = Field(..., min_length=1, max_length=64 * 1024)


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
    magic_link_service: MagicLinkService | None = None,
    session_service: SessionCookieService | None = None,
    email_sender: EmailSender | None = None,
    magic_link_base_url: str | None = None,
    magic_link_rate_limiter: RateLimiter | None = None,
    default_supervisor_url: str | None = None,
    cors_allow_origins: list[str] | None = None,
    template_dir: Path | None = None,
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

    # --- Auth wiring -----------------------------------------------------
    # When the caller does not pass explicit services, fall back to env vars
    # so production deploys can configure via fly secrets without touching
    # code. A missing secret raises at first use rather than at app build
    # so test harnesses that don't exercise auth routes still work.
    effective_magic_link = magic_link_service
    if effective_magic_link is None:
        magic_secret = os.environ.get("MARS_MAGIC_LINK_SECRET", "")
        if magic_secret:
            effective_magic_link = MagicLinkService(secret=magic_secret)
    effective_session_service = session_service
    if effective_session_service is None:
        session_secret = os.environ.get("MARS_SESSION_SECRET", "")
        if session_secret:
            effective_session_service = SessionCookieService(secret=session_secret)
    effective_email_sender = email_sender
    if effective_email_sender is None:
        resend_key = os.environ.get("RESEND_API_KEY", "")
        from_addr = os.environ.get("MARS_FROM_EMAIL", "")
        if resend_key and from_addr:
            effective_email_sender = ResendEmailSender(
                api_key=resend_key, from_address=from_addr
            )
    effective_ml_base = (
        magic_link_base_url
        if magic_link_base_url is not None
        else os.environ.get("MARS_MAGIC_LINK_BASE_URL", "")
    )
    # Story 9.2: per-IP rate limit on POST /auth/magic-link. Defaults
    # to 5 req/min/IP when the caller doesn't inject one.
    effective_rate_limiter = magic_link_rate_limiter or RateLimiter()

    # Local Fly emulation (pre-deploy): when the admin points
    # ``MARS_DEFAULT_SUPERVISOR_URL`` at a single runtime supervisor
    # (docker or uvicorn on localhost:8080), ``GET /sessions`` and
    # ``POST /sessions/{id}/input`` proxy there directly. Epic 5 replaces
    # this single-target env var with a persisted session registry that
    # can fan out to N supervisors.
    effective_default_supervisor = (
        default_supervisor_url
        if default_supervisor_url is not None
        else os.environ.get("MARS_DEFAULT_SUPERVISOR_URL", "") or None
    )

    # Local dev CORS — the frontend runs on :3000 and the control plane
    # on :8000 in local mode, which are different origins. Production
    # (same-origin behind Fly) does not need this middleware. Defaults
    # to the MARS_CORS_ORIGINS env var (comma-separated) or an empty
    # list when neither is set.
    effective_cors_origins = (
        cors_allow_origins
        if cors_allow_origins is not None
        else [
            o.strip()
            for o in os.environ.get("MARS_CORS_ORIGINS", "").split(",")
            if o.strip()
        ]
    )

    # Story 8.2 — template discovery directory. Tests inject a tmp
    # path; production uses the repo-relative templates/ folder.
    effective_template_dir: Path = (
        Path(template_dir)
        if template_dir is not None
        else Path(
            os.environ.get("MARS_TEMPLATE_DIR", "")
            or DEFAULT_TEMPLATE_DIR
        )
    )

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

    if effective_cors_origins:
        # Codex caught this: `allow_credentials=True` is only safe with
        # a tight, explicit list of origins. Reject `*` and the empty
        # string at factory time so a mis-set MARS_CORS_ORIGINS env var
        # cannot silently open cookie-carrying cross-origin requests.
        for origin in effective_cors_origins:
            if origin == "*" or not origin:
                raise ValueError(
                    "cors_allow_origins must be an explicit list of HTTP origins "
                    "when credentials are forwarded — '*' and empty strings are "
                    "rejected"
                )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=effective_cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    # Protected-route dependency. When the session service is not
    # configured (headless tests, ingest-only deploys) we raise 503
    # rather than silently letting unauth'd traffic through — codex
    # flagged the unauthed proxy endpoints as a critical regression.
    def _require_current_user(request: Request) -> SessionUser:
        if effective_session_service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "auth not configured on the control plane: missing "
                    "session cookie service"
                ),
            )
        return make_current_user_dependency(effective_session_service)(request)

    app.include_router(
        create_ingest_router(effective_store, effective_secret, effective_sink)
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Magic-link auth (Story 4.2)
    # ------------------------------------------------------------------
    def _require_auth_stack() -> tuple[MagicLinkService, SessionCookieService, EmailSender]:
        missing = []
        if effective_magic_link is None:
            missing.append("magic-link service")
        if effective_session_service is None:
            missing.append("session cookie service")
        if effective_email_sender is None:
            missing.append("email sender")
        if missing:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "auth not configured on the control plane: missing "
                    + ", ".join(missing)
                ),
            )
        return effective_magic_link, effective_session_service, effective_email_sender  # type: ignore[return-value]

    @app.post("/auth/magic-link", status_code=status.HTTP_202_ACCEPTED)
    async def request_magic_link(
        payload: MagicLinkRequestPayload,
        request: Request,
    ) -> dict[str, str]:
        magic, _, sender = _require_auth_stack()
        client_ip = (
            request.client.host if request.client is not None else "unknown"
        )
        if not effective_rate_limiter.check(client_ip):
            retry_after = int(
                effective_rate_limiter.retry_after_seconds(client_ip) + 1
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"too many magic-link requests from {client_ip} — "
                    f"retry in {retry_after}s"
                ),
                headers={"Retry-After": str(retry_after)},
            )
        token = magic.issue(payload.email)
        link_base = effective_ml_base or ""
        link = (
            f"{link_base.rstrip('/')}/auth/verify?token={token}"
            if link_base
            else f"/auth/verify?token={token}"
        )
        body = (
            f"Your Mars sign-in link:\n\n{link}\n\n"
            f"This link is single-use and expires in 15 minutes. "
            f"If you did not request this, ignore the email."
        )
        try:
            await sender.send(
                to=payload.email,
                subject="Your Mars sign-in link",
                body_text=body,
            )
        except EmailSendError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"email delivery failed: {exc}",
            ) from exc
        return {"status": "sent", "email": payload.email}

    @app.post("/auth/magic-link/verify")
    async def verify_magic_link(
        payload: MagicLinkVerifyPayload, response: Response
    ) -> dict[str, str]:
        magic, sess, _ = _require_auth_stack()
        try:
            verified: MagicLinkToken = magic.verify(payload.token)
        except MagicLinkError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
        magic.consume(verified)
        cookie_value = sess.issue(verified.email)
        response.set_cookie(**sess.build_set_cookie_kwargs(cookie_value))
        return {"email": verified.email, "status": "signed_in"}

    @app.post("/auth/logout")
    async def logout(response: Response) -> dict[str, str]:
        _, sess, _ = _require_auth_stack()
        response.set_cookie(**sess.build_clear_cookie_kwargs())
        return {"status": "signed_out"}

    @app.get("/me")
    async def me(request: Request) -> dict[str, object]:
        _, sess, _ = _require_auth_stack()
        current_user_dep = make_current_user_dependency(sess)
        user: SessionUser = current_user_dep(request)
        return {
            "email": user.email,
            "issued_at": user.issued_at.isoformat(),
            "expires_at": user.expires_at.isoformat(),
        }

    @app.patch("/agents/{agent_name}/prompt")
    async def update_agent_prompt(
        agent_name: str,
        payload: PromptUpdatePayload,
        _user: SessionUser = Depends(_require_current_user),
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

    # ------------------------------------------------------------------
    # Template discovery (Story 8.2)
    # ------------------------------------------------------------------
    @app.get("/templates")
    async def list_templates(
        _user: SessionUser = Depends(_require_current_user),
    ) -> dict[str, object]:
        try:
            summaries = discover_templates(effective_template_dir)
        except TemplateDirMissingError as exc:
            # Codex review — previously this silently returned an
            # empty list when MARS_TEMPLATE_DIR pointed at a
            # nonexistent path. Now we fail loud with 500 so a
            # misconfigured deploy surfaces immediately.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc
        return {"templates": [s.to_dict() for s in summaries]}

    # ------------------------------------------------------------------
    # Session proxy — v1 forwards to MARS_DEFAULT_SUPERVISOR_URL so the
    # browser only ever talks to the control plane. Epic 5 replaces this
    # with a persisted session registry that can fan out to multiple
    # supervisors and aggregate results.
    # ------------------------------------------------------------------
    def _require_default_supervisor() -> str:
        if not effective_default_supervisor:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "no default supervisor configured — set "
                    "MARS_DEFAULT_SUPERVISOR_URL or pass default_supervisor_url "
                    "to create_control_app()"
                ),
            )
        return effective_default_supervisor

    @app.get("/sessions")
    async def list_sessions_proxy(
        _user: SessionUser = Depends(_require_current_user),
    ) -> dict[str, object]:
        supervisor = _require_default_supervisor()
        target = f"{supervisor.rstrip('/')}/sessions"
        try:
            resp = await effective_http.get(target)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"supervisor unreachable at {target}: {exc}",
            ) from exc
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"supervisor returned {resp.status_code}: {resp.text[:500]}",
            )
        return resp.json()

    @app.post("/sessions/{session_id}/input")
    async def session_input_proxy(
        session_id: str,
        payload: SessionInputPayload,
        _user: SessionUser = Depends(_require_current_user),
    ) -> dict[str, object]:
        supervisor = _require_default_supervisor()
        target = f"{supervisor.rstrip('/')}/sessions/{session_id}/input"
        try:
            resp = await effective_http.post(target, json={"text": payload.text})
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"supervisor unreachable at {target}: {exc}",
            ) from exc
        if resp.status_code >= 500:
            # Mask upstream 5xx as 502 so the browser can distinguish
            # "bad request from you" (upstream 4xx) from "runtime sad"
            # (our 502). Body is truncated to keep error payloads small.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"supervisor {resp.status_code}: {resp.text[:500]}",
            )
        if resp.status_code >= 400:
            # Preserve upstream 4xx body + selected headers so the
            # browser sees structured errors (e.g. supervisor's 410
            # "session stdin closed" or 429 cap-reached + Retry-After).
            # Codex flagged that the original "status-only passthrough"
            # was dropping browser-actionable semantics.
            passthrough_headers: dict[str, str] = {}
            for h in ("retry-after", "www-authenticate"):
                value = resp.headers.get(h)
                if value is not None:
                    passthrough_headers[h.title()] = value
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
                headers=passthrough_headers or None,
            )  # type: ignore[return-value]
        return resp.json()

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
