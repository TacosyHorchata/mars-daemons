"""Contract test — runs the real pinned Claude Code CLI and asserts that
:func:`session.claude_code_stream.parse_stream` produces the canonical
Mars event sequence for a minimal ``Bash``-using prompt.

Enablement
----------

This test is OPT-IN locally and disabled by default in CI.

* Runs iff all three hold:
    1. ``claude`` is on ``PATH``
    2. ``claude --version`` reports exactly
       :data:`session.claude_code_version.PINNED_CLAUDE_CODE_VERSION`
    3. ``MARS_CONTRACT_LIVE=1`` is set in the environment

The explicit opt-in gate matters because a real ``claude -p`` call
spends Pedro's Claude Max quota. The test is still a one-line command
when enabled:

    MARS_CONTRACT_LIVE=1 uv run pytest tests/contract/test_claude_code_stream.py

CI wiring (post-Epic 3): install the pinned CLI in the CI container,
inject a ``CLAUDE_CODE_OAUTH_TOKEN`` via secrets, and flip
``MARS_CONTRACT_LIVE=1`` on a scheduled daily job so drift surfaces
within 24 hours of an upstream Claude Code release.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest

from events.types import AssistantText, SessionEnded, SessionStarted, ToolCall, ToolResult
from session.claude_code_stream import parse_stream
from session.claude_code_version import PINNED_CLAUDE_CODE_VERSION


def _extract_version_token(version_output: str) -> str:
    """``claude --version`` prints e.g. ``2.1.101 (Claude Code)`` — pull
    the first whitespace-delimited token so we compare exact versions,
    not substrings (``2.1.1010`` must not match ``2.1.101``).
    """
    return version_output.strip().split()[0] if version_output.strip() else ""


def _skip_reason() -> str | None:
    if os.environ.get("MARS_CONTRACT_LIVE") != "1":
        return "MARS_CONTRACT_LIVE=1 not set (opt-in: test spends real quota)"
    claude = shutil.which("claude")
    if not claude:
        return "claude CLI not on PATH"
    try:
        out = subprocess.check_output([claude, "--version"], text=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return f"claude --version failed: {exc}"
    token = _extract_version_token(out)
    if token != PINNED_CLAUDE_CODE_VERSION:
        return (
            f"claude version drift: got {token!r}, "
            f"expected pinned {PINNED_CLAUDE_CODE_VERSION!r}"
        )
    return None


_SKIP = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")


def test_real_claude_cli_produces_canonical_mars_event_sequence():
    """Drive a real ``claude -p`` invocation through :func:`parse_stream`.

    Asserts:
    * SessionStarted is the first event, carries pinned version
    * ToolCall (Bash) appears before ToolResult (same tool_use_id)
    * AssistantText appears after the tool_result
    * SessionEnded is the last event, with ``stop_reason == "end_turn"``
    """
    cmd = [
        "claude",
        "-p",
        "Use the Bash tool to run exactly 'echo mars-contract' and then reply "
        "with one short sentence confirming what it printed.",
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowed-tools",
        "Bash",
        "--permission-mode",
        "acceptEdits",
    ]
    env = {**os.environ}
    # Scrub cmux / parent Claude Code session leakage so the subprocess
    # uses on-disk auth only.
    for k in (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CMUX_CLAUDE_PID",
    ):
        env.pop(k, None)

    warnings: list[tuple[str, Exception | None]] = []

    async def _run() -> tuple[list, int, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        # Drain stderr concurrently so a noisy CLI can't fill its pipe
        # and deadlock the stdout parser. Ignore content for v1 — we
        # only care that the pipe keeps flowing.
        async def _drain_stderr() -> bytes:
            chunks: list[bytes] = []
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())

        collected: list = []
        async for ev in parse_stream(
            "mars-contract-1",
            proc.stdout,
            on_warning=lambda msg, exc: warnings.append((msg, exc)),
        ):
            collected.append(ev)

        stderr_bytes = await stderr_task
        await proc.wait()
        return collected, proc.returncode or 0, stderr_bytes

    events, returncode, stderr_bytes = asyncio.run(_run())

    # Contract: the CLI must exit cleanly. A non-zero exit after
    # emitting a superficially valid stdout is a drift we want to catch.
    assert returncode == 0, (
        f"claude exited with returncode={returncode}; stderr="
        f"{stderr_bytes[-1000:]!r}"
    )

    # Contract: no dropped lines during a healthy run. Any warning here
    # indicates parser drift or unexpected runtime output and should
    # fail loud, not silently degrade.
    assert warnings == [], (
        "parse_stream surfaced warnings during a healthy contract run:\n"
        + "\n".join(f"  - {m!r}: {e}" for m, e in warnings)
    )

    types = [type(e).__name__ for e in events]
    assert "SessionStarted" in types, f"missing SessionStarted; got {types}"
    assert "ToolCall" in types, f"missing ToolCall; got {types}"
    assert "ToolResult" in types, f"missing ToolResult; got {types}"
    assert "AssistantText" in types, f"missing AssistantText; got {types}"
    assert "SessionEnded" in types, f"missing SessionEnded; got {types}"

    # Ordering: SessionStarted first, SessionEnded last, canonical sandwich
    assert types[0] == "SessionStarted"
    assert types[-1] == "SessionEnded"
    assert types.index("ToolCall") < types.index("ToolResult")
    assert types.index("ToolResult") < types.index("AssistantText")

    started = next(e for e in events if isinstance(e, SessionStarted))
    assert started.claude_code_version == PINNED_CLAUDE_CODE_VERSION

    call = next(e for e in events if isinstance(e, ToolCall))
    assert call.tool_name == "Bash"
    assert "mars-contract" in call.input.get("command", "")

    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.tool_use_id == call.tool_use_id
    assert "mars-contract" in result.content
    assert result.is_error is False

    text = next(e for e in events if isinstance(e, AssistantText))
    assert "mars-contract" in text.text.lower() or "printed" in text.text.lower()

    ended = next(e for e in events if isinstance(e, SessionEnded))
    assert ended.stop_reason == "end_turn"
    assert ended.num_turns is not None and ended.num_turns >= 1
    assert ended.total_cost_usd is not None and ended.total_cost_usd > 0
