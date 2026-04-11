"""Unit tests for the baked PreToolUse hook scripts (Story 3.2).

Tests the two shell scripts in ``apps/mars-runtime/hooks/``:

* ``deny-protected-edit.sh`` — denies Edit/Write/MultiEdit on
  CLAUDE.md, AGENTS.md, claude_code_settings.json.
* ``deny-secret-read-bash.sh`` — denies Bash commands matching
  env / printenv / echo $ / bare ``set``.

The scripts are exercised via subprocess with a mock tool-input JSON
on stdin. These same scripts run inside the mars-runtime image via
``--settings /app/claude_code_settings.json``; this test file is the
single-source-of-truth regression guard.

Also validates the settings.json shape so we fail fast on schema drift.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_DIR = _REPO_ROOT / "apps" / "mars-runtime" / "hooks"
_SETTINGS_JSON = _REPO_ROOT / "apps" / "mars-runtime" / "claude_code_settings.json"

_DENY_EDIT = _HOOKS_DIR / "deny-protected-edit.sh"
_DENY_BASH = _HOOKS_DIR / "deny-secret-read-bash.sh"


def _run_hook(script: Path, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Settings JSON shape
# ---------------------------------------------------------------------------


def test_settings_json_exists_and_is_valid():
    assert _SETTINGS_JSON.exists()
    data = json.loads(_SETTINGS_JSON.read_text())
    assert data["permissionMode"] == "acceptEdits"
    assert "hooks" in data and "PreToolUse" in data["hooks"]


def test_settings_json_wires_both_hook_scripts():
    data = json.loads(_SETTINGS_JSON.read_text())
    pretooluse = data["hooks"]["PreToolUse"]
    assert isinstance(pretooluse, list)
    by_matcher = {entry["matcher"]: entry for entry in pretooluse}

    # Edit-family matcher fires the protected-edit script
    assert "Edit|Write|MultiEdit" in by_matcher
    edit_entry = by_matcher["Edit|Write|MultiEdit"]
    assert edit_entry["hooks"][0]["command"].endswith("deny-protected-edit.sh")
    assert edit_entry["hooks"][0]["type"] == "command"

    # Bash matcher fires the secret-read script
    assert "Bash" in by_matcher
    bash_entry = by_matcher["Bash"]
    assert bash_entry["hooks"][0]["command"].endswith("deny-secret-read-bash.sh")
    assert bash_entry["hooks"][0]["type"] == "command"


def test_settings_json_command_paths_match_image_layout():
    """The container copies hooks/ to /app/hooks so the settings.json
    paths must use the absolute /app/hooks prefix."""
    data = json.loads(_SETTINGS_JSON.read_text())
    for entry in data["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            assert hook["command"].startswith("/app/hooks/"), hook


# ---------------------------------------------------------------------------
# deny-protected-edit.sh
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "file_path",
    [
        "/workspace/CLAUDE.md",
        "CLAUDE.md",
        "/nested/path/CLAUDE.md",
        "/workspace/AGENTS.md",
        "AGENTS.md",
        "/app/claude_code_settings.json",
    ],
)
def test_deny_protected_edit_blocks_admin_files(file_path: str):
    result = _run_hook(_DENY_EDIT, {"tool_input": {"file_path": file_path}})
    assert result.returncode == 2, result.stderr
    assert "admin-only" in result.stderr
    assert Path(file_path).name in result.stderr


@pytest.mark.parametrize(
    "file_path",
    [
        "/workspace/main.py",
        "/workspace/src/session/manager.py",
        "README.md",
        "notes/claude-musings.md",  # similar but not CLAUDE.md
        "not_CLAUDE.md",  # substring but not exact basename
    ],
)
def test_deny_protected_edit_allows_other_files(file_path: str):
    result = _run_hook(_DENY_EDIT, {"tool_input": {"file_path": file_path}})
    assert result.returncode == 0, result.stderr


def test_deny_protected_edit_allows_missing_file_path_field():
    """If the tool_input lacks file_path entirely, the hook has nothing
    to check and should not interfere."""
    result = _run_hook(_DENY_EDIT, {"tool_input": {}})
    assert result.returncode == 0


def test_deny_protected_edit_allows_empty_payload():
    result = _run_hook(_DENY_EDIT, {})
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# deny-secret-read-bash.sh
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "env",
        "env | head",
        "env > /tmp/secrets",
        "printenv",
        "printenv SECRET",
        "echo $SECRET_KEY",
        "echo  $FOO",  # extra whitespace
        "set",
        "set ",  # trailing whitespace still bare set
    ],
)
def test_deny_secret_read_bash_blocks_denylist(command: str):
    result = _run_hook(_DENY_BASH, {"tool_input": {"command": command}})
    assert result.returncode == 2, (
        f"expected {command!r} to be blocked, got rc={result.returncode}"
    )
    assert "secret-read denylist" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "pytest",
        "python3 main.py",
        "echo hello",  # no $ after
        "echo prepared-content",  # word starting with "prep..."
        "cat file.txt",
        "grep -r env /src",  # env as argument, not command
        "sed 's/env/ENV/g' file",  # env in string literal
        "curl https://example.com/envelope",  # "env" inside another word
        "set -euo pipefail",  # set with args (not a bare dump)
    ],
)
def test_deny_secret_read_bash_allows_benign_commands(command: str):
    result = _run_hook(_DENY_BASH, {"tool_input": {"command": command}})
    assert result.returncode == 0, (
        f"expected {command!r} to be allowed, got rc={result.returncode} "
        f"stderr={result.stderr}"
    )


def test_deny_secret_read_bash_allows_missing_command_field():
    result = _run_hook(_DENY_BASH, {"tool_input": {}})
    assert result.returncode == 0


def test_deny_secret_read_bash_allows_empty_payload():
    result = _run_hook(_DENY_BASH, {})
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# Hook scripts are executable + present
# ---------------------------------------------------------------------------


def test_hook_scripts_are_executable():
    import os

    for script in (_DENY_EDIT, _DENY_BASH):
        assert script.exists(), script
        assert os.access(script, os.X_OK), script
