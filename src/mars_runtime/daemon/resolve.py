from __future__ import annotations

import re
from pathlib import Path

from ..cli.run import _resolve_system_prompt
from ..config import AgentConfig

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class UnknownAssistant(Exception):
    pass


def _safe_child(parent: Path, name: str) -> Path:
    candidate = (parent / name).resolve()
    try:
        candidate.relative_to(parent.resolve())
    except ValueError as exc:
        raise UnknownAssistant(name) from exc
    return candidate


def resolve_agent_config(
    user_workspace: Path,
    shared_dir: Path,
    assistant_id: str,
) -> AgentConfig:
    if not _SAFE_ID_RE.fullmatch(assistant_id):
        raise UnknownAssistant(assistant_id)
    filename = f"{assistant_id}.yaml"
    user_agents = user_workspace / "agents"
    shared_agents = shared_dir / "agents"
    user_path = _safe_child(user_agents, filename) if user_agents.is_dir() else None
    shared_path = _safe_child(shared_agents, filename) if shared_agents.is_dir() else None
    if user_path is not None and user_path.is_file():
        target = user_path
    elif shared_path is not None and shared_path.is_file():
        target = shared_path
    else:
        raise UnknownAssistant(assistant_id)
    config = AgentConfig.from_yaml_file(target)
    return _resolve_system_prompt(config, target)
