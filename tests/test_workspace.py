"""Git workspace helper tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mars_runtime.storage import workspace


def _git_log_count(ws: Path) -> int:
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def test_init_creates_git_repo_in_empty_dir(tmp_path: Path):
    ws = tmp_path / "ws"
    workspace.init_if_needed(ws)
    assert (ws / ".git").is_dir()


def test_init_is_idempotent(tmp_path: Path):
    ws = tmp_path / "ws"
    workspace.init_if_needed(ws)
    head_before = (ws / ".git" / "HEAD").read_text()
    workspace.init_if_needed(ws)
    assert (ws / ".git" / "HEAD").read_text() == head_before


def test_commit_turn_with_changes_returns_sha(tmp_path: Path):
    ws = tmp_path / "ws"
    workspace.init_if_needed(ws)
    (ws / "hello.txt").write_text("hi")

    sha = workspace.commit_turn(ws, turn_number=1, preview="first change")

    assert sha is not None
    assert len(sha) == 40
    assert _git_log_count(ws) == 1


def test_commit_turn_without_changes_returns_none(tmp_path: Path):
    ws = tmp_path / "ws"
    workspace.init_if_needed(ws)

    assert workspace.commit_turn(ws, turn_number=1, preview="nothing") is None
    # And no commit was made.
    result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=ws,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""


def test_consecutive_commits_increment_log(tmp_path: Path):
    ws = tmp_path / "ws"
    workspace.init_if_needed(ws)

    (ws / "a.txt").write_text("a")
    sha1 = workspace.commit_turn(ws, 1, "added a")
    (ws / "b.txt").write_text("b")
    sha2 = workspace.commit_turn(ws, 2, "added b")

    assert sha1 != sha2
    assert _git_log_count(ws) == 2


def test_commit_message_includes_turn_and_preview(tmp_path: Path):
    ws = tmp_path / "ws"
    workspace.init_if_needed(ws)
    (ws / "c.txt").write_text("c")
    workspace.commit_turn(ws, 7, "review PR #42")

    result = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "turn 7: review PR #42"
