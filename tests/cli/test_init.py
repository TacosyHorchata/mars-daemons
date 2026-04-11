"""Tests for `mars init` — must produce an agent.yaml that AgentConfig accepts."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from mars.__main__ import cli
from schema.agent import AgentConfig


def test_init_creates_parseable_agent_yaml():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0, result.output
        assert Path("agent.yaml").exists()
        cfg = AgentConfig.parse_file("agent.yaml")
        assert cfg.runtime == "claude-code"
        assert cfg.name == "my-daemon"


def test_init_refuses_to_overwrite_existing_file():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("agent.yaml").write_text("name: existing\n")
        result = runner.invoke(cli, ["init"])
        assert result.exit_code != 0
        assert "already exists" in result.output
        # Existing file must be untouched.
        assert Path("agent.yaml").read_text() == "name: existing\n"


def test_init_force_overwrites_existing_file():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("agent.yaml").write_text("stale content\n")
        result = runner.invoke(cli, ["init", "--force"])
        assert result.exit_code == 0, result.output
        cfg = AgentConfig.parse_file("agent.yaml")
        assert cfg.name == "my-daemon"


def test_cli_help_lists_init_subcommand():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output
