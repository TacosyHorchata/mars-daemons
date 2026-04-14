"""Git helpers for the workspace — the "world state" memory.

The supervisor (agent loop) commits to this repo at the end of every
clean turn. The LLM never sees these calls as tools; they run as plain
Python outside the agent's reach so the audit history is non-negotiable.

Commits are never amended and never empty. If the agent didn't touch
files during a turn, no commit is made (`commit_turn` returns None).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=check,
    )


def init_if_needed(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    dot_git = workspace / ".git"
    if dot_git.exists() and not dot_git.is_dir():
        raise RuntimeError(
            f"{dot_git} exists but is not a directory — refusing to init over it"
        )
    if dot_git.is_dir():
        return
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "user.email", "agent@mars")
    _git(workspace, "config", "user.name", "mars-agent")


def commit_turn(workspace: Path, turn_number: int, preview: str) -> str | None:
    """Stage everything and commit. Returns sha, or None if nothing changed."""
    _git(workspace, "add", "-A")

    # exit 1 = there are staged changes; exit 0 = no changes.
    diff = _git(workspace, "diff", "--cached", "--quiet", check=False)
    if diff.returncode == 0:
        return None

    message = f"turn {turn_number}: {preview}".strip()
    _git(workspace, "commit", "-q", "-m", message)
    rev = _git(workspace, "rev-parse", "HEAD")
    return rev.stdout.strip()
