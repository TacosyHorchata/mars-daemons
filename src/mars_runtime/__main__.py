"""Entry point — the broker.

Usage:
  python -m mars_runtime <agent.yaml>          # start a new session
  python -m mars_runtime --resume <id>         # resume an existing session
  python -m mars_runtime --list                # list recent sessions (JSON lines)

The broker is the credentialed process. It reads secrets from env,
constructs the LLM client, then spawns a WORKER subprocess with a
scrubbed env (no ANTHROPIC_API_KEY, no AZURE_OPENAI_* etc.) and proxies
every `chat()` call over a JSON-line pipe protocol.

Environment variables named in `AgentConfig.env` are forwarded to the
worker (and only those); all other inherited env is stripped. The
worker has no credential-bearing objects in memory, so reflection and
/proc/self/environ attacks from inside a tool cannot recover the keys.

Data layout (per entorno = per Fly volume):
  $MARS_DATA_DIR/workspace/   — git repo, worker cwd
  $MARS_DATA_DIR/sessions/    — <session_id>.json atomic snapshots

Default $MARS_DATA_DIR: ./.mars-data/
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from pydantic import ValidationError

from . import llm_client, session_store
from ._rpc import response_to_dict
from .schema import AgentConfig
from .session_store import InvalidSessionId


# LLM-provider env vars that must NEVER reach the worker, even if the
# user accidentally declares them in `AgentConfig.env`. This is the list
# of things the registered provider clients (anthropic.py, azure_openai.py,
# openai_direct.py, gemini.py) consume at construction time. Anything else
# the user declares in `env:` is their own workload credential (GITHUB_TOKEN,
# AWS_ACCESS_KEY_ID, DATABASE_URL, ...) and is forwarded normally.
_ALWAYS_STRIP_EXACT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",  # custom base URL — may reveal a private Azure resource host
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "OPENAI_API_VERSION",
        "OPENAI_ORGANIZATION",
        "GOOGLE_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
    }
)


def _data_dir(override: str | None) -> Path:
    raw = override or os.environ.get("MARS_DATA_DIR") or "./.mars-data"
    return Path(raw).resolve()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mars_runtime")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("yaml_path", nargs="?", help="Path to agent.yaml (starts a new session)")
    group.add_argument("--resume", metavar="SESSION_ID", help="Resume an existing session")
    group.add_argument("--list", action="store_true", dest="list_sessions", help="List recent sessions as JSON lines")
    p.add_argument("--data-dir", dest="data_dir", help="Override $MARS_DATA_DIR")
    return p.parse_args(argv)


def _resolve_system_prompt(config: AgentConfig, yaml_path: Path) -> AgentConfig:
    """Resolve system_prompt_path relative to the yaml's own directory."""
    sp = Path(config.system_prompt_path)
    if not sp.is_absolute():
        sp = (yaml_path.parent / sp).resolve()
    return config.model_copy(update={"system_prompt_path": str(sp)})


def _build_worker_env(config: AgentConfig) -> dict[str, str]:
    """Construct the env handed to the worker subprocess.

    Strategy: start from empty, forward only PATH / PYTHONPATH / locale
    plus explicitly-declared `AgentConfig.env` names. Never forward
    secrets matched by the strip lists — even if the user accidentally
    declared them.

    PYTHONPATH is forwarded (and augmented with this package's src root)
    so `python -m mars_runtime._worker` resolves even when mars-runtime
    is not installed site-wide (editable dev, pytest, etc.).
    """
    allowlist = {"PATH", "HOME", "LANG", "LC_ALL", "TZ"}
    clean: dict[str, str] = {}
    for k in allowlist:
        if k in os.environ:
            clean[k] = os.environ[k]

    # src/ parent — `src/mars_runtime/__main__.py` → parents[1] = `src/`.
    pkg_root = str(Path(__file__).resolve().parents[1])
    existing_pp = os.environ.get("PYTHONPATH", "")
    clean["PYTHONPATH"] = (
        f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root
    )

    for name in config.env:
        if name in _ALWAYS_STRIP_EXACT:
            continue
        if name in os.environ:
            clean[name] = os.environ[name]
    return clean


def _spawn_worker(
    config: AgentConfig,
    session_id: str,
    data_dir: Path,
    start_messages: list | None,
) -> subprocess.Popen:
    env = _build_worker_env(config)

    args = [
        sys.executable,
        "-m",
        "mars_runtime._worker",
        "--agent-json",
        json.dumps(config.model_dump()),
        "--session-id",
        session_id,
        "--data-dir",
        str(data_dir),
    ]

    if start_messages is not None:
        # Pass via temp file — argv has size limits and we don't want
        # the entire transcript visible in `ps`.
        fd, path = tempfile.mkstemp(prefix="mars-resume-", suffix=".json", dir=str(data_dir))
        os.close(fd)
        Path(path).write_text(json.dumps(start_messages), encoding="utf-8")
        args.extend(["--start-messages-file", path])

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
    )


_worker_stdin_lock = threading.Lock()


def _send_to_worker(worker: subprocess.Popen, obj: dict) -> None:
    if worker.stdin is None or worker.stdin.closed:
        return
    line = json.dumps(obj) + "\n"
    with _worker_stdin_lock:
        try:
            worker.stdin.write(line)
            worker.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass


def _pump_user_input(worker: subprocess.Popen) -> None:
    """Forward the broker's stdin, line by line, to the worker as RPC."""
    try:
        for raw in sys.stdin:
            line = raw.rstrip("\n")
            if not line:
                continue
            _send_to_worker(worker, {"rpc": "user_input", "text": line})
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        _send_to_worker(worker, {"rpc": "eof"})


def _pump_worker_output(
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
            try:
                resp = llm.chat(**args)
                reply = {
                    "rpc": "chat_response",
                    "id": req_id,
                    "response": response_to_dict(resp),
                }
            except Exception as e:
                # Do NOT forward raw SDK exception strings — some SDKs
                # embed the offending Authorization header or request
                # body (which contains the api_key) in error messages.
                # Log the full detail on broker stderr for operators,
                # and send only the exception type + a generic message
                # to the worker.
                print(
                    f"[broker] LLM call failed ({type(e).__name__}): {e}",
                    file=sys.stderr,
                    flush=True,
                )
                reply = {
                    "rpc": "chat_error",
                    "id": req_id,
                    "error": f"{type(e).__name__} (detail suppressed; see broker stderr)",
                    "type": type(e).__name__,
                }
            _send_to_worker(worker, reply)
        else:
            print(f"[broker] unknown rpc kind: {kind}", file=sys.stderr)


def _harden_broker() -> None:
    """Close artifact-level leaks: deny ptrace, block core dumps.

    - `PR_SET_DUMPABLE(0)` flips the Linux dumpable flag off. Same-UID
      callers can no longer open `/proc/<broker>/mem` or
      `process_vm_readv()` even under relaxed `ptrace_scope`. Also
      sanitizes ownership of /proc/<pid>/* entries to root:root so a
      same-UID worker can't read many of them.
    - `RLIMIT_CORE=0` ensures that if broker ever crashes, the kernel
      does NOT write a core file containing heap memory (which holds
      api_key as a Python string).

    Silent on non-Linux (macOS dev has no prctl PR_SET_DUMPABLE). This
    code path only matters inside the production Docker container.
    """
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass

    try:
        # ctypes call into libc prctl. Python stdlib has no wrapper.
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        PR_SET_DUMPABLE = 4
        rc = libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
        if rc != 0:
            # -1 on failure (unusual libc, seccomp filter, non-Linux). Log
            # to stderr so operators can detect a deployment where the
            # hardening silently didn't apply.
            errno = ctypes.get_errno()
            print(
                f"[broker] warning: PR_SET_DUMPABLE failed rc={rc} errno={errno}. "
                "Same-UID /proc memory reads may not be blocked.",
                file=sys.stderr,
                flush=True,
            )
    except (OSError, AttributeError):
        pass


def _ingest_secrets_fd() -> None:
    """If `_bootstrap` handed us secrets on a pipe FD, drain it and put
    them into `os.environ` so the LLM SDK constructors find them.

    No-op if MARS_SECRETS_FD is unset (local dev / pytest path — broker
    reads os.environ directly like before bootstrap existed).
    """
    fd_str = os.environ.pop("MARS_SECRETS_FD", None)
    if fd_str is None:
        return
    try:
        fd = int(fd_str)
    except ValueError:
        return
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if not data:
        return
    try:
        secrets = json.loads(data)
    except json.JSONDecodeError:
        return
    if not isinstance(secrets, dict):
        return
    for k, v in secrets.items():
        if isinstance(k, str) and isinstance(v, str):
            os.environ[k] = v


def main(argv: list[str] | None = None) -> int:
    _ingest_secrets_fd()
    _harden_broker()

    args = _parse_args(argv if argv is not None else sys.argv[1:])

    data_dir = _data_dir(args.data_dir)
    workspace_path = data_dir / "workspace"
    sessions_dir = data_dir / "sessions"

    if args.list_sessions:
        for entry in session_store.list_recent(sessions_dir):
            print(json.dumps(entry), flush=True)
        return 0

    workspace_path.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)

    if args.resume:
        try:
            data = session_store.load(sessions_dir, args.resume)
        except InvalidSessionId as e:
            print(f"invalid session id: {e}", file=sys.stderr)
            return 2
        except FileNotFoundError:
            print(f"session not found: {args.resume}", file=sys.stderr)
            return 1
        except json.JSONDecodeError as e:
            print(f"session file is corrupt: {e}", file=sys.stderr)
            return 1
        try:
            config = AgentConfig(**data["agent_config"])
        except (KeyError, TypeError, ValidationError) as e:
            print(f"session has invalid agent_config: {e}", file=sys.stderr)
            return 1
        session_id = args.resume
        start_messages = data.get("messages", [])
        if not session_store._is_valid_messages_shape(start_messages):
            print(
                "session has malformed messages array (expected list of "
                "{role, content: list} dicts)",
                file=sys.stderr,
            )
            return 1
    else:
        yaml_path = Path(args.yaml_path).resolve()
        config = AgentConfig.from_yaml_file(yaml_path)
        config = _resolve_system_prompt(config, yaml_path)
        session_id = session_store.new_id()
        start_messages = None

    # Broker owns the LLM client — and therefore the API key. The SDK
    # captures the key into client state at construction. It never
    # enters the worker process.
    llm_client.load_all()
    provider_name = config.provider or llm_client.infer_provider(config.model)
    llm = llm_client.get(provider_name)

    worker = _spawn_worker(config, session_id, data_dir, start_messages)

    input_pump = threading.Thread(target=_pump_user_input, args=(worker,), daemon=True)
    input_pump.start()

    try:
        _pump_worker_output(worker, llm)
    except KeyboardInterrupt:
        worker.terminate()
        return 130
    finally:
        if worker.stdin and not worker.stdin.closed:
            try:
                worker.stdin.close()
            except BrokenPipeError:
                pass

    return worker.wait()


if __name__ == "__main__":
    sys.exit(main())
