"""Entry point — dispatches to CLI subcommands and the broker flow.

Session usage:
  python -m mars_runtime <agent.yaml>          # start a new session
  python -m mars_runtime --resume <id>         # resume an existing session
  python -m mars_runtime --list                # list recent sessions

File transfer (host ↔ sandbox, USER only — the agent has no access):
  python -m mars_runtime push <local> <dest>
  python -m mars_runtime pull <src>   <local>

Architecture (post-Phase-1b):

  __main__.py               thin dispatcher — routes push/pull to cli.files,
                            else runs the broker flow below.

  broker/
    env.py                  secret ingest + worker env scrub
    hardening.py            PR_SET_DUMPABLE + RLIMIT_CORE
    process.py              worker spawn + stdin pump + RPC forward

  cli/
    files.py                push / pull

The broker flow here still owns argparse + config/session loading; a
follow-up phase will lift those into `cli/run.py` per the architecture
plan. This commit is structural only — zero behavior change.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from pydantic import ValidationError

from . import llm_client, session_store
from ._paths import data_dir as _data_dir
from .broker import env as broker_env
from .broker import hardening as broker_hardening
from .broker import process as broker_process
from .cli import files as _cli_files
from .schema import AgentConfig
from .session_store import InvalidSessionId


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


def _ingest_secrets_fd() -> None:
    """Legacy alias — see `broker.env.ingest_secrets_fd`. Kept callable via
    the __main__ module so existing tests patching `mars_runtime.__main__.
    _ingest_secrets_fd` don't break."""
    broker_env.ingest_secrets_fd()


def _harden_broker() -> None:
    """Legacy alias — see `broker.hardening.harden_broker`."""
    broker_hardening.harden_broker()


def main(argv: list[str] | None = None) -> int:
    argv_list = list(argv) if argv is not None else sys.argv[1:]

    # File-transfer subcommands are host-side only; they do not reach the
    # runtime hardening path or the broker/worker split.
    # If the user has a yaml literally named "push" or "pull", treat it as
    # the yaml path (preserves the original entrypoint contract).
    if argv_list and argv_list[0] in ("push", "pull") and not Path(argv_list[0]).is_file():
        if argv_list[0] == "push":
            return _cli_files.cmd_push(argv_list[1:])
        return _cli_files.cmd_pull(argv_list[1:])

    # Call through the __main__ wrappers (not the broker modules directly)
    # so tests that patch `mars_runtime.__main__._ingest_secrets_fd` /
    # `mars_runtime.__main__._harden_broker` still intercept startup.
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

    worker = broker_process.spawn_worker(config, session_id, data_dir, start_messages)

    input_pump = threading.Thread(
        target=broker_process.pump_user_input, args=(worker,), daemon=True
    )
    input_pump.start()

    try:
        broker_process.pump_worker_output(worker, llm)
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
