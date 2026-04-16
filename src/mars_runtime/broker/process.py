"""Broker-side worker lifecycle: spawn + bidirectional RPC pumps.

Once the broker has constructed the LLM client and scrubbed the worker
env, this module:

1. `spawn_worker()` — Popen's `python -m mars_runtime.worker` with
   clean env, stdin/stdout pipes, and the resume-transcript handed on
   a temp-file pointer if present.
2. `pump_user_input()` — daemon thread that forwards the broker's
   stdin (one line = one user turn) to the worker as RPC messages.
3. `pump_worker_output()` — reads worker stdout JSON-line RPCs,
   forwards `event` messages to broker stdout (so the user still sees
   events), and routes `chat_request` messages into `handle_chat_request`.
4. `handle_chat_request()` — iterates llm.chat_stream() and forwards
   each chunk as `chat_chunk` RPC, ending with `chat_response` or
   `chat_error` (with SDK exception text suppressed — may embed api_key).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .. import providers as llm_client
from .._rpc import chunk_to_wire, response_to_dict
from ..config import AgentConfig
from .env import build_worker_env


# One lock serializes writes to the worker's stdin: the input-pump thread
# and the main RPC-handler thread both send JSON lines into the same pipe.
# Without this, a user_input message and a chat_response can interleave
# bytes and corrupt the line-framed protocol.
_worker_stdin_lock = threading.Lock()


def send_to_worker(worker: subprocess.Popen, obj: dict) -> None:
    if worker.stdin is None or worker.stdin.closed:
        return
    line = json.dumps(obj) + "\n"
    with _worker_stdin_lock:
        try:
            worker.stdin.write(line)
            worker.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass


def spawn_worker(
    config: AgentConfig,
    session_id: str,
    data_dir: Path,
    start_messages: list | None,
    *,
    workspace_path: Path | None = None,
    run_as_uid: int | None = None,
    run_as_gid: int | None = None,
) -> subprocess.Popen:
    env = build_worker_env(config)

    args = [
        sys.executable,
        "-m",
        "mars_runtime.worker",
        "--agent-json",
        json.dumps(config.model_dump()),
        "--session-id",
        session_id,
        "--data-dir",
        str(data_dir),
    ]

    if workspace_path is not None:
        args.extend(["--workspace-path", str(workspace_path)])

    if start_messages is not None:
        # Pass via temp file — argv has size limits and we don't want
        # the entire transcript visible in `ps`.
        fd, path = tempfile.mkstemp(prefix="mars-resume-", suffix=".json", dir=str(data_dir))
        os.close(fd)
        Path(path).write_text(json.dumps(start_messages), encoding="utf-8")
        if run_as_uid is not None:
            try:
                os.chown(path, run_as_uid, run_as_gid if run_as_gid is not None else -1)
            except OSError:
                pass
        args.extend(["--start-messages-file", path])

    preexec_fn = None
    if run_as_uid is not None or run_as_gid is not None:
        uid_for_groups = run_as_uid

        def _drop_privileges() -> None:
            # setgid MUST happen before setuid. initgroups must succeed — if
            # the passwd entry is missing, the worker could keep inherited
            # supplementary groups (e.g., root's), weakening isolation. Fail
            # loud so the caller sees a non-zero child exit immediately.
            if run_as_gid is not None:
                os.setgid(run_as_gid)
                if uid_for_groups is not None:
                    os.initgroups(f"mars_u{uid_for_groups}", run_as_gid)
            if run_as_uid is not None:
                os.setuid(run_as_uid)

        preexec_fn = _drop_privileges

    return subprocess.Popen(
        args,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # stderr=None inherits the parent's real FD (2). Passing
        # sys.stderr here would break when the caller replaced it
        # with a non-file stream (e.g., pytest's capsys).
        stderr=None,
        bufsize=1,
        text=True,
        preexec_fn=preexec_fn,
    )


def pump_user_input(worker: subprocess.Popen) -> None:
    """Forward the broker's stdin, line by line, to the worker as RPC."""
    try:
        for raw in sys.stdin:
            line = raw.rstrip("\n")
            if not line:
                continue
            send_to_worker(worker, {"rpc": "user_input", "text": line})
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        send_to_worker(worker, {"rpc": "eof"})


def handle_chat_request(
    worker: subprocess.Popen,
    llm: "llm_client.LLMClient",
    req_id: int,
    args: dict,
) -> None:
    """Drive a single chat_stream() call, forwarding every chunk as an
    RPC message. Ends with either a chat_response (success, carries the
    final Response) or a chat_error (SDK/network failure).

    Raw SDK exception strings are NOT forwarded — some SDKs embed the
    offending Authorization header or request body (which contains the
    api_key) in error messages. Full detail goes to broker stderr for
    operators; the worker sees only the exception type.
    """
    try:
        stream = llm.chat_stream(**args)
        final_response = None
        final_stop_reason = None
        for chunk in stream:
            if chunk.kind == "message_stop":
                final_response = chunk.final_response
                final_stop_reason = chunk.stop_reason
                break
            send_to_worker(
                worker,
                {"rpc": "chat_chunk", "id": req_id, "chunk": chunk_to_wire(chunk)},
            )
        send_to_worker(
            worker,
            {
                "rpc": "chat_response",
                "id": req_id,
                "response": response_to_dict(final_response) if final_response else None,
                "stop_reason": final_stop_reason,
            },
        )
    except Exception as e:
        print(
            f"[broker] LLM call failed ({type(e).__name__}): {e}",
            file=sys.stderr,
            flush=True,
        )
        send_to_worker(
            worker,
            {
                "rpc": "chat_error",
                "id": req_id,
                "error": f"{type(e).__name__} (detail suppressed; see broker stderr)",
                "type": type(e).__name__,
            },
        )


def pump_worker_output(
    worker: subprocess.Popen,
    llm: "llm_client.LLMClient",
) -> None:
    """Read RPC messages from the worker and service them."""
    assert worker.stdout is not None

    for raw in worker.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # Malformed line from worker — pass-through to our stderr for debug.
            print(f"[broker] unparseable worker output: {line!r}", file=sys.stderr)
            continue
        if not isinstance(msg, dict):
            print(f"[broker] non-object worker output: {line!r}", file=sys.stderr)
            continue

        kind = msg.get("rpc")
        if kind == "event":
            # Forward to our stdout unchanged.
            sys.stdout.write(json.dumps(msg["event"]) + "\n")
            sys.stdout.flush()
        elif kind == "chat_request":
            req_id = msg["id"]
            args = msg["args"]
            handle_chat_request(worker, llm, req_id, args)
        else:
            print(f"[broker] unknown rpc kind: {kind}", file=sys.stderr)
