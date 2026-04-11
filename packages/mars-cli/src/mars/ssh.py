"""``mars ssh <agent>`` — open an SSH console inside a deployed daemon's Fly machine.

Thin wrapper around ``flyctl ssh console -a <app>``. We deliberately
shell out instead of reimplementing SSH: ``flyctl`` already owns the
wireguard bootstrap, the auth handshake, and the interactive TTY
plumbing. Mars just computes the app name from the agent name
(``mars-<agent>`` matching ``mars deploy``) and execs ``flyctl``.

Exits:

* **127** if ``flyctl`` is not on ``PATH``.
* **inherits flyctl's exit code** on every other path — we use
  :func:`os.execvp`, which replaces the current process, so this
  script never returns on success.
"""

from __future__ import annotations

import os
import sys

import click

__all__ = ["APP_NAME_PREFIX", "FLYCTL_ENV", "ssh_command"]

#: Matches the convention in :mod:`mars.deploy`.
APP_NAME_PREFIX = "mars-"

#: Env var that overrides the ``flyctl`` binary name (useful when
#: multiple versions are installed or for testing).
FLYCTL_ENV = "FLYCTL"


@click.command("ssh")
@click.argument("agent_name")
@click.option(
    "--app",
    "app_name_override",
    default=None,
    help="Override the Fly app name (default: 'mars-<agent_name>').",
)
def ssh_command(agent_name: str, app_name_override: str | None) -> None:
    """Open an interactive SSH console inside the daemon's Fly machine."""
    app_name = app_name_override or f"{APP_NAME_PREFIX}{agent_name}"
    flyctl_bin = os.environ.get(FLYCTL_ENV, "flyctl")
    cmd = [flyctl_bin, "ssh", "console", "-a", app_name]

    click.echo(f"$ {' '.join(cmd)}")
    try:
        os.execvp(flyctl_bin, cmd)
    except FileNotFoundError:
        raise click.ClickException(
            f"{flyctl_bin} not found on PATH — install flyctl first "
            "(curl -L https://fly.io/install.sh | sh)"
        )
    except OSError as exc:
        raise click.ClickException(f"failed to exec {flyctl_bin}: {exc}") from exc
    # execvp replaces the current process on success; this line is
    # unreachable in practice but keeps static checkers quiet.
    sys.exit(0)
