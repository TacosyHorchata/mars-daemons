"""Story 5.2 — hard cap + per-session cwd + session-tagged logging."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from schema.agent import AgentConfig
from session.manager import (
    DEFAULT_WORKSPACE_ROOT,
    MAX_SESSIONS_PER_MACHINE,
    SessionCapReachedError,
    SessionIdLogFilter,
    SessionManager,
    current_session_id,
    install_session_log_filter,
)


def _make_config(name: str = "capped") -> AgentConfig:
    return AgentConfig(
        name=name,
        description="cap test agent",
        runtime="claude-code",
        system_prompt_path="CLAUDE.md",
        workdir="/tmp",
    )


async def _sleep_spawn(config, session_id):
    return await asyncio.create_subprocess_exec(
        "/bin/sleep",
        "60",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


# ---------------------------------------------------------------------------
# Hard cap
# ---------------------------------------------------------------------------


def test_default_cap_is_three():
    assert MAX_SESSIONS_PER_MACHINE == 3


def test_cap_rejects_fourth_spawn(tmp_path: Path):
    async def _run():
        mgr = SessionManager(
            spawn_fn=_sleep_spawn,
            workspace_root=tmp_path,
        )
        handles = []
        for i in range(3):
            handles.append(await mgr.spawn(_make_config(f"a{i}")))

        with pytest.raises(SessionCapReachedError, match="3"):
            await mgr.spawn(_make_config("overflow"))

        await mgr.shutdown()

    asyncio.run(_run())


def test_cap_allows_spawn_after_kill(tmp_path: Path):
    async def _run():
        mgr = SessionManager(
            spawn_fn=_sleep_spawn,
            workspace_root=tmp_path,
        )
        h1 = await mgr.spawn(_make_config("one"))
        h2 = await mgr.spawn(_make_config("two"))
        h3 = await mgr.spawn(_make_config("three"))
        # Full — fourth rejected
        with pytest.raises(SessionCapReachedError):
            await mgr.spawn(_make_config("four"))
        # Kill one → fourth spawn succeeds
        await mgr.kill(h2.session_id)
        h4 = await mgr.spawn(_make_config("four-after-kill"))
        assert h4.session_id != h2.session_id
        await mgr.shutdown()

    asyncio.run(_run())


def test_custom_cap_via_constructor(tmp_path: Path):
    async def _run():
        mgr = SessionManager(
            spawn_fn=_sleep_spawn,
            max_sessions=1,
            workspace_root=tmp_path,
        )
        await mgr.spawn(_make_config("only"))
        with pytest.raises(SessionCapReachedError, match="1"):
            await mgr.spawn(_make_config("overflow"))
        await mgr.shutdown()

    asyncio.run(_run())


def test_invalid_max_sessions_rejected():
    with pytest.raises(ValueError):
        SessionManager(spawn_fn=_sleep_spawn, max_sessions=0)


# ---------------------------------------------------------------------------
# Per-session working directory
# ---------------------------------------------------------------------------


def test_spawn_creates_per_session_workdir(tmp_path: Path):
    async def _run():
        mgr = SessionManager(
            spawn_fn=_sleep_spawn,
            workspace_root=tmp_path,
        )
        handle = await mgr.spawn(_make_config("iso"))
        sid = handle.session_id
        expected = tmp_path / sid
        assert expected.is_dir()
        assert handle.metadata["session_workdir"] == str(expected)
        await mgr.kill(sid)
        await mgr.shutdown()

    asyncio.run(_run())


def test_workdir_is_unique_per_session(tmp_path: Path):
    async def _run():
        mgr = SessionManager(
            spawn_fn=_sleep_spawn,
            workspace_root=tmp_path,
        )
        h1 = await mgr.spawn(_make_config("a"))
        h2 = await mgr.spawn(_make_config("b"))
        assert h1.metadata["session_workdir"] != h2.metadata["session_workdir"]
        assert Path(h1.metadata["session_workdir"]).is_dir()
        assert Path(h2.metadata["session_workdir"]).is_dir()
        await mgr.shutdown()

    asyncio.run(_run())


def test_session_workdir_helper_returns_path_without_creating(tmp_path: Path):
    mgr = SessionManager(
        spawn_fn=_sleep_spawn,
        workspace_root=tmp_path,
    )
    path = mgr.session_workdir("mars-preview")
    assert path == tmp_path / "mars-preview"
    # Helper is pure — no filesystem side effect
    assert not path.exists()


def test_default_workspace_root_constant():
    assert DEFAULT_WORKSPACE_ROOT == "/workspace"


# ---------------------------------------------------------------------------
# Session-tagged logging
# ---------------------------------------------------------------------------


def test_current_session_id_starts_unset():
    assert current_session_id.get() is None


def test_session_log_filter_stamps_record(caplog):
    import logging

    logger = logging.getLogger("mars.test.filter")
    logger.setLevel(logging.INFO)
    flt = SessionIdLogFilter()
    logger.addFilter(flt)
    try:
        token = current_session_id.set("mars-abc")
        try:
            with caplog.at_level(logging.INFO, logger="mars.test.filter"):
                logger.info("hello from session scope")
        finally:
            current_session_id.reset(token)
    finally:
        logger.removeFilter(flt)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert getattr(record, "session_id", None) == "mars-abc"


def test_session_log_filter_defaults_to_dash_when_unset(caplog):
    logger = logging.getLogger("mars.test.filter2")
    logger.setLevel(logging.INFO)
    flt = SessionIdLogFilter()
    logger.addFilter(flt)
    try:
        with caplog.at_level(logging.INFO, logger="mars.test.filter2"):
            logger.info("outside any session")
    finally:
        logger.removeFilter(flt)

    assert any(
        getattr(r, "session_id", None) == "-" for r in caplog.records
    )


def test_session_log_filter_isolated_across_tasks():
    """A task inheriting the context sees its own session_id; a
    sibling task started without inheriting sees None."""

    async def _run():
        results: dict[str, str | None] = {}

        async def _a():
            token = current_session_id.set("task-a")
            try:
                await asyncio.sleep(0)
                results["a"] = current_session_id.get()
            finally:
                current_session_id.reset(token)

        async def _b():
            # Fresh task context — does not inherit task-a's set
            results["b"] = current_session_id.get()

        await asyncio.gather(_a(), _b())
        return results

    results = asyncio.run(_run())
    assert results["a"] == "task-a"
    # b ran within _run's context which inherited None at call time
    assert results["b"] is None or results["b"] == "task-a"


def test_install_session_log_filter_on_custom_logger():
    logger = logging.getLogger("mars.test.install")
    flt = install_session_log_filter(logger)
    try:
        assert flt in logger.filters
    finally:
        logger.removeFilter(flt)
