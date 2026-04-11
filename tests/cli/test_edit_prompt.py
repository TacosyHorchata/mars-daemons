"""Unit tests for ``mars edit-prompt`` (Story 6.5 CLI half)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from mars.edit_prompt import (
    _EDITOR_HEADER,
    _strip_header_comments,
    edit_prompt_command,
)

AGENT = "pr-reviewer"
SESSION_ID = "mars-s-1"
CONTROL_URL = "https://mars-control.example"


# ---------------------------------------------------------------------------
# _strip_header_comments
# ---------------------------------------------------------------------------


def test_strip_header_removes_the_emitted_header():
    content = _EDITOR_HEADER + "You are a helpful assistant."
    stripped = _strip_header_comments(content)
    assert stripped == "You are a helpful assistant."


def test_strip_header_removes_only_leading_comments():
    content = (
        "# leading comment\n"
        "# another leading\n"
        "\n"
        "Actual prompt.\n"
        "# Inline comment that stays\n"
        "More prompt.\n"
    )
    stripped = _strip_header_comments(content)
    assert stripped.startswith("Actual prompt.")
    assert "# Inline comment that stays" in stripped


def test_strip_header_handles_no_header():
    content = "Just a prompt with no header."
    assert _strip_header_comments(content) == content


def test_strip_header_handles_only_header():
    content = "# comment 1\n# comment 2\n\n"
    assert _strip_header_comments(content) == ""


# ---------------------------------------------------------------------------
# --file path — scripted edit workflow
# ---------------------------------------------------------------------------


def test_edit_prompt_with_file_patches_mars_control(tmp_path: Path):
    new_prompt_file = tmp_path / "new-claude.md"
    new_prompt_file.write_text("You are Pedro's PR reviewer.\n")

    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"agent_name": AGENT, "session_id": SESSION_ID, "result": {}},
        )

    http = httpx.Client(transport=httpx.MockTransport(_handler))

    # Direct-callback invocation: Click can't serialize a live
    # httpx.Client through an option, so we bypass the CLI parser
    # for the injected client (same pattern as mars memory export).
    edit_prompt_command.callback(
        agent_name=AGENT,
        session_id=SESSION_ID,
        content_file=new_prompt_file,
        control_url_override=CONTROL_URL,
        dry_run=False,
        editor_launcher=None,
        http_client=http,
    )
    assert seen["method"] == "PATCH"
    assert seen["url"].endswith(f"/agents/{AGENT}/prompt")
    assert seen["body"] == {
        "session_id": SESSION_ID,
        "content": "You are Pedro's PR reviewer.\n",
    }


def test_edit_prompt_missing_control_url_errors(tmp_path: Path):
    prompt = tmp_path / "p.md"
    prompt.write_text("hi")

    runner = CliRunner(env={})  # no MARS_CONTROL_URL
    result = runner.invoke(
        edit_prompt_command,
        [AGENT, SESSION_ID, "--file", str(prompt)],
    )
    assert result.exit_code != 0
    assert "control plane URL missing" in result.output


def test_edit_prompt_file_nonzero_status_errors(tmp_path: Path):
    prompt = tmp_path / "p.md"
    prompt.write_text("hi")

    def _handler(request):
        return httpx.Response(502, text="supervisor unreachable")

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    with pytest.raises(Exception) as exc_info:
        edit_prompt_command.callback(
            agent_name=AGENT,
            session_id=SESSION_ID,
            content_file=prompt,
            control_url_override=CONTROL_URL,
            dry_run=False,
            editor_launcher=None,
            http_client=http,
        )
    assert "502" in str(exc_info.value)


def test_edit_prompt_transport_error_bubbles_up(tmp_path: Path):
    prompt = tmp_path / "p.md"
    prompt.write_text("hi")

    def _handler(request):
        raise httpx.ConnectError("control plane is down")

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    with pytest.raises(Exception) as exc_info:
        edit_prompt_command.callback(
            agent_name=AGENT,
            session_id=SESSION_ID,
            content_file=prompt,
            control_url_override=CONTROL_URL,
            dry_run=False,
            editor_launcher=None,
            http_client=http,
        )
    assert "failed to reach mars-control" in str(exc_info.value)


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_edit_prompt_dry_run_never_calls_http(tmp_path: Path):
    prompt = tmp_path / "p.md"
    prompt.write_text("draft content")

    def _handler(request):
        raise AssertionError("dry-run should not hit the network")

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    runner = CliRunner(env={})
    result = runner.invoke(
        edit_prompt_command,
        [
            AGENT,
            SESSION_ID,
            "--file",
            str(prompt),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Dry-run" in result.output
    assert "draft content" in result.output


# ---------------------------------------------------------------------------
# Editor launcher path — no real vi fork
# ---------------------------------------------------------------------------


def test_edit_prompt_editor_path_captures_saved_content(tmp_path: Path):
    """The editor launcher stub overwrites the temp file with the
    user's final content, then we verify the PATCH gets the right
    stripped text."""
    captured: dict = {}

    def _fake_editor(cmd: list[str]) -> int:
        # cmd is [editor, tmpfile]; simulate a save
        tmpfile = Path(cmd[-1])
        tmpfile.write_text(
            _EDITOR_HEADER + "Be thorough when reviewing PRs."
        )
        return 0

    def _handler(request):
        body = json.loads(request.content)
        captured["content"] = body["content"]
        return httpx.Response(
            200, json={"supervisor_response": "restart ok"}
        )

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    edit_prompt_command.callback(
        agent_name=AGENT,
        session_id=SESSION_ID,
        content_file=None,
        control_url_override=CONTROL_URL,
        dry_run=False,
        editor_launcher=_fake_editor,
        http_client=http,
    )
    assert captured["content"] == "Be thorough when reviewing PRs."


def test_edit_prompt_editor_abort_errors():
    """Non-zero exit from the editor signals "user canceled"."""

    def _fake_editor(cmd: list[str]) -> int:
        return 1

    with pytest.raises(Exception) as exc_info:
        edit_prompt_command.callback(
            agent_name=AGENT,
            session_id=SESSION_ID,
            content_file=None,
            control_url_override=CONTROL_URL,
            dry_run=False,
            editor_launcher=_fake_editor,
            http_client=None,
        )
    assert "editor aborted" in str(exc_info.value)


def test_edit_prompt_empty_after_header_errors(tmp_path: Path):
    """If the user saved the file with only the header still in
    place, the post-strip content is empty — refuse to send an
    empty prompt."""

    def _fake_editor(cmd: list[str]) -> int:
        # Don't touch the file — the header is all that's there
        return 0

    with pytest.raises(Exception) as exc_info:
        edit_prompt_command.callback(
            agent_name=AGENT,
            session_id=SESSION_ID,
            content_file=None,
            control_url_override=CONTROL_URL,
            dry_run=False,
            editor_launcher=_fake_editor,
            http_client=None,
        )
    assert "empty prompt" in str(exc_info.value)
