"""Host↔sandbox file transfer — operator-side CLI only.

This module is NOT reachable from the agent. It runs purely on the host
(the user's machine), copies files between local FS and the entorno
workspace, and does not touch broker/worker process lifecycle.

Subcommands:
  mars push <local> <dest>   — copy local file into $MARS_DATA_DIR/workspace
  mars pull <src>   <local>  — copy workspace file out

<dest>/<src> accepts either:
  - a path relative to the entorno workspace (local bind-mount path)
  - fly://<app-name>/<path-under-/data/workspace> — shells out to
    `fly ssh sftp shell -a APP` with `put`/`get` piped on stdin.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .._paths import data_dir as _data_dir


# Fly app names: lowercase alnum + hyphens.
_FLY_URL_RE = re.compile(r"^fly://([a-z0-9][a-z0-9-]*)/(.+)$")

# Agent-config file names that neither the agent (via its tools) nor the
# user (via push) may overwrite. Preserves the integrity of the deployed
# agent yaml + system prompt.
_PROTECTED_BASENAMES = frozenset({"CLAUDE.md", "AGENTS.md", "agent.yaml"})


def _parse_fly_url(s: str) -> tuple[str, str] | None:
    m = _FLY_URL_RE.match(s)
    if not m:
        return None
    return m.group(1), m.group(2)


def _validate_fly_remote(remote: str) -> None:
    """Apply path-confinement rules to Fly remote paths.

    - Absolute paths rejected (would escape /data/workspace prefix).
    - `..` segments rejected (remote SFTP may normalize and escape).
    - Whitespace / control chars rejected (would break the single-line
      SFTP command parser, possible injection).
    - Protected agent-config basenames refused.
    """
    if remote.startswith("/"):
        raise ValueError(
            f"Fly remote path must be relative to /data/workspace, "
            f"got absolute {remote!r}"
        )
    parts = [p for p in remote.split("/") if p]
    if any(p == ".." for p in parts):
        raise ValueError(
            f"Fly remote path may not contain '..' segments: {remote!r}"
        )
    if any(ord(c) < 0x20 or c in (" ", "\t") for c in remote):
        raise ValueError(
            f"Fly remote path contains whitespace or control characters: {remote!r}"
        )
    leaf = Path(remote).name
    if leaf in _PROTECTED_BASENAMES:
        raise ValueError(
            f"{leaf} is a protected agent config file — "
            "replace it by editing the yaml directly, not via push"
        )


def _confined_workspace_path(workspace: Path, rel_path: str) -> Path:
    """Resolve `rel_path` against `workspace` and guarantee it stays under
    the workspace root. Refuses absolute paths, ..-escapes, and protected
    basenames."""
    if Path(rel_path).is_absolute():
        raise ValueError(
            f"workspace paths must be relative to the entorno workspace, "
            f"got absolute {rel_path!r}"
        )
    resolved = (workspace / rel_path).resolve()
    ws = workspace.resolve()
    if ws != resolved and ws not in resolved.parents:
        raise ValueError(f"{rel_path!r} escapes workspace {ws}")
    if resolved.name in _PROTECTED_BASENAMES:
        raise ValueError(
            f"{resolved.name} is a protected agent config file — "
            "replace it by editing the yaml directly, not via push"
        )
    return resolved


def _run_fly_sftp(command: str, app: str, *paths: str, timeout: int = 180) -> None:
    """Run one sftp command via `fly ssh sftp shell -a APP`, piping
    `{command} {paths}\\nexit\\n` on stdin. Raises on non-zero exit."""
    stdin_script = f"{command} {' '.join(paths)}\nexit\n"
    try:
        subprocess.run(
            ["fly", "ssh", "sftp", "shell", "-a", app],
            input=stdin_script,
            text=True,
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "flyctl not found on PATH — install from https://fly.io/docs/hands-on/install-flyctl/"
        ) from e


def cmd_push(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="mars_runtime push")
    p.add_argument("local", help="Local file to upload")
    p.add_argument(
        "dest",
        help="Destination: relative workspace path (local) or fly://<app>/<path>",
    )
    p.add_argument("--data-dir", dest="data_dir", help="Override $MARS_DATA_DIR")
    args = p.parse_args(argv)

    local = Path(args.local).expanduser().resolve()
    if not local.exists():
        print(f"source not found: {local}", file=sys.stderr)
        return 1
    if not local.is_file():
        print(f"source must be a regular file: {local}", file=sys.stderr)
        return 1

    fly = _parse_fly_url(args.dest)
    if fly:
        app, remote = fly
        try:
            _validate_fly_remote(remote)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        if any(c in str(local) for c in (" ", "\t", "\n", "\r")):
            print(
                f"local path contains whitespace; Fly SFTP shell can't handle it safely: {local}",
                file=sys.stderr,
            )
            return 1
        try:
            _run_fly_sftp("put", app, str(local), f"/data/workspace/{remote}")
        except subprocess.CalledProcessError as e:
            print(
                f"fly sftp failed (exit {e.returncode}): {e.stderr.strip() if e.stderr else ''}",
                file=sys.stderr,
            )
            return 1
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"pushed {local} → fly://{app}/{remote}", file=sys.stderr)
        return 0

    dir_ = _data_dir(args.data_dir)
    workspace = dir_ / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        dst = _confined_workspace_path(workspace, args.dest)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local, dst)
    print(f"pushed {local} → {dst}", file=sys.stderr)
    return 0


def cmd_pull(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="mars_runtime pull")
    p.add_argument(
        "src",
        help="Source: relative workspace path (local) or fly://<app>/<path>",
    )
    p.add_argument("local", help="Local destination path")
    p.add_argument("--data-dir", dest="data_dir", help="Override $MARS_DATA_DIR")
    args = p.parse_args(argv)

    local = Path(args.local).expanduser().resolve()
    if local.exists() and local.is_dir():
        print(
            f"local destination is a directory; pass a file path: {local}",
            file=sys.stderr,
        )
        return 1
    local.parent.mkdir(parents=True, exist_ok=True)

    fly = _parse_fly_url(args.src)
    if fly:
        app, remote = fly
        try:
            _validate_fly_remote(remote)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        if any(c in str(local) for c in (" ", "\t", "\n", "\r")):
            print(
                f"local path contains whitespace; Fly SFTP shell can't handle it safely: {local}",
                file=sys.stderr,
            )
            return 1
        try:
            _run_fly_sftp("get", app, f"/data/workspace/{remote}", str(local))
        except subprocess.CalledProcessError as e:
            print(
                f"fly sftp failed (exit {e.returncode}): {e.stderr.strip() if e.stderr else ''}",
                file=sys.stderr,
            )
            return 1
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"pulled fly://{app}/{remote} → {local}", file=sys.stderr)
        return 0

    dir_ = _data_dir(args.data_dir)
    workspace = dir_ / "workspace"
    try:
        src = _confined_workspace_path(workspace, args.src)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not src.exists():
        print(f"source not found in workspace: {src}", file=sys.stderr)
        return 1
    if not src.is_file():
        print(f"source must be a regular file: {src}", file=sys.stderr)
        return 1
    shutil.copy2(src, local)
    print(f"pulled {src} → {local}", file=sys.stderr)
    return 0
