"""Entry point — the broker + user-side file CLI.

Session usage:
  python -m mars_runtime <agent.yaml>          # start a new session
  python -m mars_runtime --resume <id>         # resume an existing session
  python -m mars_runtime --list                # list recent sessions (JSON lines)

File transfer (host ↔ sandbox, USER only — the agent has no access):
  python -m mars_runtime push <local> <dest>   # copy local file into workspace
  python -m mars_runtime pull <src> <local>    # copy workspace file out

  <dest> / <src> either:
    - a path relative to the entorno workspace (local bind-mount)
    - fly://<app-name>/<path-relative-to-/data/workspace>

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
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from pydantic import ValidationError

from . import llm_client, session_store
from ._rpc import chunk_to_wire, response_to_dict
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


def _handle_chat_request(
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
            _send_to_worker(
                worker,
                {"rpc": "chat_chunk", "id": req_id, "chunk": chunk_to_wire(chunk)},
            )
        _send_to_worker(
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
        _send_to_worker(
            worker,
            {
                "rpc": "chat_error",
                "id": req_id,
                "error": f"{type(e).__name__} (detail suppressed; see broker stderr)",
                "type": type(e).__name__,
            },
        )


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
            _handle_chat_request(worker, llm, req_id, args)
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


# ---------------------------------------------------------------------------
# File transfer — user pushes/pulls files into/out of the entorno workspace.
# This surface is NOT available to the agent. It is a host-side CLI only.
# ---------------------------------------------------------------------------

_FLY_URL_RE = re.compile(r"^fly://([a-z0-9][a-z0-9-]*)/(.+)$")

# Basenames the agent must not overwrite via its own tools; also prevented
# on push so a user-supplied file cannot silently replace agent config.
_PROTECTED_BASENAMES = frozenset({"CLAUDE.md", "AGENTS.md", "agent.yaml"})


def _parse_fly_url(s: str) -> tuple[str, str] | None:
    m = _FLY_URL_RE.match(s)
    if not m:
        return None
    return m.group(1), m.group(2)


def _validate_fly_remote(remote: str) -> None:
    """Apply the same confinement rules to Fly remote paths as we do for
    local workspace paths.

    - Reject absolute paths (they would escape /data/workspace prefix).
    - Reject any component that contains `..` (string normalization on
      the remote SFTP server may resolve the traversal).
    - Reject whitespace and control characters (would break the `put/get`
      command on `fly ssh sftp shell`'s single-line parser, and may open
      injection depending on how Fly handles CR/LF).
    - Reject protected agent-config basenames.
    """
    if remote.startswith("/"):
        raise ValueError(
            f"Fly remote path must be relative to /data/workspace, "
            f"got absolute {remote!r}"
        )
    # PosixPath normalizes slashes but keeps `..` components as separate
    # parts, which is what we want to detect.
    parts = [p for p in remote.split("/") if p]
    if any(p == ".." for p in parts):
        raise ValueError(
            f"Fly remote path may not contain '..' segments: {remote!r}"
        )
    if any(ord(c) < 0x20 or c in (" ", "\t") for c in remote):
        raise ValueError(
            f"Fly remote path contains whitespace or control characters: {remote!r}"
        )
    leaf = Path(remote).name
    if leaf in _PROTECTED_BASENAMES:
        raise ValueError(
            f"{leaf} is a protected agent config file — "
            "replace it by editing the yaml directly, not via push"
        )


def _confined_workspace_path(workspace: Path, rel_path: str) -> Path:
    """Resolve `rel_path` against `workspace` and guarantee the result
    stays under the workspace root. Refuses absolute paths and ..-escapes."""
    if Path(rel_path).is_absolute():
        raise ValueError(
            f"workspace paths must be relative to the entorno workspace, "
            f"got absolute {rel_path!r}"
        )
    resolved = (workspace / rel_path).resolve()
    ws = workspace.resolve()
    if ws != resolved and ws not in resolved.parents:
        raise ValueError(f"{rel_path!r} escapes workspace {ws}")
    if resolved.name in _PROTECTED_BASENAMES:
        raise ValueError(
            f"{resolved.name} is a protected agent config file — "
            "replace it by editing the yaml directly, not via push"
        )
    return resolved


def _run_fly_sftp(command: str, app: str, *paths: str, timeout: int = 180) -> None:
    """Run a single sftp command via `fly ssh sftp shell -a APP` by piping
    the command on stdin. Raises on non-zero exit."""
    stdin_script = f"{command} {' '.join(paths)}\nexit\n"
    try:
        subprocess.run(
            ["fly", "ssh", "sftp", "shell", "-a", app],
            input=stdin_script,
            text=True,
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "flyctl not found on PATH — install from https://fly.io/docs/hands-on/install-flyctl/"
        ) from e


def _cmd_push(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="mars_runtime push")
    p.add_argument("local", help="Local file to upload")
    p.add_argument(
        "dest",
        help="Destination: relative workspace path (local) or fly://<app>/<path>",
    )
    p.add_argument("--data-dir", dest="data_dir", help="Override $MARS_DATA_DIR")
    args = p.parse_args(argv)

    local = Path(args.local).expanduser().resolve()
    if not local.exists():
        print(f"source not found: {local}", file=sys.stderr)
        return 1
    if not local.is_file():
        print(f"source must be a regular file: {local}", file=sys.stderr)
        return 1

    fly = _parse_fly_url(args.dest)
    if fly:
        app, remote = fly
        try:
            _validate_fly_remote(remote)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        # Reject local path too if it contains whitespace — it goes on the
        # same SFTP command line.
        if any(c in str(local) for c in (" ", "\t", "\n", "\r")):
            print(
                f"local path contains whitespace; Fly SFTP shell can't handle it safely: {local}",
                file=sys.stderr,
            )
            return 1
        try:
            _run_fly_sftp(
                "put", app, str(local), f"/data/workspace/{remote}",
            )
        except subprocess.CalledProcessError as e:
            print(
                f"fly sftp failed (exit {e.returncode}): {e.stderr.strip() if e.stderr else ''}",
                file=sys.stderr,
            )
            return 1
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"pushed {local} → fly://{app}/{remote}", file=sys.stderr)
        return 0

    data_dir = _data_dir(args.data_dir)
    workspace = data_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        dst = _confined_workspace_path(workspace, args.dest)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local, dst)
    print(f"pushed {local} → {dst}", file=sys.stderr)
    return 0


def _cmd_pull(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="mars_runtime pull")
    p.add_argument(
        "src",
        help="Source: relative workspace path (local) or fly://<app>/<path>",
    )
    p.add_argument("local", help="Local destination path")
    p.add_argument("--data-dir", dest="data_dir", help="Override $MARS_DATA_DIR")
    args = p.parse_args(argv)

    local = Path(args.local).expanduser().resolve()
    if local.exists() and local.is_dir():
        print(
            f"local destination is a directory; pass a file path: {local}",
            file=sys.stderr,
        )
        return 1
    local.parent.mkdir(parents=True, exist_ok=True)

    fly = _parse_fly_url(args.src)
    if fly:
        app, remote = fly
        try:
            _validate_fly_remote(remote)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        if any(c in str(local) for c in (" ", "\t", "\n", "\r")):
            print(
                f"local path contains whitespace; Fly SFTP shell can't handle it safely: {local}",
                file=sys.stderr,
            )
            return 1
        try:
            _run_fly_sftp(
                "get", app, f"/data/workspace/{remote}", str(local),
            )
        except subprocess.CalledProcessError as e:
            print(
                f"fly sftp failed (exit {e.returncode}): {e.stderr.strip() if e.stderr else ''}",
                file=sys.stderr,
            )
            return 1
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"pulled fly://{app}/{remote} → {local}", file=sys.stderr)
        return 0

    data_dir = _data_dir(args.data_dir)
    workspace = data_dir / "workspace"
    try:
        src = _confined_workspace_path(workspace, args.src)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not src.exists():
        print(f"source not found in workspace: {src}", file=sys.stderr)
        return 1
    if not src.is_file():
        print(f"source must be a regular file: {src}", file=sys.stderr)
        return 1
    shutil.copy2(src, local)
    print(f"pulled {src} → {local}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv_list = list(argv) if argv is not None else sys.argv[1:]

    # File-transfer subcommands are host-side only; they do not reach the
    # runtime hardening path or the broker/worker split.
    # If the user has a yaml literally named "push" or "pull", treat it as
    # the yaml path (preserves the original entrypoint contract).
    if argv_list and argv_list[0] in ("push", "pull") and not Path(argv_list[0]).is_file():
        if argv_list[0] == "push":
            return _cmd_push(argv_list[1:])
        return _cmd_pull(argv_list[1:])

    _ingest_secrets_fd()
    _harden_broker()

    args = _parse_args(argv_list)

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
