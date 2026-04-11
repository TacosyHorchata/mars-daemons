"""Unit tests for :mod:`session.codex` — the Codex runtime subprocess wrapper.

Tests the command builder + env forwarder purely, then spawns a
harmless subprocess (``/bin/cat``) via a monkey-patched
:func:`asyncio.create_subprocess_exec` so we can assert the spawn
contract without running the real codex binary (or paying OpenAI).

Also covers the :func:`supervisor._default_spawn_fn` runtime-dispatch
switch (``claude-code`` vs ``codex``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from schema.agent import AgentConfig
from session.codex import (
    CODEX_SECRET_ENV_VARS,
    build_codex_command,
    build_codex_env,
    spawn_codex,
)


def _make_config(
    name: str = "codex-agent",
    tools: list[str] | None = None,
    env_names: list[str] | None = None,
) -> AgentConfig:
    return AgentConfig(
        name=name,
        description=f"codex test agent {name}",
        runtime="codex",
        system_prompt_path="CLAUDE.md",
        tools=tools or [],
        env=env_names or [],
        workdir="/tmp",
    )


# ---------------------------------------------------------------------------
# Command shape
# ---------------------------------------------------------------------------


def test_build_codex_command_uses_exec_json_and_stdin():
    cmd = build_codex_command(_make_config())
    assert cmd[0] == "codex"
    assert cmd[1] == "exec"
    assert "--json" in cmd
    # The `-` argument makes codex read the prompt from stdin so
    # Mars can inject user messages via POST /sessions/{id}/input.
    assert cmd[-1] == "-"


# ---------------------------------------------------------------------------
# Env forwarding
# ---------------------------------------------------------------------------


def test_build_codex_env_forwards_codex_secret_env_vars():
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "OPENAI_API_KEY": "sk-test-token",
        "OPENAI_BASE_URL": "https://api.example.com/v1",
        "NOT_A_SECRET": "leave-me",
    }
    env = build_codex_env(_make_config(), parent_env=parent)
    assert env["OPENAI_API_KEY"] == "sk-test-token"
    assert env["OPENAI_BASE_URL"] == "https://api.example.com/v1"
    # Non-declared non-secret envs are NOT forwarded
    assert "NOT_A_SECRET" not in env
    # POSIX baseline is forwarded
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/user"


def test_build_codex_env_includes_declared_env_names():
    parent = {"PATH": "/usr/bin", "GITHUB_TOKEN": "ghp_xxx"}
    config = _make_config(env_names=["GITHUB_TOKEN", "UNSET_VAR"])
    env = build_codex_env(config, parent_env=parent)
    assert env["GITHUB_TOKEN"] == "ghp_xxx"
    assert "UNSET_VAR" not in env


def test_build_codex_env_scrubs_claude_nesting_leaks_after_extra():
    """Regression guard: ``extra`` must not be able to reintroduce
    CLAUDECODE / CLAUDE_CODE_* into the codex subprocess env."""
    parent = {"PATH": "/usr/bin"}
    env = build_codex_env(
        _make_config(),
        parent_env=parent,
        extra={
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CMUX_CLAUDE_PID": "12345",
            "OPENAI_API_KEY": "sk-from-extra",
        },
    )
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "CMUX_CLAUDE_PID" not in env
    # OPENAI_API_KEY from extra DOES survive
    assert env["OPENAI_API_KEY"] == "sk-from-extra"


def test_codex_secret_env_vars_constant_includes_openai_key():
    assert "OPENAI_API_KEY" in CODEX_SECRET_ENV_VARS


# ---------------------------------------------------------------------------
# spawn_codex — use a harmless stub via monkeypatched subprocess exec
# ---------------------------------------------------------------------------


def test_spawn_codex_uses_create_subprocess_exec(monkeypatch):
    """The spawn function must:
    1. Call asyncio.create_subprocess_exec
    2. With cmd = codex exec --json -
    3. With stdin=PIPE (default) or DEVNULL (opt-out)
    4. With stdout=PIPE, stderr=PIPE
    5. With the computed env
    6. With cwd = config.workdir
    """
    captured: dict[str, Any] = {}

    async def _fake_exec(*cmd: str, **kwargs: Any):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        # Return a sentinel object that spawn_codex returns as-is
        return object()

    import session.codex as codex_mod

    monkeypatch.setattr(codex_mod.asyncio, "create_subprocess_exec", _fake_exec)

    async def _run():
        return await spawn_codex(
            _make_config(env_names=["OPENAI_API_KEY"]),
            "mars-session-1",
            extra_env={"OPENAI_API_KEY": "sk-override"},
        )

    result = asyncio.run(_run())
    assert result is not None
    assert captured["cmd"] == ("codex", "exec", "--json", "-")
    kwargs = captured["kwargs"]
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["cwd"] == "/tmp"
    env = kwargs["env"]
    assert env["OPENAI_API_KEY"] == "sk-override"


def test_spawn_codex_stdin_devnull_opt_out(monkeypatch):
    captured: dict[str, Any] = {}

    async def _fake_exec(*cmd: str, **kwargs: Any):
        captured["kwargs"] = kwargs
        return object()

    import session.codex as codex_mod

    monkeypatch.setattr(codex_mod.asyncio, "create_subprocess_exec", _fake_exec)

    async def _run():
        await spawn_codex(_make_config(), "s", stdin_pipe=False)

    asyncio.run(_run())
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL


# ---------------------------------------------------------------------------
# Runtime dispatch at the supervisor's default spawn_fn
# ---------------------------------------------------------------------------


def test_default_spawn_fn_dispatches_on_runtime(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _fake_claude(config, session_id, **kwargs):
        calls.append(("claude-code", config.name))
        return object()

    async def _fake_codex(config, session_id, **kwargs):
        calls.append(("codex", config.name))
        return object()

    import supervisor as sup

    monkeypatch.setattr(sup, "spawn_claude_code", _fake_claude)
    monkeypatch.setattr(sup, "spawn_codex", _fake_codex)

    spawn = sup._default_spawn_fn()

    async def _run():
        await spawn(_make_config("c-agent", env_names=[]), "s-1")  # codex
        cc_config = AgentConfig(
            name="cc-agent",
            description="claude",
            runtime="claude-code",
            system_prompt_path="CLAUDE.md",
            workdir="/tmp",
        )
        await spawn(cc_config, "s-2")

    asyncio.run(_run())
    assert ("codex", "c-agent") in calls
    assert ("claude-code", "cc-agent") in calls


def test_default_spawn_fn_rejects_unknown_runtime():
    """Unknown runtimes must raise loudly so a malformed agent.yaml
    never silently spawns a no-op subprocess."""
    import supervisor as sup

    spawn = sup._default_spawn_fn()

    # AgentConfig validation currently restricts runtime to the
    # Literal["claude-code", "codex"] set, but the factory function
    # still defensively raises for completeness. Simulate by
    # building a SimpleNamespace-like object that bypasses
    # validation.
    class _BadConfig:
        runtime = "go-agents"
        name = "bad"

    async def _run():
        await spawn(_BadConfig(), "s-x")

    with pytest.raises(ValueError, match="unknown runtime"):
        asyncio.run(_run())
