"""Stable embedding surface for mars-runtime.

Library consumers should only import from this module. Everything else
(broker/, worker/, cli/, providers/, runtime/, storage/, tools/) is
internal and subject to change without notice across versions.

Public types
  - AgentConfig, ChatChunk, Response, ToolCall, ToolSpec, Message
  - ProviderCollision, InvalidSessionId, BrokerDisconnected

Public functions
  - list_sessions(data_dir=None) → list[dict]
  - load_session(session_id, data_dir=None) → dict
  - run_new_session(config, data_dir=None) → int
  - resume_session(session_id, data_dir=None) → int

Invocation model
  `run_new_session` and `resume_session` drive the broker/worker split
  the same way the CLI does: they read user input from stdin, emit
  events as JSON lines on stdout, and return the broker's exit code.
  Embedders that want programmatic I/O can invoke the CLI entry via
  subprocess with wired pipes (same contract) or drop down to the
  internal modules at their own risk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._paths import data_dir as _data_dir
from .cli.run import _run_broker_session
from .providers import (
    ChatChunk,
    Message,
    ProviderCollision,
    Response,
    ToolCall,
    ToolSpec,
)
from .schema import AgentConfig
from .storage import sessions as _sessions
from .storage.sessions import InvalidSessionId
from .worker.broker_client import BrokerDisconnected


__all__ = [
    # Types
    "AgentConfig",
    "BrokerDisconnected",
    "ChatChunk",
    "InvalidSessionId",
    "Message",
    "ProviderCollision",
    "Response",
    "ToolCall",
    "ToolSpec",
    # Session queries
    "list_sessions",
    "load_session",
    # Session lifecycle
    "run_new_session",
    "resume_session",
]


def _data_paths(data_dir_override: str | Path | None) -> tuple[Path, Path, Path]:
    dir_ = _data_dir(str(data_dir_override) if data_dir_override else None)
    return dir_, dir_ / "workspace", dir_ / "sessions"


def list_sessions(data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Return metadata (newest-first) for sessions persisted under the
    given entorno data directory.

    Defaults: $MARS_DATA_DIR or ./.mars-data/.
    Entries: {id, agent_name, created_at, updated_at, turn_count}.
    """
    _, _, sessions_dir = _data_paths(data_dir)
    return _sessions.list_recent(sessions_dir)


def load_session(
    session_id: str,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load a session snapshot as a dict with keys id, agent_name,
    agent_config, created_at, messages. Raises `InvalidSessionId` for
    malformed ids, `FileNotFoundError` if the snapshot is absent."""
    _, _, sessions_dir = _data_paths(data_dir)
    return _sessions.load(sessions_dir, session_id)


def run_new_session(
    config: AgentConfig,
    data_dir: str | Path | None = None,
) -> int:
    """Start a fresh session with `config`. Spawns the broker/worker
    split, reads user turns from stdin, emits events to stdout, and
    returns the worker's exit code.

    The caller is responsible for any hardening (harden_broker,
    secret ingest) since `api` does not assume it owns process setup.
    For full CLI semantics, prefer `python -m mars_runtime`.
    """
    dir_, workspace_path, sessions_dir = _data_paths(data_dir)
    workspace_path.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = _sessions.new_id()
    return _run_broker_session(config, session_id, None, dir_)


def resume_session(
    session_id: str,
    data_dir: str | Path | None = None,
) -> int:
    """Resume an existing session by id. Reads the snapshot's
    agent_config and message history, then drives the same broker/worker
    flow as `run_new_session`."""
    dir_, workspace_path, sessions_dir = _data_paths(data_dir)
    workspace_path.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    data = _sessions.load(sessions_dir, session_id)
    config = AgentConfig(**data["agent_config"])
    start_messages = data.get("messages", [])
    if not _sessions._is_valid_messages_shape(start_messages):
        raise ValueError(
            "session has malformed messages array (expected list of "
            "{role, content: list} dicts)"
        )
    return _run_broker_session(config, session_id, start_messages, dir_)
