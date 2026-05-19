from __future__ import annotations

import json
from pathlib import Path

from mars_runtime.providers.claude_cli import ClaudeCLIClient


def test_claude_cli_provider_parses_assistant_message(tmp_path: Path) -> None:
    bin_path = tmp_path / "claude"
    bin_path.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf '%s\\n' "
        + repr(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "hello"}],
                    },
                }
            )
        )
        + "\n"
        "printf '%s\\n' "
        + repr(
            json.dumps(
                {
                    "type": "result",
                    "result": "hello",
                    "stop_reason": "end_turn",
                }
            )
        )
        + "\n",
        encoding="utf-8",
    )
    bin_path.chmod(0o755)

    client = ClaudeCLIClient(binary=str(bin_path), cwd=str(tmp_path))
    response = client.chat(
        system="sys",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[],
        model="claude",
        max_tokens=100,
    )

    assert response.text == "hello"
    assert response.tool_calls == []
    assert response.stop_reason == "end_turn"


def test_claude_cli_provider_prepares_writable_config_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    shared_credentials = tmp_path / "shared-credentials.json"
    shared_credentials.write_text('{"token":"test"}', encoding="utf-8")
    shared_config = tmp_path / "shared-claude.json"
    shared_config.write_text("{}", encoding="utf-8")

    bin_path = tmp_path / "claude"
    bin_path.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "test -f \"$CLAUDE_CONFIG_DIR/.credentials.json\" || exit 42\n"
        "test -f \"$CLAUDE_CONFIG_DIR/.claude.json\" || exit 43\n"
        "test \"$HOME\" = \"$PWD\" || exit 44\n"
        "printf '%s\\n' "
        + repr(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "ok"}],
                    },
                }
            )
        )
        + "\n",
        encoding="utf-8",
    )
    bin_path.chmod(0o755)

    monkeypatch.setenv("HOME", str(tmp_path / "missing-home"))
    monkeypatch.setenv("MARS_CLAUDE_SHARED_CREDENTIALS", str(shared_credentials))
    monkeypatch.setenv("MARS_CLAUDE_SHARED_CONFIG", str(shared_config))

    client = ClaudeCLIClient(binary=str(bin_path), cwd=str(tmp_path))
    response = client.chat(
        system="sys",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[],
        model="claude",
        max_tokens=100,
    )

    assert response.text == "ok"
