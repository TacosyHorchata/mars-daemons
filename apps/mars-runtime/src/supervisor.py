"""FastAPI control API for the Mars runtime supervisor.

One :class:`~fastapi.FastAPI` app per Fly machine. Endpoints:

* ``POST   /sessions`` — spawn a session from an ``agent.yaml`` payload.
  Accepts JSON or YAML based on ``Content-Type``. Returns 201 +
  serialized :class:`~session.manager.SessionHandle`.
* ``GET    /sessions`` — list active sessions.
* ``GET    /sessions/{id}`` — fetch one.
* ``DELETE /sessions/{id}`` — kill a session.
* ``POST   /sessions/{id}/input`` — write a user message into the
  session's stdin as a stream-json event. v1.5 only supports plain-text
  prompts; tool results round-trip is v1.6+ territory.
* ``GET    /sessions/{id}/events`` — drain the session's in-memory
  event queue. v1.5 returns whatever is currently buffered. Epic 2
  replaces this with an outbound HTTP forwarder to the control plane.
* ``GET    /health`` — cheap liveness probe for Fly health checks.

Scope notes
-----------

* The control API has NO auth in v1. It binds to a Fly private network
  address; the control plane signs its inbound requests with a shared
  secret at the edge. Epic 3 lays down the Fly networking.
* The event queue is in-memory only, per session. A background task
  per session reads stdout → parses → enqueues. Epic 2 replaces the
  queue with an outbound HTTP sink that POSTs events to the control
  plane. Stories 1.5 + 2.x overlap deliberately so v1.5 can stand on
  its own during dogfood.
* Parser :class:`CriticalParseError` kills the session (propagates
  through :func:`parse_stream`, cancels the background task, the task
  tears down the session via :meth:`SessionManager.kill`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, ValidationError

from events.types import MARS_EVENT_ADAPTER, MarsEventBase
from schema.agent import AgentConfig
from session.claude_code import spawn_claude_code
from session.claude_code_stream import CriticalParseError, parse_stream
from session.codex import spawn_codex
from session.manager import SessionHandle, SessionManager, SpawnFn

# Hard cap on request body size. An agent.yaml is under a few hundred
# bytes in practice; anything larger than 64 KB is either malicious or
# misconfigured. Prevents naive memory-DoS via unbounded YAML/JSON.
_MAX_BODY_BYTES = 64 * 1024

# Timeout for :func:`asyncio.StreamWriter.drain` on /input writes. If
# the child stops reading, the write() buffer fills and drain() blocks
# forever — we want to surface that as a 503 instead of hanging the
# request.
_STDIN_DRAIN_TIMEOUT_S = 5.0

__all__ = [
    "InputPayload",
    "PermissionResponsePayload",
    "create_app",
    "serialize_handle",
]

_log = logging.getLogger("mars.supervisor")


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class InputPayload(BaseModel):
    """Request body for ``POST /sessions/{id}/input``."""

    text: str = Field(..., min_length=1, description="User message text.")


class PermissionResponsePayload(BaseModel):
    """Request body for ``POST /sessions/{id}/permission-response``.

    v1 deferred — returns 501 until the round-trip schema is finalized.
    """

    tool_use_id: str
    approved: bool


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------


def serialize_handle(handle: SessionHandle) -> dict[str, Any]:
    """JSON-safe representation of a :class:`SessionHandle`."""
    return {
        "session_id": handle.session_id,
        "name": handle.name,
        "description": handle.description,
        "status": handle.status,
        "pid": handle.pid,
        "is_alive": handle.is_alive,
        "started_at": handle.started_at.isoformat(),
        "terminated_at": (
            handle.terminated_at.isoformat() if handle.terminated_at else None
        ),
    }


# ---------------------------------------------------------------------------
# Event-pump task (per session)
# ---------------------------------------------------------------------------


class _SessionPump:
    """Background task that reads a session's stdout and fills its queue.

    One pump per session. Lives for the lifetime of the session. Stops
    when stdout hits EOF, the session is killed, or the parser raises
    :class:`CriticalParseError`.

    On any exit path that is NOT an explicit cancellation (EOF or fatal
    parser error), the pump schedules :meth:`SessionManager.kill` so
    the session leaves the ``running`` state and the supervisor does
    not accumulate orphan dict entries.

    Overflow policy: the pump uses a *blocking* :meth:`asyncio.Queue.put`
    (not ``put_nowait``). Durable Mars events must not be silently
    dropped (see :mod:`events.types`), so backpressure propagates all
    the way back to the subprocess stdout buffer if the consumer
    stalls. This is the correct degradation mode for v1.5 — Epic 2
    replaces the queue with an outbound HTTP sink and the pressure
    goes to the control plane instead.
    """

    def __init__(
        self,
        handle: SessionHandle,
        queue: asyncio.Queue[MarsEventBase],
        manager: SessionManager,
    ):
        self._handle = handle
        self._queue = queue
        self._manager = manager
        self._task: asyncio.Task[None] | None = None
        self._fatal: BaseException | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(), name=f"mars-pump-{self._handle.session_id}"
        )

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def fatal_error(self) -> BaseException | None:
        return self._fatal

    async def _run(self) -> None:
        proc = self._handle.process
        if proc.stdout is None:
            return
        cancelled = False
        try:
            async for ev in parse_stream(
                self._handle.session_id,
                proc.stdout,
                on_warning=lambda msg, exc: _log.warning(
                    "session %s parser warning: %s (%s)",
                    self._handle.session_id,
                    msg,
                    exc,
                ),
            ):
                await self._queue.put(ev)
        except CriticalParseError as exc:
            self._fatal = exc
            _log.error(
                "session %s hit CriticalParseError: %s",
                self._handle.session_id,
                exc,
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # noqa: BLE001
            self._fatal = exc
            _log.exception(
                "session %s event pump crashed", self._handle.session_id
            )
        finally:
            if not cancelled:
                # Stream ended naturally (EOF) or fatally. Schedule a
                # kill so the session leaves 'running' state and the
                # subprocess is reaped. Fire-and-forget: we are still
                # inside our own task and cannot await our own cancel.
                asyncio.create_task(  # noqa: RUF006
                    self._manager.kill(self._handle.session_id),
                    name=f"mars-pump-kill-{self._handle.session_id}",
                )


# ---------------------------------------------------------------------------
# create_app factory
# ---------------------------------------------------------------------------


# Queue size per session. An assistant turn emits ~5-10 events; chat
# UIs poll every few seconds; this buffers ~100 turns worth before the
# drop-oldest backpressure kicks in.
_DEFAULT_QUEUE_SIZE = 1024


def _default_spawn_fn() -> SpawnFn:
    """Production default: dispatch on ``config.runtime``.

    * ``claude-code`` → :func:`session.claude_code.spawn_claude_code`
      with ``stdin_stream_json=True`` so ``POST /sessions/{id}/input``
      can inject user events.
    * ``codex`` → :func:`session.codex.spawn_codex`. Story 3.5 only
      wires spawn + dispatch; a full codex event parser is v1.1.
    """

    async def _spawn(
        config: AgentConfig, session_id: str
    ) -> asyncio.subprocess.Process:
        if config.runtime == "claude-code":
            return await spawn_claude_code(
                config, session_id, stdin_stream_json=True
            )
        if config.runtime == "codex":
            return await spawn_codex(config, session_id, stdin_pipe=True)
        raise ValueError(
            f"unknown runtime {config.runtime!r} in agent.yaml — "
            "supported: 'claude-code', 'codex'"
        )

    return _spawn


def create_app(
    *,
    manager: SessionManager | None = None,
    event_queue_size: int = _DEFAULT_QUEUE_SIZE,
) -> FastAPI:
    """Build a FastAPI app wired to the given :class:`SessionManager`.

    Tests pass an in-memory manager with a stub spawn_fn. Production
    code calls ``create_app()`` with no args and gets the default
    manager (real ``claude`` spawn + stdin pipe).
    """

    mgr = manager or SessionManager(spawn_fn=_default_spawn_fn())
    queues: dict[str, asyncio.Queue[MarsEventBase]] = {}
    pumps: dict[str, _SessionPump] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.session_manager = mgr
        yield
        # Shutdown: cancel pumps, await them to ensure clean exit,
        # then kill any remaining sessions. The await step matters —
        # without it shutdown can leave tasks in a canceling state.
        pump_tasks: list[asyncio.Task[None]] = []
        for pump in pumps.values():
            if pump.task is not None and not pump.task.done():
                pump.task.cancel()
                pump_tasks.append(pump.task)
        if pump_tasks:
            await asyncio.gather(*pump_tasks, return_exceptions=True)
        await mgr.shutdown()

    app = FastAPI(
        title="Mars Runtime Supervisor",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.session_manager = mgr

    # --- helpers ---------------------------------------------------------

    def _require_session(session_id: str) -> SessionHandle:
        handle = mgr.get(session_id)
        if handle is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"session {session_id!r} not found",
            )
        return handle

    async def _parse_body(request: Request) -> dict[str, Any]:
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="empty request body")
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"request body exceeds {_MAX_BODY_BYTES} bytes",
            )
        content_type = (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        try:
            if content_type in ("application/x-yaml", "application/yaml", "text/yaml"):
                data = yaml.safe_load(body)
            else:
                data = json.loads(body)
        except (yaml.YAMLError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid payload: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="payload must be a mapping at the top level"
            )
        return data

    # --- routes ----------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "active_sessions": len(mgr.list())}

    @app.post("/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(request: Request) -> dict[str, Any]:
        data = await _parse_body(request)
        try:
            config = AgentConfig.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        try:
            handle = await mgr.spawn(config)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"spawn failed (binary not found): {exc}",
            ) from exc

        queue: asyncio.Queue[MarsEventBase] = asyncio.Queue(maxsize=event_queue_size)
        queues[handle.session_id] = queue
        pump = _SessionPump(handle, queue, mgr)
        pump.start()
        pumps[handle.session_id] = pump

        return serialize_handle(handle)

    @app.get("/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": [serialize_handle(h) for h in mgr.list()]}

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return serialize_handle(_require_session(session_id))

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        handle = mgr.get(session_id)
        if handle is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"session {session_id!r} not found",
            )
        killed = await mgr.kill(session_id)
        pump = pumps.pop(session_id, None)
        if pump is not None and pump.task is not None and not pump.task.done():
            pump.task.cancel()
            try:
                await pump.task
            except asyncio.CancelledError:
                pass
        queues.pop(session_id, None)
        return {"session_id": session_id, "killed": killed}

    @app.post("/sessions/{session_id}/input")
    async def send_input(session_id: str, payload: InputPayload) -> dict[str, Any]:
        handle = _require_session(session_id)
        proc = handle.process
        if proc.stdin is None:
            raise HTTPException(
                status_code=409,
                detail="session stdin is not pipeable (spawned with DEVNULL)",
            )
        user_event = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": payload.text}],
            },
        }
        line = (json.dumps(user_event) + "\n").encode("utf-8")
        try:
            proc.stdin.write(line)
            await asyncio.wait_for(
                proc.stdin.drain(), timeout=_STDIN_DRAIN_TIMEOUT_S
            )
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise HTTPException(
                status_code=410,
                detail=f"session stdin closed: {exc}",
            ) from exc
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "session stdin drain timed out after "
                    f"{_STDIN_DRAIN_TIMEOUT_S}s — child may be hung"
                ),
            ) from exc
        return {"session_id": session_id, "accepted": True}

    @app.get("/sessions/{session_id}/events")
    async def get_events(session_id: str) -> dict[str, Any]:
        _require_session(session_id)
        queue = queues.get(session_id)
        if queue is None:
            return {"session_id": session_id, "events": []}
        drained: list[dict[str, Any]] = []
        while True:
            try:
                ev = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            drained.append(MARS_EVENT_ADAPTER.dump_python(ev, mode="json"))
        return {"session_id": session_id, "events": drained}

    @app.post("/sessions/{session_id}/permission-response")
    async def permission_response(
        session_id: str, payload: PermissionResponsePayload
    ) -> Response:
        _require_session(session_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "permission round-trip deferred to v1.1 — v1 uses static "
                "allowlist + PreToolUse hooks (see spikes/03-permission-"
                "roundtrip.md)"
            ),
        )

    return app
