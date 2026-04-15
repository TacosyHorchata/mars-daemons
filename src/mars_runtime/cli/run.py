"""Session CLI: parse args, load config/session, spawn broker, pump RPC.

This is where the session-lifecycle flow lives. `__main__.py` just
dispatches to this module after deciding the command isn't a file
transfer (push/pull).

Used by:
  - `__main__.py` for CLI invocation (`python -m mars_runtime <yaml>`)
  - `api.py` for programmatic embedding (indirectly, via the split-out
    core `_run_broker_session()` helper)
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from pydantic import ValidationError

from .. import providers as llm_client
from .._paths import data_dir as _data_dir
from ..broker import env as broker_env
from ..broker import hardening as broker_hardening
from ..broker import process as broker_process
from ..config import AgentConfig
from ..storage import sessions as session_store
from ..storage.sessions import InvalidSessionId


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


def _run_broker_session(
    config: AgentConfig,
    session_id: str,
    start_messages: list | None,
    data_dir: Path,
) -> int:
    """Core broker-side flow: construct LLM client, spawn worker, pump
    RPC. Shared by the CLI session command and the programmatic api."""
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


def main(
    argv: list[str],
    *,
    ingest_secrets_fd=broker_env.ingest_secrets_fd,
    harden_broker=broker_hardening.harden_broker,
) -> int:
    """CLI entry for session commands (yaml / --resume / --list).

    `ingest_secrets_fd` and `harden_broker` are injectable so tests can
    patch them via the legacy `mars_runtime.__main__._ingest_secrets_fd`
    names when they wrap this function.
    """
    ingest_secrets_fd()
    harden_broker()

    args = _parse_args(argv)

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

    return _run_broker_session(config, session_id, start_messages, data_dir)
