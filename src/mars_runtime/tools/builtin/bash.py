"""Bash — run a shell command.

Security model: SPEED BUMP, NOT A SANDBOX.

The denylist (env / printenv / echo $VAR / bare set) catches accidental
secret reads and shallow prompt-injection attempts. It is trivially
bypassable by a determined agent: /usr/bin/env, $(printenv), backticks,
python -c, perl -e, reading /proc/self/environ, etc. That is by design
— Mars assumes the daemon runs code you wrote using keys you own. Do
not deploy untrusted agents expecting this to contain them.

If you need real isolation, run the container with a hardened seccomp
profile, a read-only filesystem, and a scrubbed env. Those are Fly /
Docker concerns, not this file's.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

from ..base import Tool, ToolOutput
from ..registry import register

_ECHO_EXPAND_RE = re.compile(r"\becho\s+\$")
_DENY_COMMANDS: frozenset[str] = frozenset({"env", "printenv"})
_SEPARATOR_RE = re.compile(r"[;|&]+")


def _first_token_of_segment(segment: str) -> str:
    stripped = segment.strip()
    if not stripped:
        return ""
    return stripped.split()[0]


def _denied(command: str) -> str | None:
    if _ECHO_EXPAND_RE.search(command):
        return "matches `echo $...` pattern"
    for segment in _SEPARATOR_RE.split(command):
        first = _first_token_of_segment(segment)
        if first in _DENY_COMMANDS:
            return f"command `{first}` is denylisted"
        if first == "set" and segment.strip() == "set":
            return "bare `set` dumps all vars"
    return None


def _bash(input_: dict[str, Any]) -> ToolOutput:
    command = input_["command"]
    timeout = input_.get("timeout", 60)

    reason = _denied(command)
    if reason:
        return ToolOutput(f"bash command blocked: {reason}", is_error=True)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolOutput(f"command timed out after {timeout}s", is_error=True)

    output = result.stdout
    if result.stderr:
        output += f"\n[stderr]\n{result.stderr}"
    if result.returncode != 0:
        return ToolOutput(output + f"\n[exit={result.returncode}]", is_error=True)
    return ToolOutput(output or "(no output)")


register(
    Tool(
        name="bash",
        description="Run a shell command. Blocks env/printenv/echo $VAR secret-read patterns.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "Seconds before kill.", "default": 60},
            },
            "required": ["command"],
        },
        fn=_bash,
    )
)
