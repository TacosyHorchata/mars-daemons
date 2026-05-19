"""Codex CLI provider.

Uses the authenticated `codex` binary on the host. This is intentionally
different from the OpenAI API provider: it works with `codex login` credentials
and lets Pedro run cloud turns through his Codex subscription session.
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


class CodexCLIClient:
    def __init__(self, binary: str | None = None, cwd: str | None = None) -> None:
        self._binary = binary or os.environ.get("CODEX_CLI_PATH") or "codex"
        self._cwd = Path(cwd or os.environ.get("MARS_CODEX_CWD") or os.getcwd())

    def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Response:
        full_text = ""
        input_tokens = 0
        output_tokens = 0
        prompt = _build_prompt(system=system, messages=messages, tools=tools)
        args = [
            self._binary,
            "exec",
            "--json",
            "--ephemeral",
            "--full-auto",
            "--skip-git-repo-check",
            "-",
        ]
        if model and model != "codex":
            args[2:2] = ["--model", model]

        proc = subprocess.run(
            args,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=str(self._cwd),
            env=_build_codex_env(self._cwd),
            timeout=int(os.environ.get("CODEX_CLI_TIMEOUT_S", "300")),
            check=False,
        )

        if proc.returncode != 0 and "item.completed" not in proc.stdout:
            raise RuntimeError((proc.stderr or proc.stdout or "codex failed").strip())

        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    full_text += str(item.get("text") or "")
            elif event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
            elif event.get("type") in {"error", "turn.failed"}:
                error = event.get("message") or (event.get("error") or {}).get("message")
                if error:
                    full_text += f"\n[Codex Error]: {error}"

        return Response(
            text=full_text,
            tool_calls=[],
            stop_reason="end_turn",
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
        yield ChatChunk(
            kind="message_stop",
            stop_reason=resp.stop_reason,
            final_response=resp,
        )


def _content_to_text(content: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "tool_result":
            parts.append(f"[Tool Result {block.get('tool_use_id')}]\n{block.get('content')}")
        elif kind == "tool_use":
            parts.append(
                f"[Tool Call {block.get('name')}]\n{json.dumps(block.get('input') or {})}"
            )
    return "\n\n".join(p for p in parts if p)


def _build_codex_env(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    home = Path(env.get("HOME") or str(cwd))
    if not home.exists() or not os.access(home, os.W_OK):
        home = cwd
    env["HOME"] = str(home)

    codex_home = Path(
        env.get("MARS_CODEX_HOME")
        or env.get("CODEX_HOME")
        or str(home / ".codex")
    )
    codex_home.mkdir(parents=True, exist_ok=True)

    shared_auth = env.get("MARS_CODEX_SHARED_AUTH")
    auth_path = codex_home / "auth.json"
    if shared_auth and not auth_path.exists():
        shutil.copyfile(shared_auth, auth_path)
        auth_path.chmod(0o600)

    shared_installation_id = env.get("MARS_CODEX_SHARED_INSTALLATION_ID")
    installation_id_path = codex_home / "installation_id"
    if shared_installation_id and not installation_id_path.exists():
        shutil.copyfile(shared_installation_id, installation_id_path)
        installation_id_path.chmod(0o600)

    env["CODEX_HOME"] = str(codex_home)
    return env


def _build_prompt(
    *,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
) -> str:
    parts = ["[System Instructions]", system.strip()]
    if tools:
        parts.extend(
            [
                "[Available Host Tools]",
                "You are running inside Codex CLI on the EC2 workspace. Use the filesystem and shell directly when useful.",
            ]
        )
    parts.append("[Conversation]")
    for message in messages:
        role = message["role"].upper()
        text = _content_to_text(message["content"])
        if text:
            parts.append(f"{role}:\n{text}")
    return "\n\n".join(parts)


def _factory(**kwargs: Any) -> CodexCLIClient:
    return CodexCLIClient(**kwargs)


register("codex_cli", _factory, model_prefixes=["codex"])
