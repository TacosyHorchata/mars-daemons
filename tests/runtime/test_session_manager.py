"""Unit tests for :class:`session.manager.SessionManager`.

These tests deliberately substitute ``/bin/sleep`` for the real
``claude`` binary (via the injectable ``spawn_fn``) so the suite never
spends Claude Max quota and can run fast in CI.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

from schema.agent import AgentConfig
from session.claude_code import build_claude_command, build_claude_env
from session.manager import SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(name: str = "test-agent", tools: list[str] | None = None) -> AgentConfig:
    return AgentConfig(
        name=name,
        description=f"test agent {name}",
        runtime="claude-code",
        system_prompt_path="CLAUDE.md",
        tools=tools or [],
        env=[],
        workdir="/tmp",
    )


async def _sleep_spawn(config, session_id):
    """Replacement for spawn_claude_code that runs /bin/sleep instead of
    the real claude binary. Keeps stdin/stdout/stderr piped so the
    interface matches production."""
    return await asyncio.create_subprocess_exec(
        "/bin/sleep",
        "60",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def _pid_exists(pid: int) -> bool:
    """Standard POSIX idiom for 'is pid alive' — signal 0 probes the
    process without delivering anything. Raises ProcessLookupError if
    the pid is not currently a process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Pid exists but is owned by someone else (can't happen in
        # the test because we spawned it)
        return True
    return True


def _ps_has_pid(pid: int) -> bool:
    """Belt-and-suspenders check: ask ``ps -p`` whether the pid is
    present. Returns False on a non-zero exit (no matching pid)."""
    result = subprocess.run(
        ["ps", "-p", str(pid)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Happy path — spawn, list, get
# ---------------------------------------------------------------------------


def test_spawn_registers_session_and_returns_handle():
    async def _run():
        mgr = SessionManager(spawn_fn=_sleep_spawn)
        handle = await mgr.spawn(_make_config("one"))
        assert handle.session_id.startswith("mars-")
        assert handle.name == "one"
        assert handle.description == "test agent one"
        assert handle.status == "running"
        assert handle.is_alive
        assert handle.pid > 0
        assert mgr.get(handle.session_id) is handle
        listed = mgr.list()
        assert len(listed) == 1 and listed[0].session_id == handle.session_id
        await mgr.shutdown()

    asyncio.run(_run())


def test_kill_unknown_session_id_returns_false():
    async def _run():
        mgr = SessionManager(spawn_fn=_sleep_spawn)
        result = await mgr.kill("mars-does-not-exist")
        assert result is False

    asyncio.run(_run())


def test_session_ids_are_unique_across_spawns():
    async def _run():
        mgr = SessionManager(spawn_fn=_sleep_spawn)
        ids = set()
        handles = []
        for i in range(5):
            h = await mgr.spawn(_make_config(f"a{i}"))
            ids.add(h.session_id)
            handles.append(h)
        assert len(ids) == 5
        await mgr.shutdown()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Done-when: 10 spawn + kill leaves zero zombies
# ---------------------------------------------------------------------------


def test_spawn_and_kill_10_sessions_leaves_no_zombies():
    """Story 1.4 done-when: spawning and killing 10 sessions must
    leave zero leftover processes on the OS side. We verify with two
    independent probes (``os.kill(pid, 0)`` and ``ps -p``) so a failure
    is attributable."""

    async def _run():
        mgr = SessionManager(spawn_fn=_sleep_spawn)
        handles = []
        for i in range(10):
            h = await mgr.spawn(_make_config(f"load-{i}"))
            handles.append(h)

        pids = [h.pid for h in handles]
        assert len(pids) == len(set(pids))  # all unique
        assert len(mgr.list()) == 10

        # Every pid should currently exist
        for pid in pids:
            assert _pid_exists(pid), f"pid {pid} should be alive before kill"
            assert _ps_has_pid(pid), f"ps should see pid {pid} before kill"

        # Kill each session
        for h in handles:
            killed = await mgr.kill(h.session_id)
            assert killed is True

        # Manager dict is empty
        assert mgr.list() == []

        # Every process has been awaited/reaped
        for h in handles:
            assert h.process.returncode is not None
            assert h.status in ("killed", "exited_clean", "exited_error")
            assert h.terminated_at is not None

        return pids

    pids = asyncio.run(_run())

    # After the event loop closes, the children have been reaped.
    # Neither probe should see them anymore.
    for pid in pids:
        assert not _pid_exists(pid), f"pid {pid} still alive — zombie leak"
        assert not _ps_has_pid(pid), f"ps still sees pid {pid} — zombie leak"


def test_shutdown_kills_every_registered_session():
    async def _run():
        mgr = SessionManager(spawn_fn=_sleep_spawn)
        handles = [await mgr.spawn(_make_config(f"s{i}")) for i in range(3)]
        await mgr.shutdown()
        assert mgr.list() == []
        for h in handles:
            assert h.process.returncode is not None
        return [h.pid for h in handles]

    pids = asyncio.run(_run())
    for pid in pids:
        assert not _pid_exists(pid)


# ---------------------------------------------------------------------------
# Resilience: already-dead child, concurrent kill
# ---------------------------------------------------------------------------


def test_kill_handles_already_dead_child_without_raising():
    """If the subprocess exited naturally before ``kill`` was called,
    :meth:`SessionManager.kill` should still succeed and update the
    handle state."""

    async def _short_spawn(config, session_id):
        # Spawn a child that exits immediately.
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.exit(0)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _run():
        mgr = SessionManager(spawn_fn=_short_spawn)
        handle = await mgr.spawn(_make_config("short"))
        # Let the child exit, then reap via wait.
        await handle.process.wait()
        assert handle.process.returncode == 0
        killed = await mgr.kill(handle.session_id)
        assert killed is True
        assert mgr.get(handle.session_id) is None

    asyncio.run(_run())


def test_concurrent_kill_calls_resolve_without_race():
    """Two coroutines racing to kill the same session must not raise;
    exactly one should get True back."""

    async def _run():
        mgr = SessionManager(spawn_fn=_sleep_spawn)
        handle = await mgr.spawn(_make_config("race"))
        results = await asyncio.gather(
            mgr.kill(handle.session_id),
            mgr.kill(handle.session_id),
        )
        assert results.count(True) == 1
        assert results.count(False) == 1
        assert mgr.list() == []

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# claude_code.build_* helpers
# ---------------------------------------------------------------------------


def test_build_claude_command_has_required_flags():
    cmd = build_claude_command(_make_config("pr-review", tools=["Bash", "Read"]))
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--output-format" in cmd
    out_idx = cmd.index("--output-format")
    assert cmd[out_idx + 1] == "stream-json"
    assert "--verbose" in cmd
    assert "--permission-mode" in cmd
    pm_idx = cmd.index("--permission-mode")
    assert cmd[pm_idx + 1] == "acceptEdits"
    assert "--allowed-tools" in cmd
    at_idx = cmd.index("--allowed-tools")
    assert cmd[at_idx + 1] == "Bash Read"


def test_build_claude_command_does_not_set_input_format_in_v1_4():
    """v1.4 scope: the supervisor has not yet wired input injection
    (Story 1.5). Passing `--input-format stream-json` with an
    unwritten stdin risks the child blocking on a stdin read."""
    cmd = build_claude_command(_make_config("no-input"))
    assert "--input-format" not in cmd


def test_build_claude_command_without_tools_has_no_allowlist_flag():
    cmd = build_claude_command(_make_config("no-tools", tools=[]))
    assert "--allowed-tools" not in cmd


def test_build_claude_env_only_forwards_declared_secrets():
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "ZOHO_API_KEY": "zoho-secret",
        "OTHER_SECRET": "should-not-leak",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
    }
    config = _make_config("leak-check", tools=[])
    config = config.model_copy(update={"env": ["ZOHO_API_KEY"]})
    env = build_claude_env(config, parent_env=parent)
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/x"
    assert env["ZOHO_API_KEY"] == "zoho-secret"
    assert "OTHER_SECRET" not in env
    # cmux/parent Claude Code leakage scrubbed
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env


def test_build_claude_env_extra_overrides_parent():
    parent = {"PATH": "/usr/bin", "HOME": "/home/x"}
    config = _make_config("override")
    env = build_claude_env(
        config,
        parent_env=parent,
        extra={"CLAUDE_CODE_OAUTH_TOKEN": "abc123"},
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "abc123"


def test_build_claude_env_scrub_runs_after_extra_merge():
    """Nesting-leak scrub must apply AFTER `extra` is merged so a
    careless caller cannot reintroduce CLAUDECODE through extra."""
    parent = {"PATH": "/usr/bin"}
    config = _make_config("scrub-order")
    env = build_claude_env(
        config,
        parent_env=parent,
        extra={"CLAUDECODE": "1", "CMUX_CLAUDE_PID": "42"},
    )
    assert "CLAUDECODE" not in env
    assert "CMUX_CLAUDE_PID" not in env


# ---------------------------------------------------------------------------
# Status taxonomy (from codex adversarial review)
# ---------------------------------------------------------------------------


def test_killed_session_has_killed_status():
    """Explicit SessionManager.kill on a running child → 'killed'."""

    async def _run():
        mgr = SessionManager(spawn_fn=_sleep_spawn)
        h = await mgr.spawn(_make_config("status-killed"))
        await mgr.kill(h.session_id)
        assert h.status == "killed"

    asyncio.run(_run())


def test_cleanly_exited_session_has_exited_clean_status():
    """Child that exits 0 before kill → 'exited_clean'."""

    async def _clean_spawn(config, session_id):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.exit(0)",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _run():
        mgr = SessionManager(spawn_fn=_clean_spawn)
        h = await mgr.spawn(_make_config("clean-exit"))
        await h.process.wait()
        await mgr.kill(h.session_id)
        assert h.status == "exited_clean"

    asyncio.run(_run())


def test_error_exited_session_has_exited_error_status():
    """Child that exits with non-zero code → 'exited_error'."""

    async def _error_spawn(config, session_id):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.exit(42)",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _run():
        mgr = SessionManager(spawn_fn=_error_spawn)
        h = await mgr.spawn(_make_config("err-exit"))
        await h.process.wait()
        await mgr.kill(h.session_id)
        assert h.status == "exited_error"

    asyncio.run(_run())


def test_orphaned_list_starts_empty():
    mgr = SessionManager(spawn_fn=_sleep_spawn)
    assert mgr.orphaned == []
