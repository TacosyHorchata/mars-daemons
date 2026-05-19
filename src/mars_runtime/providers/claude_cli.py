"""Claude Code CLI provider.

Uses the authenticated `claude` binary on the host. This supports cloud
daemons that run with Claude Code subscription credentials instead of an
Anthropic API key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import ChatChunk, Message, Response, ToolSpec, register
from .codex_cli import _content_to_text


class ClaudeCLIClient:
    def __init__(self, binary: str | None = None, cwd: str | None = None) -> None:
        self._binary = binary or os.environ.get("CLAUDE_CLI_PATH") or "claude"
        self._cwd = Path(cwd or os.environ.get("MARS_CLAUDE_CWD") or os.getcwd())

    def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Response:
        prompt = _build_prompt(messages=messages, tools=tools)
        args = [
            self._binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            os.environ.get("CLAUDE_CLI_PERMISSION_MODE", "default"),
            "--no-session-persistence",
            "--system-prompt",
            system,
        ]
        if model and model != "claude":
            args.extend(["--model", model])

        proc = subprocess.run(
            args,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=str(self._cwd),
            env=_build_claude_env(self._cwd),
            timeout=int(os.environ.get("CLAUDE_CLI_TIMEOUT_S", "300")),
            check=False,
        )

        text_parts: list[str] = []
        result_text = ""
        stop_reason = "end_turn"
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "assistant":
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(str(block.get("text") or ""))
            elif event.get("type") == "result":
                result_text = str(event.get("result") or "")
                stop_reason = str(event.get("stop_reason") or event.get("terminal_reason") or stop_reason)

        full_text = "".join(text_parts) or result_text
        if proc.returncode != 0 and not full_text:
            raise RuntimeError((proc.stderr or proc.stdout or "claude failed").strip())

        return Response(
            text=full_text,
            tool_calls=[],
            stop_reason=stop_reason,
            raw_content=[{"type": "text", "text": full_text}] if full_text else [],
        )

    def chat_stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Iterator[ChatChunk]:
        resp = self.chat(
            system=system,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
        )
        if resp.text:
            yield ChatChunk(kind="text_delta", text=resp.text)
        yield ChatChunk(kind="message_stop", stop_reason=resp.stop_reason, final_response=resp)


def _build_claude_env(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    home = Path(env.get("HOME") or str(cwd))
    if not home.exists() or not os.access(home, os.W_OK):
        home = cwd
    env["HOME"] = str(home)

    config_dir = Path(
        env.get("MARS_CLAUDE_CONFIG_DIR")
        or env.get("CLAUDE_CONFIG_DIR")
        or str(home / ".claude")
    )
    config_dir.mkdir(parents=True, exist_ok=True)

    shared_credentials = env.get("MARS_CLAUDE_SHARED_CREDENTIALS")
    credentials_path = config_dir / ".credentials.json"
    if shared_credentials and not credentials_path.exists():
        shutil.copyfile(shared_credentials, credentials_path)
        credentials_path.chmod(0o600)

    shared_config = env.get("MARS_CLAUDE_SHARED_CONFIG")
    config_path = config_dir / ".claude.json"
    if shared_config and not config_path.exists():
        shutil.copyfile(shared_config, config_path)
        config_path.chmod(0o600)

    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def _build_prompt(*, messages: list[Message], tools: list[ToolSpec]) -> str:
    parts = []
    if tools:
        parts.append(
            "[Available Host Tools]\n"
            "You are running inside Claude Code on the EC2 workspace. "
            "Use the filesystem and shell directly when useful."
        )
    parts.append("[Conversation]")
    for message in messages:
        role = message["role"].upper()
        text = _content_to_text(message["content"])
        if text:
            parts.append(f"{role}:\n{text}")
    return "\n\n".join(parts)


def _factory(**kwargs: Any) -> ClaudeCLIClient:
    return ClaudeCLIClient(**kwargs)


register("claude_cli", _factory, model_prefixes=[])
