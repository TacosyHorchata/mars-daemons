"""Mars CLI entrypoint. Invoke via `python -m mars <command>` or the `mars` script."""

from __future__ import annotations

import click

from mars.deploy import deploy_command
from mars.init import init_command
from mars.ssh import ssh_command


@click.group()
@click.version_option(package_name="mars-daemons", prog_name="mars")
def cli() -> None:
    """Mars — deploy AI daemons to the cloud."""


cli.add_command(init_command)
cli.add_command(deploy_command)
cli.add_command(ssh_command)


if __name__ == "__main__":
    cli()
