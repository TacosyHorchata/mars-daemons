"""OpenAI Codex CLI subprocess lifecycle for the Mars runtime.

Mirror of :mod:`session.claude_code` for the ``codex`` runtime. The
two modules share a command-builder + env-forwarder + async spawn
shape so :class:`~session.manager.SessionManager` can dispatch on
``AgentConfig.runtime`` without special-casing anything downstream.

Scope (Story 3.5)
-----------------

Command + spawn only. A full Mars event parser for codex's own
JSONL output (``{"type":"thread.started"}``, ``{"type":"item.completed"}``,
``{"type":"turn.completed"}``) is **v1.1 scope** — v1 codex sessions
run end-to-end but do not emit typed :class:`~events.types.MarsEventBase`
events through the supervisor's event pump. The pump's
``parse_stream`` silently drops every unknown event type, so a codex
session is observable via ``GET /sessions/{id}`` (status, pid) but
``GET /sessions/{id}/events`` returns an empty list.

Authentication
--------------

Codex authenticates via ``~/.codex/config.toml`` (long-lived local
config) or via ``OPENAI_API_KEY`` in env. Mars v1 supports the
env-var path: the caller sets ``OPENAI_API_KEY`` on the supervisor
and it is forwarded into the codex subprocess env so codex picks it
up without needing a container-mounted config file.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping

from schema.agent import AgentConfig

__all__ = [
    "CODEX_SECRET_ENV_VARS",
    "build_codex_command",
    "build_codex_env",
    "spawn_codex",
]


#: Env vars that carry codex-related secrets. They are ALWAYS
#: forwarded to the subprocess when present on the parent env —
#: Mars never writes them to disk and the threat model treats them
#: as owned by the user, not by Mars.
CODEX_SECRET_ENV_VARS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
)


def build_codex_command(config: AgentConfig) -> list[str]:
    """Build the ``codex exec --json`` command line from an
    :class:`AgentConfig`.

    Always-on flags:

    * ``exec`` — non-interactive subcommand (equivalent of
      ``claude -p``).
    * ``--json`` — emit events to stdout as JSONL so the supervisor
      can hand the stream to a parser in v1.1.

    The initial prompt is deliberately NOT included on the command
    line — Mars writes the first user message via stdin (mirroring
    the Claude Code flow). That way the supervisor's input-injection
    path works the same for both runtimes.
    """
    cmd: list[str] = [
        "codex",
        "exec",
        "--json",
        "-",  # read prompt from stdin
    ]
    return cmd


def build_codex_env(
    config: AgentConfig,
    parent_env: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct the subprocess environment for a ``codex`` session.

    Explicit-allowlist model identical to :func:`session.claude_code.build_claude_env`:

    * Canonical POSIX baseline (PATH, HOME, LANG, LC_ALL, TZ) forwards
      if present in the parent env.
    * Each name declared in ``config.env`` forwards if present.
    * Additionally, every name in :data:`CODEX_SECRET_ENV_VARS`
      forwards if present — the runtime needs them to auth.
    * ``extra`` merges last and wins on conflict.
    * Nesting-leak vars from a parent Claude Code session are
      scrubbed AFTER merging so ``extra`` cannot reintroduce them.
    """
    src = parent_env if parent_env is not None else os.environ
    env: dict[str, str] = {}

    for base_key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ"):
        if base_key in src:
            env[base_key] = src[base_key]

    for name in config.env:
        if name in src:
            env[name] = src[name]

    # Always forward codex-auth env vars — they are the secret the
    # user owns and the runtime needs to authenticate.
    for name in CODEX_SECRET_ENV_VARS:
        if name in src and name not in env:
            env[name] = src[name]

    if extra:
        env.update(extra)

    # Scrub Claude-Code nesting leakage (mirrors build_claude_env)
    for leak in (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CMUX_CLAUDE_PID",
    ):
        env.pop(leak, None)

    return env


async def spawn_codex(
    config: AgentConfig,
    session_id: str,
    *,
    extra_env: Mapping[str, str] | None = None,
    stdin_pipe: bool = True,
) -> asyncio.subprocess.Process:
    """Spawn a ``codex exec --json`` subprocess for the given session.

    The returned process has ``stdout`` and ``stderr`` as pipes. The
    caller (``SessionManager``) handles the lifecycle.

    ``stdin_pipe=True`` (default) opens stdin as a pipe because the
    codex command is built with ``-`` as the prompt, meaning codex
    reads the initial prompt from stdin. ``stdin_pipe=False`` points
    stdin at ``/dev/null`` for tests where the caller will never
    write — codex will then see EOF immediately and exit.

    ``session_id`` is reserved for future session-scoped file paths
    (same pattern as :func:`session.claude_code.spawn_claude_code`).
    """
    del session_id  # reserved
    cmd = build_codex_command(config)
    env = build_codex_env(config, extra=extra_env)
    stdin_arg = (
        asyncio.subprocess.PIPE if stdin_pipe else asyncio.subprocess.DEVNULL
    )
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=stdin_arg,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=config.workdir,
    )
