"""``mars deploy`` — create a Fly.io machine from an ``agent.yaml``.

Flow:

1. Parse + validate the agent.yaml with :class:`schema.agent.AgentConfig`.
2. Build the app name (``mars-<name>`` by default or ``--app`` override).
3. Ensure the Fly app exists (create if missing; tolerate 422
   "already exists").
4. Launch a ``mars-runtime`` machine on that app with:
   * image: ``--image`` or the published ``ghcr.io/.../mars-runtime:latest``
   * env: MARS_EVENT_SECRET, MARS_CONTROL_URL, MARS_DEFAULT_AGENT_YAML
     (the full yaml text), plus every env var declared in
     ``AgentConfig.env`` that exists in the caller's shell.
   * region: ``--region`` (defaults to ``iad``)
   * guest: small Fly box (1 shared vCPU, 1 GB RAM)
   * service: the supervisor's port 8080 mapped to HTTPS 443 / HTTP 80
5. Print the public URL (``https://<app>.fly.dev``) so the user can
   curl the supervisor's ``/health`` endpoint to verify.

Out of scope (deferred):

* POSTing the agent.yaml to the running supervisor — v1 bakes the
  yaml as an env var so the supervisor can auto-spawn its default
  session on startup (wiring lands in a later story / supervisor
  enhancement). ``mars deploy`` stays idempotent + stateless
  machine-side.
* Multi-session per machine — Epic 5 adds that path.
* Streaming-real-time deploy progress — the output is straight-line
  "created app", "launched machine", "ready at <url>".

Authentication:

* ``--fly-token`` or ``FLY_API_TOKEN`` env var (required).
* ``--event-secret`` or ``MARS_EVENT_SECRET`` env var (required).
* ``--control-url`` or ``MARS_CONTROL_URL`` env var (optional —
  defaults to an empty string and the supervisor treats "no control
  plane" as local-only mode).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import click

from mars.fly.client import FlyApiError, FlyClient
from schema.agent import AgentConfig

__all__ = ["deploy_command"]


DEFAULT_IMAGE = "ghcr.io/tacosyhorchata/mars-runtime:latest"
DEFAULT_REGION = "iad"
DEFAULT_ORG_SLUG = "personal"
DEFAULT_SUPERVISOR_PORT = 8080
FLY_API_TOKEN_ENV = "FLY_API_TOKEN"
MARS_EVENT_SECRET_ENV = "MARS_EVENT_SECRET"
MARS_CONTROL_URL_ENV = "MARS_CONTROL_URL"


def _build_machine_config(
    *,
    image: str,
    env: dict[str, str],
    supervisor_port: int = DEFAULT_SUPERVISOR_PORT,
) -> dict[str, Any]:
    """Build the ``config`` dict Fly expects on ``POST /machines``.

    Exposed as a module-level helper so the unit tests can assert the
    exact shape without going through Click / FlyClient.
    """
    return {
        "services": [
            {
                "internal_port": supervisor_port,
                "protocol": "tcp",
                "ports": [
                    {"port": 443, "handlers": ["tls", "http"]},
                    {"port": 80, "handlers": ["http"]},
                ],
            }
        ],
        "restart": {"policy": "on-failure", "max_retries": 3},
        "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 1024},
    }


def _assemble_env(
    *,
    agent_yaml_text: str,
    event_secret: str,
    control_url: str,
    declared_env_names: list[str],
    host_env: dict[str, str],
) -> dict[str, str]:
    """Build the env dict that gets baked into ``config.env``.

    Includes the Mars-wired vars (secret, control URL, default agent
    yaml) plus any secrets named in ``AgentConfig.env`` that exist in
    the caller's shell. Missing host secrets are silently skipped —
    the supervisor fails at tool-call time if a required secret is
    missing, which is easier to debug than a mass deploy failure.
    """
    env: dict[str, str] = {
        "MARS_EVENT_SECRET": event_secret,
        "MARS_DEFAULT_AGENT_YAML": agent_yaml_text,
    }
    if control_url:
        env["MARS_CONTROL_URL"] = control_url
    for name in declared_env_names:
        value = host_env.get(name)
        if value is not None:
            env[name] = value
    return env


def _resolve_option(
    value: str | None, env_name: str, env: dict[str, str]
) -> str:
    return value if value is not None else env.get(env_name, "")


@click.command("deploy")
@click.argument("agent_yaml", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--image",
    default=None,
    help=f"Override mars-runtime image ref (default: {DEFAULT_IMAGE}).",
)
@click.option(
    "--region",
    default=DEFAULT_REGION,
    show_default=True,
    help="Fly.io region for the machine.",
)
@click.option(
    "--org",
    "org_slug",
    default=DEFAULT_ORG_SLUG,
    show_default=True,
    help="Fly.io org slug for new apps.",
)
@click.option(
    "--app",
    "app_name_override",
    default=None,
    help="Override the computed Fly app name (default: 'mars-<agent.name>').",
)
@click.option(
    "--fly-token",
    "fly_token_override",
    default=None,
    help=f"Fly API token. Defaults to ${FLY_API_TOKEN_ENV}.",
)
@click.option(
    "--event-secret",
    "event_secret_override",
    default=None,
    help=f"Shared secret for the event forwarder. Defaults to ${MARS_EVENT_SECRET_ENV}.",
)
@click.option(
    "--control-url",
    "control_url_override",
    default=None,
    help=(
        "Mars control plane base URL that the runtime forwards events "
        f"to. Defaults to ${MARS_CONTROL_URL_ENV}. Empty string = local."
    ),
)
def deploy_command(
    agent_yaml: Path,
    image: str | None,
    region: str,
    org_slug: str,
    app_name_override: str | None,
    fly_token_override: str | None,
    event_secret_override: str | None,
    control_url_override: str | None,
) -> None:
    """Deploy an agent.yaml to a fresh Fly.io machine.

    Prints the Fly app URL on success; exits 2 on missing config and
    non-zero (raising :class:`FlyApiError`) on Fly-side errors.
    """
    try:
        config = AgentConfig.from_yaml_file(agent_yaml)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"invalid agent.yaml: {exc}") from exc

    host_env = dict(os.environ)
    fly_token = _resolve_option(fly_token_override, FLY_API_TOKEN_ENV, host_env)
    if not fly_token:
        raise click.ClickException(
            f"Fly API token missing — pass --fly-token or set ${FLY_API_TOKEN_ENV}"
        )
    event_secret = _resolve_option(
        event_secret_override, MARS_EVENT_SECRET_ENV, host_env
    )
    if not event_secret:
        raise click.ClickException(
            f"Mars event secret missing — pass --event-secret or set ${MARS_EVENT_SECRET_ENV}"
        )
    control_url = _resolve_option(
        control_url_override, MARS_CONTROL_URL_ENV, host_env
    )

    app_name = app_name_override or f"mars-{config.name}"
    image_ref = image or DEFAULT_IMAGE
    agent_yaml_text = Path(agent_yaml).read_text(encoding="utf-8")

    env = _assemble_env(
        agent_yaml_text=agent_yaml_text,
        event_secret=event_secret,
        control_url=control_url,
        declared_env_names=list(config.env),
        host_env=host_env,
    )
    machine_config_overrides = _build_machine_config(
        image=image_ref, env=env, supervisor_port=DEFAULT_SUPERVISOR_PORT
    )
    machine_name = f"{config.name}-supervisor"

    async def _deploy() -> dict[str, Any]:
        async with FlyClient(api_token=fly_token) as fly:
            try:
                await fly.create_app(app_name, org_slug=org_slug)
                click.echo(f"✓ created Fly app: {app_name}")
            except FlyApiError as exc:
                if exc.status_code in (409, 422):
                    click.echo(f"· Fly app already exists: {app_name}")
                else:
                    raise

            machine = await fly.create_machine(
                app_name,
                image=image_ref,
                env=env,
                region=region,
                name=machine_name,
                extra_config=machine_config_overrides,
            )
            click.echo(
                f"✓ launched Fly machine: {machine.get('id', '<unknown>')}"
            )
            return machine

    try:
        asyncio.run(_deploy())
    except FlyApiError as exc:
        raise click.ClickException(f"Fly API error: {exc}") from exc

    public_url = f"https://{app_name}.fly.dev"
    click.echo("")
    click.echo(f"Deploy complete. Supervisor health: {public_url}/health")
    click.echo(f"When the control plane is live, chat URL: {public_url}/")
    sys.stdout.flush()
