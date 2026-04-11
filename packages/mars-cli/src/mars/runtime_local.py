"""``mars run --local ./agent.yaml`` — run a daemon locally, no Fly.

Reuses the exact same modules as remote mode
(:mod:`session.claude_code`, :mod:`session.claude_code_stream`,
:mod:`events.types`) so the two code paths never drift. The only
difference is the *driver loop*: instead of the FastAPI supervisor
taking HTTP input and fanning events out via SSE, local mode reads
user prompts from the tty, feeds them into the spawned ``claude``
subprocess as stream-json, and pretty-prints Mars events to stderr.

v1 UX:

1. ``mars run --local ./agent.yaml`` opens a tiny REPL:
   ``"daemon> "`` prompt, multi-line input ends on an empty line.
2. Each user turn is serialized as a stream-json ``user`` event
   and written to ``claude``'s stdin.
3. ``parse_stream`` consumes stdout concurrently and pretty-prints
   ``AssistantText`` / ``AssistantChunk`` / ``ToolCall`` / ``ToolResult``
   / ``SessionStarted`` / ``SessionEnded`` to stderr so the JSON
   channel stays clean if the user pipes stdout to a file.
4. Ctrl+C sends SIGINT to the subprocess and exits the REPL.
5. Ctrl+D on an empty prompt closes stdin → claude sees EOF → exits.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import TextIO

import click

from events.types import (
    AssistantChunk,
    AssistantText,
    MarsEventBase,
    SessionEnded,
    SessionStarted,
    ToolCall,
    ToolResult,
)
from schema.agent import AgentConfig
from session.claude_code import spawn_claude_code
from session.claude_code_stream import CriticalParseError, parse_stream

__all__ = ["runtime_local_command", "format_event_for_terminal", "run_local_loop"]


# ---------------------------------------------------------------------------
# Event pretty-printer
# ---------------------------------------------------------------------------


def format_event_for_terminal(event: MarsEventBase) -> str:
    """Render one Mars event as a single line for tty display.

    Matched against the 6 subtypes the runtime actually produces; other
    subtypes fall through to a generic representation.
    """
    if isinstance(event, SessionStarted):
        return (
            f"→ session started (model={event.model}, "
            f"version={event.claude_code_version})"
        )
    if isinstance(event, AssistantText):
        return f"← {event.text}"
    if isinstance(event, AssistantChunk):
        return f".. {event.delta}"
    if isinstance(event, ToolCall):
        input_preview = ""
        if event.input:
            try:
                input_preview = f" input={json.dumps(event.input)[:120]}"
            except (TypeError, ValueError):
                input_preview = ""
        return f"→ tool_call: {event.tool_name}{input_preview}"
    if isinstance(event, ToolResult):
        truncated = event.content[:200].replace("\n", " ")
        error_marker = " (error)" if event.is_error else ""
        return f"← tool_result{error_marker}: {truncated}"
    if isinstance(event, SessionEnded):
        cost = f"${event.total_cost_usd:.4f}" if event.total_cost_usd else "?"
        turns = event.num_turns if event.num_turns is not None else "?"
        return (
            f"→ session ended (stop_reason={event.stop_reason}, "
            f"turns={turns}, cost={cost})"
        )
    return f"→ {event.type}"


# ---------------------------------------------------------------------------
# stdin helpers
# ---------------------------------------------------------------------------


def read_multiline_prompt(prompt: str = "daemon> ", in_stream: TextIO | None = None) -> str | None:
    """Read a multi-line prompt from ``in_stream`` until a blank line.

    Returns ``None`` on EOF (Ctrl+D at the prompt), the empty string
    if the user entered only a blank line, or the collected text.
    """
    stream = in_stream if in_stream is not None else sys.stdin
    click.echo(prompt, nl=False, err=True)
    lines: list[str] = []
    while True:
        try:
            line = stream.readline()
        except KeyboardInterrupt:
            return None
        if line == "":  # EOF
            return None if not lines else "\n".join(lines).rstrip()
        stripped = line.rstrip("\n")
        if stripped == "" and lines:
            return "\n".join(lines).rstrip()
        if stripped == "" and not lines:
            # Blank first line — show the prompt again
            click.echo(prompt, nl=False, err=True)
            continue
        lines.append(stripped)


def encode_user_event_line(text: str) -> bytes:
    """Serialize a user turn as a single stream-json line."""
    payload = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
    return (json.dumps(payload) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Driver loop
# ---------------------------------------------------------------------------


async def run_local_loop(
    config: AgentConfig,
    *,
    session_id: str = "mars-local-1",
    in_stream: TextIO | None = None,
    out_stream: TextIO | None = None,
    settings_path: str | None = None,
    prompt: str = "daemon> ",
) -> int:
    """Core async loop. Spawns claude, shuttles prompts ↔ events.

    Returns the subprocess exit code (0 on clean exit, non-zero on
    parser error or crash). Split out so unit tests can drive it
    with asyncio.StreamReader/Writer fakes without going through Click.
    """
    out = out_stream if out_stream is not None else sys.stderr

    proc = await spawn_claude_code(
        config,
        session_id,
        stdin_stream_json=True,
        settings_path=settings_path,
    )
    assert proc.stdin is not None, "spawn_claude_code must return piped stdin"
    assert proc.stdout is not None, "spawn_claude_code must return piped stdout"

    # Event reader runs concurrently with the prompt loop
    async def _consume_events() -> int:
        try:
            async for ev in parse_stream(
                session_id,
                proc.stdout,  # type: ignore[arg-type]
                on_warning=lambda msg, exc: click.echo(
                    f"! parser warning: {msg}", err=True, file=out
                ),
            ):
                click.echo(format_event_for_terminal(ev), file=out)
        except CriticalParseError as exc:
            click.echo(f"!! critical parse error: {exc}", err=True, file=out)
            return 2
        return 0

    reader_task = asyncio.create_task(_consume_events(), name="mars-local-reader")

    async def _prompt_loop() -> None:
        loop = asyncio.get_event_loop()
        while True:
            try:
                text = await loop.run_in_executor(
                    None, read_multiline_prompt, prompt, in_stream
                )
            except KeyboardInterrupt:
                text = None
            if text is None:
                # EOF / Ctrl+D — close stdin so claude exits cleanly
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            if not text:
                continue
            line = encode_user_event_line(text)
            try:
                proc.stdin.write(line)  # type: ignore[union-attr]
                await proc.stdin.drain()  # type: ignore[union-attr]
            except (BrokenPipeError, ConnectionResetError):
                return

    prompt_task = asyncio.create_task(_prompt_loop(), name="mars-local-prompts")

    # Whichever finishes first ends the session. Handle SIGINT by
    # cancelling the prompt loop and letting the reader drain.
    done, pending = await asyncio.wait(
        {reader_task, prompt_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # Wait for claude to exit (it should after stdin close)
    try:
        return_code = await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            return_code = await proc.wait()
        except Exception:  # noqa: BLE001
            return_code = 137

    # reader_task result is our parser verdict; propagate non-zero
    reader_result = 0
    if reader_task.done() and not reader_task.cancelled():
        try:
            reader_result = reader_task.result() or 0
        except Exception:  # noqa: BLE001
            reader_result = 2

    if reader_result:
        return reader_result
    return return_code or 0


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("run")
@click.argument("agent_yaml", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--local",
    "local_mode",
    is_flag=True,
    required=True,
    help="Run the daemon locally (no Fly.io). Required flag in v1 — remote mode is `mars deploy`.",
)
@click.option(
    "--settings",
    "settings_path_override",
    default=None,
    help="Override the claude_code_settings.json path (defaults to $MARS_CLAUDE_CODE_SETTINGS).",
)
def runtime_local_command(
    agent_yaml: Path,
    local_mode: bool,
    settings_path_override: str | None,
) -> None:
    """Run a Mars daemon locally against your installed claude CLI."""
    del local_mode  # --local is required for v1; remote mode is a separate `mars deploy`
    try:
        config = AgentConfig.from_yaml_file(agent_yaml)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"invalid agent.yaml: {exc}") from exc

    settings_path = settings_path_override
    if settings_path is None:
        settings_path = os.environ.get("MARS_CLAUDE_CODE_SETTINGS") or None

    click.echo(
        f"Running {config.name} locally. Ctrl+D to end, Ctrl+C to abort.",
        err=True,
    )

    # Install a SIGINT handler that just lets asyncio.run propagate it
    def _sigint_handler(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        exit_code = asyncio.run(
            run_local_loop(config, settings_path=settings_path)
        )
    except KeyboardInterrupt:
        click.echo("\n(interrupted)", err=True)
        exit_code = 130
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"claude CLI not found ({exc}) — install it first: "
            "https://docs.claude.com/en/docs/claude-code/setup"
        ) from exc

    sys.exit(exit_code)
