"""Standalone conversations router: CRUD, turn dispatch, SSE, attachments."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Protocol, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import UploadFile

from ..core.events import (
    EVENT_CONVERSATION_STATE,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_ERROR,
    get_sink,
    publish_durable_event,
)
from ..core.exceptions import AuthenticationError, OrgScopingError
from ..core.loop import run_turn
from ..core.pruning import build_turn_error_message, ensure_turn_error_message
from ..core.state import restore_state
from ..core.store import ConversationContext, PersistedState, UsageMetrics, get_store
from ..core.tools import AuthContext
from .models import (
    ConversationDetailResponse,
    ConversationListResponse,
    CreateConversationRequest,
    PaginationResponse,
    SendMessageRequest,
    TurnResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["mars-runtime"])

_SSE_HEARTBEAT_INTERVAL = 30.0
_SSE_IDLE_TIMEOUT = 300.0
_STREAM_TERMINAL_EVENTS = frozenset({EVENT_TURN_COMPLETED, EVENT_TURN_ERROR})
_SSE_CONNECT_FLUSH = ":ping\n\n"
_SSE_HEARTBEAT_FRAME = ":ping\n\n"
_SSE_DISCONNECT_POLL_INTERVAL = 0.25

CONVERSATIONS_PER_PAGE = 20

_auth_provider: Any | None = None
_file_store: Any | None = None
_workspace_store: Any | None = None
_max_file_size: int = 10 * 1024 * 1024
_allowed_mimetypes: set[str] = set()
_default_agent_prompt = ""

_active_turns: dict[str, asyncio.Task] = {}
_cancel_locks: dict[str, asyncio.Lock] = {}


def configure_router(
    *,
    auth_provider: Any,
    default_agent_prompt: str = "",
    file_store: Any | None = None,
    workspace_store: Any | None = None,
    max_file_size: int = 10 * 1024 * 1024,
    allowed_mimetypes: set[str] | None = None,
) -> None:
    global _auth_provider, _file_store, _workspace_store, _max_file_size, _allowed_mimetypes, _default_agent_prompt
    _auth_provider = auth_provider
    _default_agent_prompt = default_agent_prompt
    _file_store = file_store
    _workspace_store = workspace_store
    _max_file_size = max_file_size
    _allowed_mimetypes = allowed_mimetypes or set()


class _StreamingSink(Protocol):
    def subscribe(self, conversation_id: str) -> asyncio.Queue: ...
    def unsubscribe(self, conversation_id: str, queue: asyncio.Queue) -> None: ...


async def _authenticate(request: Request) -> AuthContext:
    if _auth_provider is None:
        raise HTTPException(status_code=503, detail="Auth provider not configured")
    try:
        return await _auth_provider.authenticate(request)
    except AuthenticationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _get_cancel_lock(conversation_id: str) -> asyncio.Lock:
    lock = _cancel_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _cancel_locks[conversation_id] = lock
    return lock


def _register_turn_task(conversation_id: str, task: asyncio.Task) -> None:
    prior = _active_turns.get(conversation_id)
    if prior and not prior.done():
        logger.warning("Replacing active turn task for %s (prior still running)", conversation_id)
    _active_turns[conversation_id] = task

    def _cleanup(_task: asyncio.Task) -> None:
        if _active_turns.get(conversation_id) is task:
            _active_turns.pop(conversation_id, None)

    task.add_done_callback(_cleanup)


async def _safe_run_turn(**kwargs: Any) -> None:
    conversation_id = kwargs.get("conversation_id")
    org_id = kwargs.get("org_id", "") or ""

    try:
        await run_turn(**kwargs)
    except asyncio.CancelledError:
        logger.info("Turn cancelled for conversation %s", conversation_id)
        return
    except OrgScopingError:
        logger.error("Org scoping error for conversation %s", conversation_id, exc_info=True)
        return
    except Exception as exc:
        if not conversation_id:
            return
        logger.error("turn_error for %s: %s", conversation_id, exc, exc_info=True)
        try:
            store = get_store()
            persisted = await store.load(conversation_id, org_id=org_id)
            if persisted:
                state = restore_state(persisted.context, "", org_id)
                state["usage"] = persisted.usage
            else:
                state = {
                    "messages": [],
                    "tool_calls": [],
                    "conversation": [],
                    "scratchpad": {},
                    "files": [],
                    "usage": UsageMetrics(),
                    "_event_sequence": 0,
                    "_durable_events": [],
                    "active_skills": [],
                    "org_id": org_id,
                }
            state["conversation_id"] = conversation_id
            state["status"] = "error"
            user_safe_error = build_turn_error_message(exc)
            ensure_turn_error_message(state, user_safe_error)
            await publish_durable_event(
                conversation_id,
                EVENT_TURN_ERROR,
                state,
                error=user_safe_error,
            )
            return
        except Exception:
            logger.error("Failed to persist durable turn_error for %s", conversation_id, exc_info=True)

        try:
            await get_store().update_status(conversation_id, "error", org_id=org_id)
        except Exception:
            logger.error("Failed to update status for %s", conversation_id, exc_info=True)


def _get_streaming_sink() -> _StreamingSink | None:
    sink = get_sink()
    subscribe = getattr(sink, "subscribe", None)
    unsubscribe = getattr(sink, "unsubscribe", None)
    if callable(subscribe) and callable(unsubscribe):
        return cast(_StreamingSink, sink)
    return None


def _format_sse_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("type", "message"))
    sequence = event.get("sequence")
    payload = json.dumps(event, default=str)
    data_lines = payload.splitlines() or [payload]
    data = "".join(f"data: {line}\n" for line in data_lines)
    id_line = f"id: {sequence}\n" if sequence is not None else ""
    return f"{id_line}event: {event_type}\n{data}\n"


async def _parse_request_payload(request: Request, *, model_cls: type) -> tuple[Any, list[UploadFile]]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        payload = {key: value for key, value in form.multi_items() if not isinstance(value, UploadFile)}
        uploads = [file for file in form.getlist("files") if isinstance(file, UploadFile) and file.filename]
    else:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="Invalid request payload") from exc
        uploads = []

    try:
        return model_cls.model_validate(payload), uploads
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid request payload") from exc


def _normalize_mimetype(mimetype: str | None) -> str:
    normalized = (mimetype or "application/octet-stream").split(";", 1)[0].strip().lower()
    return normalized or "application/octet-stream"


async def _upload_files(conversation_id: str, uploads: list[UploadFile], *, org_id: str) -> list[dict[str, Any]]:
    if not uploads or _file_store is None:
        return []

    from .stores.file_store import FileSizeExceededError

    refs: list[dict[str, Any]] = []
    for upload in uploads:
        raw_filename = upload.filename or "upload.bin"
        mimetype = _normalize_mimetype(upload.content_type)
        try:
            if _allowed_mimetypes and mimetype not in _allowed_mimetypes:
                raise HTTPException(
                    status_code=415,
                    detail=f"File '{raw_filename}' has unsupported MIME type '{mimetype}'",
                )
            try:
                stream_upload = getattr(_file_store, "upload_file", None)
                if callable(stream_upload):
                    file_ref = await stream_upload(
                        conversation_id,
                        raw_filename,
                        upload,
                        mimetype,
                        max_size=_max_file_size,
                    )
                else:
                    content = await upload.read()
                    if len(content) > _max_file_size:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File '{raw_filename}' exceeds max size of {_max_file_size} bytes",
                        )
                    file_ref = await _file_store.upload(conversation_id, raw_filename, content, mimetype)
            except FileSizeExceededError as exc:
                raise HTTPException(status_code=413, detail=exc.detail) from exc
            refs.append(file_ref.to_dict())
        finally:
            await upload.close()
    return refs


def _build_file_metadata(file_refs: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"filename": f["filename"], "mimetype": f["mimetype"]} for f in file_refs]


async def _claim_turn(
    conversation_id: str,
    *,
    org_id: str,
    conversation: dict[str, Any],
    allowed_statuses: tuple[str, ...],
) -> None:
    store = get_store()
    claim_fn = getattr(store, "claim_turn", None)
    if not callable(claim_fn):
        raise RuntimeError("ConversationStore must implement 'claim_turn'")
    claimed = await claim_fn(
        conversation_id,
        org_id=org_id,
        expected_last_message_at=str(conversation.get("last_message_at") or ""),
        allowed_statuses=allowed_statuses,
    )
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail="A turn is already being processed for this conversation. Reload state before retrying.",
        )


@router.post("/conversations", response_model=TurnResponse)
async def create_conversation(request: Request):
    auth = await _authenticate(request)
    payload, uploads = await _parse_request_payload(request, model_cls=CreateConversationRequest)

    store = get_store()
    conversation_id = await store.create(auth.org_id, auth.user_id or "anonymous")
    try:
        uploaded_files = await _upload_files(conversation_id, uploads, org_id=auth.org_id)
    except Exception:
        await store.update_status(conversation_id, "error", org_id=auth.org_id)
        raise

    seeded_context = ConversationContext(
        messages=[],
        tool_calls=[],
        conversation=[
            {
                "role": "user",
                "content": payload.message,
                "type": "user_message",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **({"files": _build_file_metadata(uploaded_files)} if uploaded_files else {}),
            },
        ],
        scratchpad={},
        files=uploaded_files,
        system_prompt=_default_agent_prompt,
        active_skills=[],
        _event_sequence=0,
    )
    try:
        await store.save(
            conversation_id,
            PersistedState(
                context=seeded_context,
                status="working",
                usage=UsageMetrics(),
                last_message_at=datetime.now(timezone.utc).isoformat(),
            ),
            org_id=auth.org_id,
        )
    except Exception:
        await store.update_status(conversation_id, "error", org_id=auth.org_id)
        raise

    task = asyncio.create_task(
        _safe_run_turn(
            conversation_id=conversation_id,
            agent_prompt=_default_agent_prompt,
            user_message=payload.message,
            files=uploaded_files,
            org_id=auth.org_id,
            bearer_token=auth.bearer_token,
            user_id=auth.user_id,
            is_new_conversation=True,
            agent_id="default",
        ),
    )
    _register_turn_task(conversation_id, task)
    return TurnResponse(conversation_id=conversation_id, status="processing")


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(request: Request, page: int = Query(default=1, ge=1)):
    auth = await _authenticate(request)
    store = get_store()
    data = await store.list_conversations(auth.org_id, page, created_by=auth.user_id)
    return ConversationListResponse(
        data=data,
        pagination=PaginationResponse(page=page, limit=CONVERSATIONS_PER_PAGE, count=len(data)),
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(conversation_id: str, request: Request):
    auth = await _authenticate(request)
    store = get_store()
    conversation = await store.get(conversation_id, org_id=auth.org_id, created_by=auth.user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetailResponse.model_validate(conversation)


@router.post("/conversations/{conversation_id}/messages", response_model=TurnResponse)
async def send_message(conversation_id: str, request: Request):
    auth = await _authenticate(request)
    payload, uploads = await _parse_request_payload(request, model_cls=SendMessageRequest)

    store = get_store()
    conversation = await store.get(conversation_id, org_id=auth.org_id, created_by=auth.user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    status = conversation.get("status", "idle")
    if status == "working":
        raise HTTPException(
            status_code=409,
            detail="Agent is still processing. Please wait for the current turn to complete.",
        )
    if status not in {"idle", "error"}:
        raise HTTPException(
            status_code=409,
            detail=f'Cannot send message when conversation is in "{status}" status',
        )

    await _claim_turn(
        conversation_id,
        org_id=auth.org_id,
        conversation=conversation,
        allowed_statuses=(str(status),),
    )

    try:
        uploaded_files = await _upload_files(conversation_id, uploads, org_id=auth.org_id)
    except Exception:
        await store.update_status(conversation_id, str(status), org_id=auth.org_id)
        raise

    if uploaded_files:
        persisted = await store.load(conversation_id, org_id=auth.org_id)
        if persisted:
            persisted.context.files = [*persisted.context.files, *uploaded_files]
            persisted.status = "working"
            persisted.last_message_at = datetime.now(timezone.utc).isoformat()
            await store.save(conversation_id, persisted, org_id=auth.org_id)
    else:
        await store.update_status(conversation_id, "working", org_id=auth.org_id)

    task = asyncio.create_task(
        _safe_run_turn(
            conversation_id=conversation_id,
            agent_prompt=_default_agent_prompt,
            user_message=payload.message,
            files=uploaded_files,
            org_id=auth.org_id,
            bearer_token=auth.bearer_token,
            user_id=auth.user_id,
            agent_id="default",
        ),
    )
    _register_turn_task(conversation_id, task)
    return TurnResponse(conversation_id=conversation_id, status="processing")


@router.post("/conversations/{conversation_id}/cancel")
async def cancel_turn(conversation_id: str, request: Request):
    auth = await _authenticate(request)
    store = get_store()
    conversation = await store.get(conversation_id, org_id=auth.org_id, created_by=auth.user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    async with _get_cancel_lock(conversation_id):
        task = _active_turns.get(conversation_id)
        if not task or task.done():
            current = await store.get(conversation_id, org_id=auth.org_id, created_by=auth.user_id)
            return {"status": (current or conversation).get("status", "idle"), "cancelled": False}

        target_task = task
        target_task.cancel()
        try:
            await asyncio.wait_for(target_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.warning("Task await after cancel raised non-cancel exception for %s", conversation_id, exc_info=True)

        current_task = _active_turns.get(conversation_id)
        if current_task is not None and current_task is not target_task and not current_task.done():
            return {"status": "working", "cancelled": True}

        await store.update_status(conversation_id, "error", org_id=auth.org_id)
        return {"status": "error", "cancelled": True}


@router.get("/conversations/{conversation_id}/events")
async def stream_events(
    conversation_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
):
    auth = await _authenticate(request)
    store = get_store()
    conversation = await store.get(conversation_id, org_id=auth.org_id, created_by=auth.user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    sink = _get_streaming_sink()
    if sink is None:
        raise HTTPException(status_code=501, detail="Event sink does not support streaming.")

    queue = sink.subscribe(conversation_id)
    last_event_id = request.headers.get("Last-Event-ID")
    replay_after_sequence = after_sequence
    if last_event_id:
        try:
            replay_after_sequence = max(replay_after_sequence, int(last_event_id))
        except ValueError:
            pass

    replay_events: list[dict[str, Any]] = []
    last_sequence = replay_after_sequence
    terminal_after_replay = False

    if replay_after_sequence > 0:
        persisted = await store.load(conversation_id, org_id=auth.org_id)
        if persisted is not None:
            list_durable_fn = getattr(store, "list_durable_events", None)
            if callable(list_durable_fn):
                replay_events = await list_durable_fn(
                    conversation_id,
                    after_sequence=replay_after_sequence,
                    org_id=auth.org_id,
                )
                if replay_events:
                    first_sequence = int(replay_events[0].get("sequence", 0) or 0)
                    if first_sequence != replay_after_sequence + 1:
                        replay_events = [_build_state_snapshot_event(conversation_id, persisted)]
                elif persisted.context._event_sequence > replay_after_sequence:
                    replay_events = [_build_state_snapshot_event(conversation_id, persisted)]

                if replay_events:
                    last_sequence = max(
                        int(event.get("sequence", last_sequence) or last_sequence)
                        for event in replay_events
                    )
                    terminal_after_replay = any(
                        event.get("type") in _STREAM_TERMINAL_EVENTS
                        or (
                            event.get("type") == EVENT_CONVERSATION_STATE
                            and event.get("status") in {"idle", "error"}
                        )
                        for event in replay_events
                    )

    async def event_generator() -> AsyncGenerator[str, None]:
        nonlocal last_sequence
        try:
            last_event_time = time.monotonic()
            last_heartbeat_time = time.monotonic()
            yield _SSE_CONNECT_FLUSH
            for replay_event in replay_events:
                yield _format_sse_event(replay_event)
            if terminal_after_replay:
                return
            while True:
                now = time.monotonic()
                if now - last_event_time >= _SSE_IDLE_TIMEOUT:
                    return
                try:
                    event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    if await request.is_disconnected():
                        return
                    if now - last_heartbeat_time >= _SSE_HEARTBEAT_INTERVAL:
                        last_heartbeat_time = now
                        yield _SSE_HEARTBEAT_FRAME
                    await asyncio.sleep(max(min(
                        _SSE_DISCONNECT_POLL_INTERVAL,
                        _SSE_HEARTBEAT_INTERVAL,
                        _SSE_IDLE_TIMEOUT,
                    ), 0.01))
                    continue

                if await request.is_disconnected():
                    return

                event_sequence = event.get("sequence")
                if event_sequence is not None and int(event_sequence) <= last_sequence:
                    continue
                if event_sequence is not None:
                    last_sequence = int(event_sequence)

                last_event_time = time.monotonic()
                last_heartbeat_time = last_event_time

                if event.get("type") == "_gap":
                    return

                yield _format_sse_event(event)
                if event.get("type") in _STREAM_TERMINAL_EVENTS:
                    return
        except asyncio.CancelledError:
            return
        finally:
            sink.unsubscribe(conversation_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/files/{file_key:path}")
async def download_file(file_key: str, request: Request):
    await _authenticate(request)
    if _file_store is None:
        raise HTTPException(status_code=404, detail="File storage is not configured")
    try:
        path, media_type = await _file_store.open(file_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/conversations/{conversation_id}/workspace/{path:path}")
async def download_workspace_file(conversation_id: str, path: str, request: Request):
    auth = await _authenticate(request)
    if _workspace_store is None:
        raise HTTPException(status_code=404, detail="Workspace storage is not configured")
    try:
        file_path, media_type = await _workspace_store.open_file(
            auth.org_id,
            auth.user_id or "anonymous",
            conversation_id,
            path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Workspace file not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(file_path, media_type=media_type, filename=file_path.name)


def _build_state_snapshot_event(conversation_id: str, persisted: PersistedState) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "type": EVENT_CONVERSATION_STATE,
        "sequence": persisted.context._event_sequence,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": persisted.status,
        "context": {
            "conversation": persisted.context.conversation,
            "files": persisted.context.files,
            "system_prompt": persisted.context.system_prompt,
        },
        "usage": {
            "llm_calls": persisted.usage.llm_calls,
            "input_tokens": persisted.usage.input_tokens,
            "output_tokens": persisted.usage.output_tokens,
            "tool_calls": persisted.usage.tool_calls,
        },
    }
