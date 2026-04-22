"""run_bash — execute shell commands inside the conversation workspace."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..core.tools import AuthContext, BaseTool, ToolResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_BYTES = 100_000

_workspace_store: Any | None = None


def set_workspace_store(store: Any) -> None:
    global _workspace_store
    _workspace_store = store


class BashTool(BaseTool):
    name = "run_bash"
    description = (
        "Execute a bash command inside the current conversation workspace. "
        "Default cwd is the workspace root for this conversation. Use this for "
        "cat, grep, sed, find, tar, unzip, git, and other shell workflows."
    )
    execution_mode = "exclusive"
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Bash command or script to execute.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional path inside the workspace to use as cwd.",
                "default": "",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": f"Seconds before killing the command (default {DEFAULT_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS}).",
                "default": DEFAULT_TIMEOUT_SECONDS,
                "minimum": 1,
                "maximum": MAX_TIMEOUT_SECONDS,
            },
        },
        "required": ["command"],
    }

    async def _execute(self, input: dict, auth: AuthContext, state: dict) -> ToolResult:
        if _workspace_store is None:
            return ToolResult(success=False, error="Workspace not configured")

        command = str(input.get("command", "")).strip()
        if not command:
            return ToolResult(success=False, error="Command is required")

        conversation_id = str(state.get("conversation_id") or "").strip()
        if not conversation_id:
            return ToolResult(success=False, error="Conversation context is required")

        user_id = auth.user_id or "anonymous"
        cwd_input = str(input.get("cwd", "")).strip()
        try:
            timeout = int(input.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        timeout = max(1, min(timeout, MAX_TIMEOUT_SECONDS))

        try:
            cwd_path = _workspace_store.resolve_path(
                auth.org_id,
                user_id,
                conversation_id,
                cwd_input,
                expect_directory=True,
                create=True,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd_path),
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult(
                success=False,
                error=f"Bash command timed out after {timeout} seconds",
            )
        except Exception as exc:
            logger.error("Bash execution failed: %s", exc, exc_info=True)
            return ToolResult(success=False, error=f"Bash command failed: {exc}")

        return ToolResult(
            success=True,
            data={
                "stdout": _decode_and_cap(stdout_bytes),
                "stderr": _decode_and_cap(stderr_bytes),
                "exit_code": process.returncode,
                "cwd": str(cwd_path),
            },
        )


def _decode_and_cap(content: bytes) -> str:
    if len(content) <= MAX_OUTPUT_BYTES:
        return content.decode("utf-8", errors="replace")
    truncated = content[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return f"{truncated}\n\n[output truncated at {MAX_OUTPUT_BYTES} bytes]"
