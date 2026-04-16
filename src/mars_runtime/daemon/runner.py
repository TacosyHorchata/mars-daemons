from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .. import providers as llm_client
from ..broker.process import handle_chat_request, send_to_worker, spawn_worker
from ..config import AgentConfig
from ..storage import sessions
from . import isolation, replay, turns

_LEGACY_LOCK_KEY = "__legacy__"
_USER_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)
_USER_LOCKS_GUARD = threading.Lock()

_ACTIVE_WORKERS: dict[str, subprocess.Popen[str]] = {}
_ACTIVE_WORKERS_GUARD = threading.Lock()
_CANCELLED_TURNS: set[str] = set()

_DEFAULT_TURN_TIMEOUT_S = 300


def _turn_timeout_s() -> int:
    raw = os.environ.get("MARS_TURN_TIMEOUT_S")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_TURN_TIMEOUT_S


def _register_worker(turn_id: str, worker: subprocess.Popen[str]) -> None:
    with _ACTIVE_WORKERS_GUARD:
        _ACTIVE_WORKERS[turn_id] = worker


def _unregister_worker(turn_id: str) -> None:
    with _ACTIVE_WORKERS_GUARD:
        _ACTIVE_WORKERS.pop(turn_id, None)


def discard_cancelled(turn_id: str) -> None:
    with _ACTIVE_WORKERS_GUARD:
        _CANCELLED_TURNS.discard(turn_id)


def cancel_turn(turn_id: str) -> bool:
    """Best-effort kill of an active worker. Returns True if a worker was signalled."""
    with _ACTIVE_WORKERS_GUARD:
        worker = _ACTIVE_WORKERS.get(turn_id)
        _CANCELLED_TURNS.add(turn_id)
    if worker is None:
        return False
    try:
        worker.terminate()
    except Exception:
        pass
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            worker.kill()
        except Exception:
            pass
    except Exception:
        pass
    return True


def _get_user_lock(owner_subject: str | None) -> threading.Lock:
    key = owner_subject or _LEGACY_LOCK_KEY
    with _USER_LOCKS_GUARD:
        return _USER_LOCKS[key]


def _snapshot_files(root: Path) -> dict[str, float]:
    snap: dict[str, float] = {}
    if not root.exists():
        return snap
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = Path(dirpath) / name
            try:
                mtime = full.stat().st_mtime
            except OSError:
                continue
            rel = full.relative_to(root).as_posix()
            snap[rel] = mtime
    return snap


def _diff_files(before: dict[str, float], after: dict[str, float]) -> list[str]:
    changed: list[str] = []
    for rel, mt in after.items():
        if before.get(rel) != mt:
            changed.append(rel)
    return sorted(changed)

_TERMINAL_TYPES = {"turn_completed", "turn_aborted", "turn_truncated"}


def _queue_event(queue: asyncio.Queue[Any], event: dict[str, Any]) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        print("[daemon] event queue full; closing SSE stream", file=sys.stderr, flush=True)
        close_requested = getattr(queue, "_mars_close_requested", None)
        if isinstance(close_requested, threading.Event):
            close_requested.set()


def _pump_worker_output_to_queue(
    worker: subprocess.Popen[str],
    llm: llm_client.LLMClient,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[Any],
) -> None:
    assert worker.stdout is not None

    for raw in worker.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"[daemon] unparseable worker output: {line!r}", file=sys.stderr, flush=True)
            continue
        if not isinstance(msg, dict):
            print(f"[daemon] non-object worker output: {line!r}", file=sys.stderr, flush=True)
            continue

        kind = msg.get("rpc")
        if kind == "event":
            event = msg["event"]
            on_event = getattr(queue, "_mars_on_event", None)
            if callable(on_event):
                result = on_event(event)
                if result is None:
                    # Lost the terminal race; suppress emission.
                    continue
                if isinstance(result, dict):
                    event = result
            loop.call_soon_threadsafe(_queue_event, queue, event)
        elif kind == "chat_request":
            handle_chat_request(worker, llm, msg["id"], msg["args"])
        else:
            print(f"[daemon] unknown rpc kind: {kind}", file=sys.stderr, flush=True)


def _terminal_result(event: dict[str, Any]) -> tuple[str, str | None] | None:
    kind = event.get("type")
    if kind == "turn_completed":
        return "completed", None
    if kind == "turn_truncated":
        return "completed", None
    if kind == "turn_aborted":
        reason = event.get("reason")
        return "failed", str(reason) if reason else "turn_aborted"
    return None


async def stream_turn(
    *,
    config: AgentConfig,
    data_dir: Path,
    db_path: Path,
    session_id: str,
    turn_id: str,
    text: str,
    owner_subject: str | None = None,
    workspace_path: str | None = None,
    role: str | None = None,
    assistant_id: str | None = None,
) -> AsyncIterator[dict[str, str]]:
    start_messages = sessions.load(data_dir / "sessions", session_id)["messages"]
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=512)
    close_requested = threading.Event()
    worker_done = threading.Event()
    terminal_state: dict[str, str | None] = {}
    setattr(queue, "_mars_close_requested", close_requested)

    if workspace_path is not None:
        ws_path = data_dir / workspace_path
    else:
        ws_path = None

    files_root = ws_path if ws_path is not None else None
    files_before = _snapshot_files(files_root) if files_root is not None else {}
    events_out_dir = data_dir / "session-events"
    terminal_guard = threading.Lock()

    def on_event(event: dict[str, Any]) -> dict[str, Any] | None:
        """Return the (possibly enriched) event if it should be emitted to
        replay/SSE, or None if a prior terminal event already claimed the
        turn and this one is a loser in the race.
        """
        if event.get("type") == "turn_completed" and files_root is not None:
            event = {**event, "files_changed": _diff_files(files_before, _snapshot_files(files_root))}
        result = _terminal_result(event)
        if result is not None:
            with terminal_guard:
                if "state" in terminal_state:
                    return None
                terminal_state["state"] = result[0]
                terminal_state["error"] = result[1]
        try:
            replay.append_event(events_out_dir, session_id, event)
        except Exception as exc:
            print(f"[daemon] replay append failed: {exc}", file=sys.stderr, flush=True)
        return event

    setattr(queue, "_mars_on_event", on_event)
    loop = asyncio.get_running_loop()

    run_as_uid: int | None = None
    run_as_gid: int | None = None
    if owner_subject is not None and os.geteuid() == 0:
        run_as_uid = isolation.resolve_uid(owner_subject)
        run_as_gid = isolation.resolve_gid(role or "user")

    user_lock = _get_user_lock(owner_subject)
    timeout_s = _turn_timeout_s()
    turn_started_at = time.monotonic()
    events_emitted = 0

    def run_turn() -> None:
        conn = turns.connect(db_path)
        worker: subprocess.Popen[str] | None = None
        timer: threading.Timer | None = None
        try:
            with user_lock:
                # First cancel check: if cancel arrived before run_turn
                # started at all, skip the whole pipeline.
                with _ACTIVE_WORKERS_GUARD:
                    already_cancelled = turn_id in _CANCELLED_TURNS
                if already_cancelled:
                    return
                # CAS accepted→running. If this fails, the cancel endpoint
                # already claimed accepted→failed — skip the spawn entirely.
                if not turns.cas_update_state(
                    conn,
                    turn_id=turn_id,
                    from_state="accepted",
                    to_state="running",
                ):
                    return
                llm_client.load_all()
                provider_name = config.provider or llm_client.infer_provider(config.model)
                llm = llm_client.get(provider_name)
                worker = spawn_worker(
                    config,
                    session_id,
                    data_dir,
                    start_messages,
                    workspace_path=ws_path,
                    run_as_uid=run_as_uid,
                    run_as_gid=run_as_gid,
                )
                # Second check + atomic register: closes the race where
                # cancel arrived between the first check and the spawn.
                with _ACTIVE_WORKERS_GUARD:
                    if turn_id in _CANCELLED_TURNS:
                        try:
                            worker.kill()
                        except Exception:
                            pass
                        try:
                            worker.wait(timeout=5)
                        except Exception:
                            pass
                        worker = None
                        return
                    _ACTIVE_WORKERS[turn_id] = worker

                def _timeout_hit() -> None:
                    event = {"type": "turn_aborted", "reason": "timeout"}
                    emitted = on_event(event)
                    try:
                        if worker is not None:
                            worker.kill()
                    except Exception:
                        pass
                    if emitted is not None:
                        loop.call_soon_threadsafe(_queue_event, queue, emitted)

                timer = threading.Timer(timeout_s, _timeout_hit)
                timer.daemon = True
                timer.start()

                send_to_worker(worker, {"rpc": "user_input", "text": text})
                send_to_worker(worker, {"rpc": "eof"})
                pump_thread = threading.Thread(
                    target=_pump_worker_output_to_queue,
                    args=(worker, llm, loop, queue),
                    daemon=True,
                )
                pump_thread.start()
                pump_thread.join()
        except Exception as exc:
            print(f"[daemon] turn runner failed: {exc}", file=sys.stderr, flush=True)
        finally:
            if timer is not None:
                timer.cancel()
            _unregister_worker(turn_id)
            with _ACTIVE_WORKERS_GUARD:
                was_cancelled = turn_id in _CANCELLED_TURNS
                _CANCELLED_TURNS.discard(turn_id)

            # Single decision point for the terminal state. Every branch
            # uses CAS-style updates so a late-starting run_turn cannot
            # overwrite a terminal row already written by /cancel or by
            # an earlier run.
            if "state" in terminal_state:
                # Worker (or timeout) produced a terminal event. Only write
                # if the row is still in a live state.
                new_state = str(terminal_state["state"])
                new_err = terminal_state["error"]
                if not turns.cas_update_state(
                    conn,
                    turn_id=turn_id,
                    from_state="running",
                    to_state=new_state,
                    error=new_err,
                ):
                    turns.cas_update_state(
                        conn,
                        turn_id=turn_id,
                        from_state="accepted",
                        to_state=new_state,
                        error=new_err,
                    )
            elif was_cancelled:
                wrote_running = turns.cas_update_state(
                    conn,
                    turn_id=turn_id,
                    from_state="running",
                    to_state="failed",
                    error="cancelled",
                )
                wrote_accepted = False
                if not wrote_running:
                    wrote_accepted = turns.cas_update_state(
                        conn,
                        turn_id=turn_id,
                        from_state="accepted",
                        to_state="failed",
                        error="cancelled",
                    )
                if wrote_running or wrote_accepted:
                    cancel_event = {"type": "turn_aborted", "reason": "cancelled"}
                    emitted = on_event(cancel_event)
                    if emitted is not None:
                        loop.call_soon_threadsafe(_queue_event, queue, emitted)
            else:
                wrote_running = turns.cas_update_state(
                    conn,
                    turn_id=turn_id,
                    from_state="running",
                    to_state="failed",
                    error="worker_exit",
                )
                wrote_accepted = False
                if not wrote_running:
                    wrote_accepted = turns.cas_update_state(
                        conn,
                        turn_id=turn_id,
                        from_state="accepted",
                        to_state="failed",
                        error="worker_exit",
                    )
                if wrote_running or wrote_accepted:
                    failure_event = {"type": "turn_aborted", "reason": "worker_exit"}
                    emitted = on_event(failure_event)
                    if emitted is not None:
                        loop.call_soon_threadsafe(_queue_event, queue, emitted)
            try:
                if worker is not None and worker.stdin and not worker.stdin.closed:
                    worker.stdin.close()
            except BrokenPipeError:
                pass
            if worker is not None:
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()
            conn.close()
            worker_done.set()

    threading.Thread(target=run_turn, daemon=True).start()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if worker_done.is_set():
                    break
                continue
            if close_requested.is_set():
                break
            yield {"event": "message", "data": json.dumps(event)}
            events_emitted += 1
            if event.get("type") in _TERMINAL_TYPES:
                break
    finally:
        setattr(queue, "_mars_on_event", None)
        setattr(queue, "_mars_close_requested", None)
        duration_s = round(time.monotonic() - turn_started_at, 3)
        final_state = terminal_state.get("state", "unknown")
        log_entry = {
            "event": "turn_terminal",
            "turn_id": turn_id,
            "session_id": session_id,
            "owner_subject": owner_subject,
            "assistant_id": assistant_id,
            "state": final_state,
            "error": terminal_state.get("error"),
            "duration_s": duration_s,
            "events_emitted": events_emitted,
        }
        print(json.dumps(log_entry), file=sys.stderr, flush=True)
