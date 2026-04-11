"""`mars init` — scaffold a starter agent.yaml in the current directory."""

from __future__ import annotations

from pathlib import Path

import click

TEMPLATE = """# Mars daemon spec — see docs/agent-yaml-spec.md.
# Edit the description and system prompt, then `mars deploy ./agent.yaml`.
name: my-daemon
description: A Mars daemon. Edit this description to explain what it does.
runtime: claude-code
system_prompt_path: ./CLAUDE.md
workdir: /workspace/my-daemon
mcps: []
env: []
tools:
  - Read
  - Bash
"""


@click.command("init")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite agent.yaml if it already exists.",
)
def init_command(force: bool) -> None:
    """Scaffold a starter agent.yaml in the current directory."""
    target = Path.cwd() / "agent.yaml"
    if target.exists() and not force:
        raise click.ClickException(
            f"{target} already exists. Re-run with --force to overwrite."
        )
    target.write_text(TEMPLATE, encoding="utf-8")
    click.echo(f"Created {target}")
    click.echo("Next: edit the file and run `mars deploy ./agent.yaml`.")
