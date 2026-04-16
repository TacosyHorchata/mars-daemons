from __future__ import annotations

import asyncio
import hmac
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from sse_starlette import EventSourceResponse

from ..config import AgentConfig
from ..storage import sessions
from . import isolation, replay, turns
from .resolve import UnknownAssistant, resolve_agent_config

_SESSION_ID_RE = re.compile(r"^sess_[0-9a-f]{24}$")
_TURN_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
# owner_subject is used as a filesystem path component (user-workspaces/{sub}).
# Restrict to characters that cannot traverse or escape: alnum, underscore,
# hyphen, dot (but not `..`), @, colon. Backend contract is to overwrite the
# header from verified identity — validation is defence in depth.
_OWNER_SUBJECT_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_ASSISTANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class TurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    text: str


class SessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_id: str | None = None


_VALID_ROLES = {"user", "admin"}


def require_bearer(request: Request) -> None:
    auth = request.headers.get("authorization")
    if not auth:
        raise HTTPException(status_code=401, detail="missing bearer token")
    scheme, sep, token = auth.partition(" ")
    if sep != " " or scheme != "Bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not hmac.compare_digest(token, request.app.state.bearer):
        raise HTTPException(status_code=401, detail="missing bearer token")

    sub = request.headers.get("x-owner-subject") or None
    if sub is not None:
        if not _OWNER_SUBJECT_RE.fullmatch(sub) or sub in {".", ".."}:
            raise HTTPException(status_code=400, detail="invalid_owner_subject")
    request.state.sub = sub

    role = request.headers.get("x-owner-role", "user")
    if role not in _VALID_ROLES:
        raise HTTPException(status_code=400, detail="invalid_role")
    request.state.role = role


def _sessions_dir(request: Request) -> Path:
    return request.app.state.data_dir / "sessions"


def _require_session_id(session_id: str) -> None:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="session_not_found")


def _check_session_access(
    sessions_dir: Path,
    session_id: str,
    caller_sub: str | None,
) -> dict[str, object]:
    try:
        data = sessions.load(sessions_dir, session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="session_not_found") from exc
    session_owner = data.get("owner_subject")
    if session_owner is not None and caller_sub != session_owner:
        raise HTTPException(status_code=404, detail="session_not_found")
    return data


def _resolve_user_workspace(data_dir: Path, session: dict[str, object]) -> Path:
    rel = session.get("workspace_path")
    if not (isinstance(rel, str) and rel):
        return data_dir / "workspace"
    expected_root = (data_dir / "user-workspaces").resolve()
    candidate = (data_dir / rel).resolve()
    try:
        candidate.relative_to(expected_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="session_not_found") from exc
    return candidate


def _sanitize_upload_filename(raw: str) -> str:
    name = raw.replace("\x00", "").strip()
    name = os.path.basename(name)
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid_filename")
    return name


def _resolve_in_workspace(workspace: Path, relative: str) -> Path:
    if "\x00" in relative:
        raise HTTPException(status_code=400, detail="invalid_path")
    candidate = (workspace / relative).resolve()
    root = workspace.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_path") from exc
    return candidate


def _default_max_upload_bytes() -> int:
    raw = os.environ.get("MARS_MAX_UPLOAD_BYTES")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 50 * 1024 * 1024


def _sse_max_connection_s() -> int:
    raw = os.environ.get("MARS_SSE_MAX_CONNECTION_S")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 900


def _sse_max_concurrent() -> int:
    raw = os.environ.get("MARS_SSE_MAX_CONCURRENT")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 10


_SSE_ACTIVE = 0
_SSE_GUARD = threading.Lock()


def create_app(
    *,
    config: AgentConfig,
    data_dir: Path,
    db_path: Path,
    bearer: str,
) -> FastAPI:
    app = FastAPI()
    app.state.config = config
    app.state.data_dir = data_dir
    app.state.db_path = db_path
    app.state.bearer = bearer

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        checks: dict[str, bool] = {}
        try:
            conn = turns.connect(request.app.state.db_path)
            try:
                conn.execute("SELECT 1").fetchone()
                checks["db"] = True
            finally:
                conn.close()
        except Exception:
            checks["db"] = False
        try:
            probe = request.app.state.data_dir / ".readyz-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            checks["data_dir"] = True
        except Exception:
            checks["data_dir"] = False
        if all(checks.values()):
            return JSONResponse(status_code=200, content={"status": "ready"})
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "checks": checks}
        )

    @app.post("/v1/sessions", dependencies=[Depends(require_bearer)], status_code=201)
    def create_session(
        request: Request,
        body: SessionBody | None = None,
    ) -> dict[str, object]:
        assistant_id = body.assistant_id if body is not None else None
        owner_subject = request.state.sub
        role = request.state.role
        data_dir = request.app.state.data_dir

        workspace_rel: str | None = None
        if owner_subject is not None:
            uid = isolation.resolve_uid(owner_subject)
            gid = isolation.resolve_gid(role)
            try:
                isolation.ensure_user(uid, gid)
            except isolation.UidCollision as exc:
                raise HTTPException(status_code=503, detail="uid_collision") from exc
            try:
                isolation.ensure_workspace(data_dir, owner_subject, uid, gid)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid_owner_subject") from exc
            workspace_rel = f"user-workspaces/{owner_subject}"

        if assistant_id is not None:
            if not _ASSISTANT_ID_RE.fullmatch(assistant_id):
                raise HTTPException(status_code=400, detail="invalid_assistant_id")
            if owner_subject is None:
                raise HTTPException(status_code=400, detail="owner_required_for_assistant")
            user_ws = data_dir / "user-workspaces" / owner_subject
            shared = data_dir / "shared"
            try:
                config = resolve_agent_config(user_ws, shared, assistant_id)
            except UnknownAssistant as exc:
                raise HTTPException(status_code=400, detail="unknown_assistant") from exc
        else:
            config = request.app.state.config
            assistant_id = config.name

        sid = sessions.new_id()
        created_at = int(time.time())
        sessions.save(
            _sessions_dir(request),
            sid,
            config.name,
            config.model_dump(),
            messages=[],
            created_at=created_at,
            owner_subject=owner_subject,
            role=role,
            assistant_id=assistant_id,
            workspace_path=workspace_rel,
        )
        return {"session_id": sid, "created_at": created_at}

    @app.get("/v1/sessions/{session_id}", dependencies=[Depends(require_bearer)])
    def get_session(request: Request, session_id: str) -> dict[str, object]:
        _require_session_id(session_id)
        sd = _sessions_dir(request)
        data = _check_session_access(sd, session_id, request.state.sub)
        session_file = sd / f"{session_id}.json"
        updated_at = int(session_file.stat().st_mtime)
        conn = turns.connect(request.app.state.db_path)
        try:
            status, turn_count = turns.session_status(conn, session_id)
        finally:
            conn.close()
        response: dict[str, object] = {
            "session_id": session_id,
            "status": status,
            "created_at": data["created_at"],
            "updated_at": updated_at,
            "turn_count": turn_count,
        }
        assistant_id = data.get("assistant_id") or data.get("agent_name")
        if assistant_id is not None:
            response["assistant_id"] = assistant_id
        return response

    @app.post(
        "/v1/sessions/{session_id}/messages",
        dependencies=[Depends(require_bearer)],
        response_model=None,
    )
    def post_message(
        request: Request,
        session_id: str,
        body: TurnBody,
    ) -> EventSourceResponse | JSONResponse:
        _require_session_id(session_id)
        if not _TURN_ID_RE.fullmatch(body.turn_id):
            raise HTTPException(status_code=400, detail="invalid_turn_id")
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="text must be non-empty")
        data = _check_session_access(
            _sessions_dir(request), session_id, request.state.sub,
        )
        persisted_config = data.get("agent_config")
        if isinstance(persisted_config, dict):
            session_config = AgentConfig.model_validate(persisted_config)
        else:
            session_config = request.app.state.config
        owner_subject = data.get("owner_subject")
        workspace_path = data.get("workspace_path")
        conn = turns.connect(request.app.state.db_path)
        try:
            created, state = turns.create_exclusive(
                conn,
                turn_id=body.turn_id,
                session_id=session_id,
            )
        finally:
            conn.close()
        if not created:
            if state == "session_busy":
                return JSONResponse(status_code=409, content={"error": "session_busy"})
            return JSONResponse(
                status_code=409,
                content={"turn_id": body.turn_id, "state": state},
            )
        from .runner import stream_turn

        session_assistant = data.get("assistant_id") or data.get("agent_name")
        return EventSourceResponse(
            stream_turn(
                config=session_config,
                data_dir=request.app.state.data_dir,
                db_path=request.app.state.db_path,
                session_id=session_id,
                turn_id=body.turn_id,
                text=body.text,
                owner_subject=owner_subject if isinstance(owner_subject, str) else None,
                workspace_path=workspace_path if isinstance(workspace_path, str) else None,
                role=data.get("role") if isinstance(data.get("role"), str) else None,
                assistant_id=session_assistant if isinstance(session_assistant, str) else None,
            )
        )

    @app.post(
        "/v1/sessions/{session_id}/turns/{turn_id}/cancel",
        dependencies=[Depends(require_bearer)],
    )
    def cancel_turn_endpoint(
        request: Request,
        session_id: str,
        turn_id: str,
    ) -> dict[str, object]:
        _require_session_id(session_id)
        if not _TURN_ID_RE.fullmatch(turn_id):
            raise HTTPException(status_code=404, detail="turn_not_found")
        _check_session_access(
            _sessions_dir(request), session_id, request.state.sub,
        )
        conn = turns.connect(request.app.state.db_path)
        try:
            row = turns.load_turn(conn, turn_id)
            if row is None or row[0] != session_id:
                raise HTTPException(status_code=404, detail="turn_not_found")
            current_state = row[1]
            if current_state in ("completed", "failed"):
                return {"turn_id": turn_id, "state": current_state}
            from .runner import cancel_turn, discard_cancelled

            # cancel_turn() sets _CANCELLED_TURNS and (if a worker is
            # registered) sends SIGTERM.
            worker_signalled = cancel_turn(turn_id)
            if worker_signalled:
                # A worker is running — run_turn's finalizer owns the
                # terminal write + replay event (single-sourced). Avoids
                # racing with the worker's own turn_completed.
                row = turns.load_turn(conn, turn_id)
                state = row[1] if row is not None else "accepted"
                return {"turn_id": turn_id, "state": state}

            # No worker registered. Attempt an atomic CAS accepted→failed.
            # If we win: the endpoint owns the terminal write + replay
            # event, and we clear the cancel flag so a late-starting
            # run_turn (its accepted→running CAS will also fail) doesn't
            # double-emit. If we lose: the turn is already running/terminal
            # and _CANCELLED_TURNS stays set for run_turn's finalizer.
            claimed = turns.cas_update_state(
                conn,
                turn_id=turn_id,
                from_state="accepted",
                to_state="failed",
                error="cancelled",
            )
            if claimed:
                try:
                    replay.append_event(
                        request.app.state.data_dir / "session-events",
                        session_id,
                        {
                            "type": "turn_aborted",
                            "reason": "cancelled",
                            "turn_id": turn_id,
                        },
                    )
                except Exception:
                    pass
                discard_cancelled(turn_id)
                return {"turn_id": turn_id, "state": "failed"}
            row = turns.load_turn(conn, turn_id)
            state = row[1] if row is not None else "accepted"
            return {"turn_id": turn_id, "state": state}
        finally:
            conn.close()

    @app.post(
        "/v1/sessions/{session_id}/files",
        dependencies=[Depends(require_bearer)],
        status_code=201,
    )
    async def upload_file(
        request: Request,
        session_id: str,
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        _require_session_id(session_id)
        session = _check_session_access(
            _sessions_dir(request), session_id, request.state.sub,
        )
        if not file.filename:
            raise HTTPException(status_code=400, detail="invalid_filename")
        clean = _sanitize_upload_filename(file.filename)

        ws = _resolve_user_workspace(request.app.state.data_dir, session)
        uploads = ws / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        target = _resolve_in_workspace(ws, f"uploads/{clean}")

        max_bytes = _default_max_upload_bytes()
        fd, tmp_path = tempfile.mkstemp(prefix=".upload-", dir=str(uploads))
        total = 0
        try:
            with os.fdopen(fd, "wb") as sink:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(status_code=413, detail="file_too_large")
                    sink.write(chunk)
            os.replace(tmp_path, target)
        except HTTPException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        owner = session.get("owner_subject")
        role = session.get("role")
        if isinstance(owner, str) and os.geteuid() == 0:
            uid = isolation.resolve_uid(owner)
            gid = isolation.resolve_gid(role if isinstance(role, str) else "user")
            try:
                os.chown(target, uid, gid)
            except OSError:
                pass

        return {"path": f"uploads/{clean}"}

    @app.get(
        "/v1/sessions/{session_id}/events",
        dependencies=[Depends(require_bearer)],
    )
    def session_events(
        request: Request,
        session_id: str,
        after: int = Query(default=0, ge=0),
    ) -> EventSourceResponse:
        global _SSE_ACTIVE
        _require_session_id(session_id)
        _check_session_access(
            _sessions_dir(request), session_id, request.state.sub,
        )
        events_dir = request.app.state.data_dir / "session-events"
        max_conn = _sse_max_concurrent()
        max_life = _sse_max_connection_s()
        with _SSE_GUARD:
            if _SSE_ACTIVE >= max_conn:
                raise HTTPException(status_code=429, detail="too_many_connections")
            _SSE_ACTIVE += 1

        async def _gen():
            try:
                started = time.monotonic()
                last_seq = after
                # Initial replay.
                for obj in replay.replay_after(events_dir, session_id, last_seq):
                    seq = obj.get("sequence")
                    if isinstance(seq, int):
                        last_seq = max(last_seq, seq)
                    yield {"event": "message", "data": json.dumps(obj)}
                # Live-tail by polling the JSONL.
                while True:
                    if time.monotonic() - started > max_life:
                        break
                    await asyncio.sleep(0.25)
                    for obj in replay.replay_after(events_dir, session_id, last_seq):
                        seq = obj.get("sequence")
                        if isinstance(seq, int):
                            last_seq = max(last_seq, seq)
                        yield {"event": "message", "data": json.dumps(obj)}
            finally:
                global _SSE_ACTIVE
                with _SSE_GUARD:
                    _SSE_ACTIVE -= 1

        return EventSourceResponse(_gen())

    @app.get(
        "/v1/sessions/{session_id}/files/{path:path}",
        dependencies=[Depends(require_bearer)],
    )
    def download_file(
        request: Request,
        session_id: str,
        path: str,
    ) -> FileResponse:
        _require_session_id(session_id)
        session = _check_session_access(
            _sessions_dir(request), session_id, request.state.sub,
        )
        ws = _resolve_user_workspace(request.app.state.data_dir, session)
        target = _resolve_in_workspace(ws, path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file_not_found")
        mime, _ = mimetypes.guess_type(target.name)
        return FileResponse(
            path=str(target),
            media_type=mime or "application/octet-stream",
            filename=target.name,
        )

    return app
