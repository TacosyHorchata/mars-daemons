"""``mars memory export <agent>`` — download the latest S3 memory bundle.

For v1, bundles land in S3 at
``s3://<bucket>/<workspace>/<agent>/<session>/<timestamp>.tar.gz``.
This command lists the prefix for the given agent, picks the most
recent key under any session, and downloads it to local disk so the
user can ``tar tzvf`` it and review captured history.

Env contract:

* ``MARS_MEMORY_BUCKET`` — S3 bucket name (or ``--bucket`` override).
* ``MARS_MEMORY_WORKSPACE`` — workspace segment (default ``default``).
* AWS credentials resolved by boto3's default chain (env vars,
  ``~/.aws/config``, EC2 / Fly instance role, etc.).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import click

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]

__all__ = ["memory_export_command"]


def _default_workspace() -> str:
    return os.environ.get("MARS_MEMORY_WORKSPACE", "default")


def _resolve_bucket(override: str | None) -> str:
    if override:
        return override
    env_val = os.environ.get("MARS_MEMORY_BUCKET")
    if not env_val:
        raise click.ClickException(
            "memory bucket not configured — pass --bucket or set MARS_MEMORY_BUCKET"
        )
    return env_val


def _list_all_keys(s3: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    """Paginate through every object under ``prefix`` in ``bucket``."""
    keys: list[dict[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            keys.append(obj)
    return keys


def _pick_latest_bundle(
    s3: Any, *, bucket: str, prefix: str
) -> dict[str, Any] | None:
    """Find the most recent memory bundle under the prefix.

    Objects are keyed with an ISO timestamp in the filename, and S3's
    ``LastModified`` is authoritative for tie-breaks.
    """
    keys = _list_all_keys(s3, bucket, prefix)
    tarballs = [k for k in keys if k["Key"].endswith(".tar.gz")]
    if not tarballs:
        return None
    tarballs.sort(key=lambda k: (k["LastModified"], k["Key"]))
    return tarballs[-1]


@click.command("memory")
@click.argument("subcommand", type=click.Choice(["export"]))
@click.argument("agent_name")
@click.option("--bucket", default=None, help="S3 bucket (default: $MARS_MEMORY_BUCKET).")
@click.option(
    "--workspace",
    default=None,
    help=(
        "Workspace segment of the S3 prefix (default: $MARS_MEMORY_WORKSPACE "
        "or 'default')."
    ),
)
@click.option(
    "--dest",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Local output path (default: './<agent>-memory-<ts>.tar.gz').",
)
@click.option(
    "--s3-client",
    hidden=True,
    default=None,
    help="[test only] inject a pre-built boto3 s3 client.",
)
def memory_export_command(
    subcommand: str,
    agent_name: str,
    bucket: str | None,
    workspace: str | None,
    dest: Path | None,
    s3_client: Any | None,
) -> None:
    """Download an agent's latest memory bundle from S3."""
    del subcommand  # only "export" for v1

    effective_bucket = _resolve_bucket(bucket)
    effective_workspace = workspace or _default_workspace()
    prefix = f"{effective_workspace}/{agent_name}/"

    if s3_client is None:
        if boto3 is None:  # pragma: no cover
            raise click.ClickException(
                "boto3 not installed — can't call AWS from the mars CLI"
            )
        s3_client = boto3.client("s3")

    latest = _pick_latest_bundle(s3_client, bucket=effective_bucket, prefix=prefix)
    if latest is None:
        raise click.ClickException(
            f"no memory bundles found under s3://{effective_bucket}/{prefix}"
        )

    key = latest["Key"]
    size = latest.get("Size", 0)
    click.echo(
        f"Latest bundle: s3://{effective_bucket}/{key}  ({size} bytes)"
    )

    target = dest or Path(f"./{agent_name}-memory-{_timestamp_from_key(key)}.tar.gz")
    target.parent.mkdir(parents=True, exist_ok=True)
    s3_client.download_file(effective_bucket, key, str(target))
    click.echo(f"Downloaded to {target}")


def _timestamp_from_key(key: str) -> str:
    """Extract the timestamp portion of a memory bundle key so local
    filenames remain sortable."""
    filename = key.rsplit("/", 1)[-1]
    return filename.rsplit(".tar.gz", 1)[0]
