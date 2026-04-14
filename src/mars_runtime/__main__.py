"""Entry point.

Usage:
  python -m mars_runtime <agent.yaml>          # start a new session
  python -m mars_runtime --resume <id>         # resume an existing session
  python -m mars_runtime --list                # list recent sessions (JSON lines)

Data layout (per entorno = per Fly volume):
  $MARS_DATA_DIR/workspace/   — git repo, agent cwd
  $MARS_DATA_DIR/sessions/    — <session_id>.json atomic snapshots

Default $MARS_DATA_DIR: ./.mars-data/
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from . import llm_client, session_store
from .agent import run
from .schema import AgentConfig
from .session_store import InvalidSessionId
from .tools import ToolRegistry, load_all


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
    """Resolve system_prompt_path relative to the yaml's own directory.

    Without this, chdir-ing into the workspace would break relative paths
    like `./CLAUDE.md` next to the yaml.
    """
    sp = Path(config.system_prompt_path)
    if not sp.is_absolute():
        sp = (yaml_path.parent / sp).resolve()
    return config.model_copy(update={"system_prompt_path": str(sp)})


def main(argv: list[str] | None = None) -> int:
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
                f"session has malformed messages array (expected list of "
                f"{{role, content: list}} dicts)",
                file=sys.stderr,
            )
            return 1
    else:
        yaml_path = Path(args.yaml_path).resolve()
        config = AgentConfig.from_yaml_file(yaml_path)
        config = _resolve_system_prompt(config, yaml_path)
        session_id = session_store.new_id()
        start_messages = None

    os.chdir(workspace_path)

    load_all()
    tools = ToolRegistry(config.tools or None)

    llm_client.load_all()
    provider_name = config.provider or llm_client.infer_provider(config.model)
    llm = llm_client.get(provider_name)

    try:
        run(
            config,
            llm,
            tools,
            sessions_dir=sessions_dir,
            session_id=session_id,
            workspace_path=workspace_path,
            start_messages=start_messages,
        )
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        # Disk full, permission denied, missing binary — bubbled up from
        # session save or git subprocess launch.
        print(f"persistence error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        # Git command exited non-zero (e.g., pre-commit hook rejected the
        # commit, missing config). Surface without a raw traceback.
        print(f"git error: exit {e.returncode} running {e.cmd}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
