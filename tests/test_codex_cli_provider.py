from __future__ import annotations

import json
from pathlib import Path

from mars_runtime.providers.codex_cli import CodexCLIClient


def test_codex_cli_provider_parses_agent_message(tmp_path: Path) -> None:
    bin_path = tmp_path / "codex"
    bin_path.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf '%s\\n' "
        + repr(json.dumps({"type": "thread.started", "thread_id": "t"}))
        + "\n"
        "printf '%s\\n' "
        + repr(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "hello"},
                }
            )
        )
        + "\n"
        "printf '%s\\n' "
        + repr(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                }
            )
        )
        + "\n",
        encoding="utf-8",
    )
    bin_path.chmod(0o755)

    client = CodexCLIClient(binary=str(bin_path), cwd=str(tmp_path))
    response = client.chat(
        system="sys",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[],
        model="codex",
        max_tokens=100,
    )

    assert response.text == "hello"
    assert response.tool_calls == []
    assert response.stop_reason == "end_turn"


def test_codex_cli_provider_prepares_writable_codex_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    shared_auth = tmp_path / "shared-auth.json"
    shared_auth.write_text('{"token":"test"}', encoding="utf-8")
    shared_installation_id = tmp_path / "shared-installation-id"
    shared_installation_id.write_text("install-test", encoding="utf-8")

    bin_path = tmp_path / "codex"
    bin_path.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "test -f \"$CODEX_HOME/auth.json\" || exit 42\n"
        "test -f \"$CODEX_HOME/installation_id\" || exit 43\n"
        "test \"$HOME\" = \"$PWD\" || exit 44\n"
        "printf '%s\\n' "
        + repr(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "ok"},
                }
            )
        )
        + "\n",
        encoding="utf-8",
    )
    bin_path.chmod(0o755)

    monkeypatch.setenv("HOME", str(tmp_path / "missing-home"))
    monkeypatch.setenv("MARS_CODEX_SHARED_AUTH", str(shared_auth))
    monkeypatch.setenv("MARS_CODEX_SHARED_INSTALLATION_ID", str(shared_installation_id))

    client = CodexCLIClient(binary=str(bin_path), cwd=str(tmp_path))
    response = client.chat(
        system="sys",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[],
        model="codex",
        max_tokens=100,
    )

    assert response.text == "ok"
