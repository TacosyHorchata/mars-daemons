from __future__ import annotations

from pathlib import Path

import pytest

from mars_runtime.daemon.resolve import UnknownAssistant, resolve_agent_config


_AGENT_YAML = """
name: {name}
description: test agent
model: claude-opus-4-5
system_prompt_path: CLAUDE.md
tools:
  - read
"""


def _write_agent(path: Path, name: str, prompt_body: str = "hi") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_AGENT_YAML.format(name=name), encoding="utf-8")
    (path.parent / "CLAUDE.md").write_text(prompt_body, encoding="utf-8")


def test_resolve_agent_user_override(tmp_path: Path) -> None:
    user_ws = tmp_path / "user"
    shared = tmp_path / "shared"
    _write_agent(user_ws / "agents" / "bot.yaml", "bot", prompt_body="USER")
    _write_agent(shared / "agents" / "bot.yaml", "bot", prompt_body="SHARED")

    config = resolve_agent_config(user_ws, shared, "bot")

    assert Path(config.system_prompt_path).read_text() == "USER"


def test_resolve_agent_shared_fallback(tmp_path: Path) -> None:
    user_ws = tmp_path / "user"
    user_ws.mkdir(parents=True)
    shared = tmp_path / "shared"
    _write_agent(shared / "agents" / "bot.yaml", "bot", prompt_body="SHARED")

    config = resolve_agent_config(user_ws, shared, "bot")

    assert Path(config.system_prompt_path).read_text() == "SHARED"


def test_resolve_agent_not_found(tmp_path: Path) -> None:
    user_ws = tmp_path / "user"
    user_ws.mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir(parents=True)

    with pytest.raises(UnknownAssistant):
        resolve_agent_config(user_ws, shared, "nope")


def test_resolve_system_prompt_relative(tmp_path: Path) -> None:
    user_ws = tmp_path / "user"
    shared = tmp_path / "shared"
    _write_agent(user_ws / "agents" / "bot.yaml", "bot", prompt_body="relative ok")

    config = resolve_agent_config(user_ws, shared, "bot")

    assert Path(config.system_prompt_path).is_absolute()
    assert Path(config.system_prompt_path).read_text() == "relative ok"
