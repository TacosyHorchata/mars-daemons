"""``mars edit-prompt <agent> <session-id>`` — update a daemon's CLAUDE.md.

Two workflows, one command:

* ``--file some-claude.md`` — read the new prompt content from a file
  and PATCH it to ``mars-control``. For scripted / CI use.
* No ``--file`` — open ``$EDITOR`` on a temporary file pre-seeded
  with a one-line comment header, collect the user's edited content
  when they save + exit, then PATCH. For interactive use.

Either way the actual update hits
``PATCH /agents/{agent}/prompt`` on ``MARS_CONTROL_URL`` (or
``--control-url``), which forwards to the supervisor's
``POST /sessions/{id}/reload-prompt`` — same path exercised in Story 6.4.

The ``session_id`` is required because one workspace can host many
live sessions of the same agent (multi-session is Epic 5). For now
Pedro types it manually; when Epic 5 lands, a ``mars sessions``
command will enumerate live ids.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import click
import httpx

__all__ = ["edit_prompt_command"]


MARS_CONTROL_URL_ENV = "MARS_CONTROL_URL"
EDITOR_ENV = "EDITOR"
DEFAULT_EDITOR = "vi"
_EDITOR_HEADER = (
    "# Edit this file and save to update the daemon's system prompt.\n"
    "# Lines starting with '#' are kept as-is — Claude Code reads the\n"
    "# entire contents of CLAUDE.md including comments.\n"
    "\n"
)


def _open_in_editor(
    initial_content: str,
    *,
    editor_launcher: Callable[[list[str]], int] | None = None,
) -> str | None:
    """Write ``initial_content`` to a temp file, launch ``$EDITOR`` on
    it, return the file's contents after the editor exits.

    ``editor_launcher`` is injected by tests to avoid actually
    forking ``vi``. Production uses :func:`subprocess.call`.
    """
    editor = os.environ.get(EDITOR_ENV, DEFAULT_EDITOR)
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".md", encoding="utf-8"
    ) as tmp:
        tmp.write(initial_content)
        tmp_path = Path(tmp.name)

    launcher = editor_launcher or (lambda cmd: subprocess.call(cmd))
    try:
        rc = launcher([editor, str(tmp_path)])
        if rc != 0:
            return None
        return tmp_path.read_text(encoding="utf-8")
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _strip_header_comments(content: str) -> str:
    """Remove leading comment lines that match the editor header so
    the PATCH body carries only what the user actually edited.

    Only strips lines at the very top that start with ``#`` — any
    comments the user added lower down stay.
    """
    lines = content.splitlines(keepends=True)
    i = 0
    while i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    # Also eat a single trailing blank line that the header emits
    if i < len(lines) and lines[i].strip() == "":
        i += 1
    return "".join(lines[i:])


def _do_patch(
    *,
    control_url: str,
    agent_name: str,
    session_id: str,
    content: str,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Issue the PATCH request. Raises :class:`click.ClickException`
    on any failure so the CLI user sees a clean error line."""
    url = f"{control_url.rstrip('/')}/agents/{agent_name}/prompt"
    owned = http_client is None
    client = http_client or httpx.Client(timeout=30.0)
    try:
        try:
            resp = client.patch(
                url, json={"session_id": session_id, "content": content}
            )
        except httpx.RequestError as exc:
            raise click.ClickException(
                f"failed to reach mars-control at {url}: {exc}"
            ) from exc
    finally:
        if owned:
            client.close()

    if resp.status_code >= 400:
        raise click.ClickException(
            f"mars-control returned {resp.status_code}: {resp.text[:500]}"
        )
    try:
        return resp.json()
    except ValueError:
        return {}


@click.command("edit-prompt")
@click.argument("agent_name")
@click.argument("session_id")
@click.option(
    "--file",
    "content_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read the new prompt content from this file (skip $EDITOR).",
)
@click.option(
    "--control-url",
    "control_url_override",
    default=None,
    help=f"Mars control plane base URL. Defaults to ${MARS_CONTROL_URL_ENV}.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the content that would be sent and exit without calling mars-control.",
)
@click.option(
    "--editor-launcher",
    "editor_launcher",
    hidden=True,
    default=None,
    help="[test only] injected callable to replace $EDITOR forking.",
)
@click.option(
    "--http-client",
    "http_client",
    hidden=True,
    default=None,
    help="[test only] injected httpx.Client.",
)
def edit_prompt_command(
    agent_name: str,
    session_id: str,
    content_file: Path | None,
    control_url_override: str | None,
    dry_run: bool,
    editor_launcher: Callable[[list[str]], int] | None,
    http_client: httpx.Client | None,
) -> None:
    """Update a running daemon's CLAUDE.md via the admin edit flow."""
    control_url = control_url_override or os.environ.get(MARS_CONTROL_URL_ENV, "")
    if not control_url and not dry_run:
        raise click.ClickException(
            f"control plane URL missing — pass --control-url or set ${MARS_CONTROL_URL_ENV}"
        )

    if content_file is not None:
        content = content_file.read_text(encoding="utf-8")
    else:
        raw = _open_in_editor(_EDITOR_HEADER, editor_launcher=editor_launcher)
        if raw is None:
            raise click.ClickException("editor aborted — no changes saved")
        content = _strip_header_comments(raw).strip()
        if not content:
            raise click.ClickException(
                "empty prompt after stripping the header — aborting"
            )

    if dry_run:
        click.echo(
            f"Dry-run: would PATCH /agents/{agent_name}/prompt with "
            f"session_id={session_id!r} and content=\n---\n{content}\n---"
        )
        return

    result = _do_patch(
        control_url=control_url,
        agent_name=agent_name,
        session_id=session_id,
        content=content,
        http_client=http_client,
    )
    click.echo(
        f"✓ prompt updated for agent={agent_name} session={session_id}"
    )
    if result:
        click.echo(f"  supervisor response: {result}")
