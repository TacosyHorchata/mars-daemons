"""Subprocess lifecycle for ``claude`` sessions.

This module is the thin shim between :class:`session.manager.SessionManager`
and the externally-owned Claude Code CLI. Its sole job is to build a
valid command line + environment from an :class:`AgentConfig` and spawn
an :class:`asyncio.subprocess.Process` with piped stdin/stdout/stderr.

Stream parsing happens downstream in
:mod:`session.claude_code_stream`; this file does not touch stdout bytes.

The function signature is also the extension point for
:class:`SessionManager`: tests pass a fake spawn function that runs a
cheap stand-in command (``sleep``, a python script, etc.) so the
session-lifecycle tests never spend real Claude Max quota.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping

from schema.agent import AgentConfig

from .claude_code_version import PINNED_CLAUDE_CODE_VERSION

__all__ = [
    "PINNED_CLAUDE_CODE_VERSION",
    "build_claude_command",
    "build_claude_env",
    "spawn_claude_code",
]


def build_claude_command(config: AgentConfig) -> list[str]:
    """Build the ``claude -p`` command line from an :class:`AgentConfig`.

    Defaults chosen for Mars v1.4 scope:

    * ``--output-format stream-json`` — the sole parseable channel.
    * ``--verbose`` — without it ``stream-json`` drops the ``system.init``
      event, breaking the parser's session anchor.
    * ``--permission-mode acceptEdits`` — v1 static allowlist model; see
      ``spikes/03-permission-roundtrip.md``.
    * ``--allowed-tools`` — driven by ``config.tools``; empty list means
      "no explicit allowlist" (runtime defaults apply).

    Deliberately NOT passed in v1.4:

    * ``--input-format stream-json`` — Story 1.5 wires the supervisor's
      ``POST /sessions/{id}/input`` endpoint and passes this flag
      together with an open stdin pipe. Passing it in 1.4 with a never-
      written stdin would risk the child blocking on stdin read.
    """
    cmd: list[str] = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "acceptEdits",
    ]
    if config.tools:
        cmd.extend(["--allowed-tools", " ".join(config.tools)])
    return cmd


#: Env vars that must never reach a Mars subprocess because they cause
#: the child ``claude`` to piggy-back on a nested Claude Code session
#: (useful for cmux dev, catastrophic for a Mars daemon). Scrubbed AFTER
#: merging ``extra`` so a careless caller cannot reintroduce them.
_CLAUDE_NESTING_LEAKS: frozenset[str] = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CMUX_CLAUDE_PID",
    }
)


def build_claude_env(
    config: AgentConfig,
    parent_env: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct the subprocess environment for a ``claude`` session.

    Explicit-allowlist model: only env vars named in ``config.env`` plus
    a small fixed set of canonical POSIX baseline vars (PATH, HOME, LANG,
    LC_ALL, TZ) are forwarded from the parent environment. Everything
    else is scrubbed. The supervisor is the only place that decides
    which secrets the daemon sees — e.g. ``OPENAI_API_KEY``,
    ``AWS_ACCESS_KEY_ID``, or ``ANTHROPIC_API_KEY`` will NOT leak unless
    they appear in ``config.env`` or ``extra``.

    ``extra`` is merged first and then the nesting-leak scrub runs
    *after*, so a caller cannot accidentally reintroduce
    ``CLAUDECODE`` et al. through the extra dict.

    **Known trade-off for HOME forwarding:** in the production Mars Fly
    container ``HOME`` points at an empty volume owned by Mars, so
    forwarding it does not expose user credentials. In local development
    it exposes ``~/.aws``, ``~/.ssh``, etc. to the daemon — acceptable
    because the daemon is code Pedro wrote on keys Pedro owns (see
    ``docs/security.md`` — Epic 9).
    """
    src = parent_env if parent_env is not None else os.environ
    env: dict[str, str] = {}
    for base_key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ"):
        if base_key in src:
            env[base_key] = src[base_key]
    for name in config.env:
        if name in src:
            env[name] = src[name]
    if extra:
        env.update(extra)
    # Scrub nesting-leak vars LAST so `extra` cannot reintroduce them.
    for leak in _CLAUDE_NESTING_LEAKS:
        env.pop(leak, None)
    return env


async def spawn_claude_code(
    config: AgentConfig,
    session_id: str,
    *,
    extra_env: Mapping[str, str] | None = None,
) -> asyncio.subprocess.Process:
    """Spawn a ``claude -p`` subprocess for the given session.

    The returned process has ``stdout`` and ``stderr`` attached as
    pipes. The caller (SessionManager) keeps a reference and passes
    ``process.stdout`` to :func:`session.claude_code_stream.parse_stream`.

    ``stdin`` is pointed at ``/dev/null`` in v1.4 so the child sees EOF
    immediately if it ever tries to read. Story 1.5 switches this to a
    pipe and pairs it with ``--input-format stream-json`` when the
    supervisor's ``POST /sessions/{id}/input`` endpoint goes live.

    ``session_id`` is currently unused by the command itself — Claude
    Code maintains its own session id internally — but the argument is
    kept in the signature so Mars has a single place to thread the
    session through when we add session-id-scoped file paths (Epic 5 /
    Epic 6).
    """
    del session_id  # reserved for future use (see docstring)
    cmd = build_claude_command(config)
    env = build_claude_env(config, extra=extra_env)
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=config.workdir,
    )
