"""Unit tests for ``mars ssh``.

:func:`os.execvp` is monkey-patched to capture the exec call instead
of actually replacing the current process.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from mars.ssh import ssh_command


@pytest.fixture
def capture_execvp(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_execvp(file: str, args: list[str]) -> None:
        captured["file"] = file
        captured["args"] = list(args)
        # execvp normally replaces the process and never returns; fake
        # returns None so the test continues executing.

    import mars.ssh as ssh_mod

    monkeypatch.setattr(ssh_mod.os, "execvp", _fake_execvp)
    return captured


def test_ssh_execs_flyctl_with_computed_app_name(capture_execvp):
    runner = CliRunner()
    result = runner.invoke(ssh_command, ["pr-reviewer"])
    assert result.exit_code == 0, result.output
    assert capture_execvp["file"] == "flyctl"
    assert capture_execvp["args"] == [
        "flyctl",
        "ssh",
        "console",
        "-a",
        "mars-pr-reviewer",
    ]


def test_ssh_respects_app_override(capture_execvp):
    runner = CliRunner()
    result = runner.invoke(
        ssh_command, ["any", "--app", "custom-app"]
    )
    assert result.exit_code == 0
    assert capture_execvp["args"][-1] == "custom-app"


def test_ssh_respects_FLYCTL_env_var(capture_execvp, monkeypatch):
    monkeypatch.setenv("FLYCTL", "/usr/local/bin/fly")
    runner = CliRunner()
    result = runner.invoke(ssh_command, ["pr-reviewer"])
    assert result.exit_code == 0
    assert capture_execvp["file"] == "/usr/local/bin/fly"
    assert capture_execvp["args"][0] == "/usr/local/bin/fly"


def test_ssh_reports_missing_flyctl(monkeypatch):
    def _fake_execvp(file, args):
        raise FileNotFoundError(file)

    import mars.ssh as ssh_mod

    monkeypatch.setattr(ssh_mod.os, "execvp", _fake_execvp)
    runner = CliRunner()
    result = runner.invoke(ssh_command, ["pr-reviewer"])
    assert result.exit_code != 0
    assert "not found on PATH" in result.output


def test_ssh_prints_command_before_execing(capture_execvp):
    runner = CliRunner()
    result = runner.invoke(ssh_command, ["pr-reviewer"])
    assert "flyctl ssh console -a mars-pr-reviewer" in result.output
