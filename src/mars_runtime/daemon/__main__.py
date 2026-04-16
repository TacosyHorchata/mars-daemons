from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from .._paths import data_dir as _data_dir
from ..broker.env import ingest_secrets_fd
from ..broker.hardening import harden_broker
from ..cli.run import _resolve_system_prompt
from ..config import AgentConfig
from . import isolation, replay, turns


def _parse_listen(raw: str) -> tuple[str, int]:
    if not raw.startswith("tcp://"):
        raise ValueError("listen address must use tcp://host:port")
    rest = raw[len("tcp://") :]
    host, sep, port_raw = rest.rpartition(":")
    if not sep or not host or not port_raw:
        raise ValueError("listen address must be tcp://host:port")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError("listen port must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("listen port must be in 1..65535")
    return host, port


def _load_agent_config(yaml_path: str | Path) -> AgentConfig:
    path = Path(yaml_path).resolve()
    config = AgentConfig.from_yaml_file(path)
    return _resolve_system_prompt(config, path)


def _token_error(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _load_bearer_token(data_dir: Path, token_path: Path) -> str:
    try:
        fd = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        _token_error(f"invalid bearer token file: {exc}")
    try:
        try:
            token_real = token_path.resolve()
            data_real = data_dir.resolve()
            if token_real.is_relative_to(data_real):
                _token_error("invalid bearer token file: must live outside MARS_DATA_DIR")
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                _token_error("invalid bearer token file: not a regular file")
            if info.st_uid != os.getuid():
                _token_error("invalid bearer token file: wrong owner")
            if (info.st_mode & 0o777) not in (0o400, 0o600):
                _token_error("invalid bearer token file: mode must be 0400 or 0600")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode().strip()
        except (OSError, UnicodeDecodeError) as exc:
            _token_error(f"invalid bearer token file: {exc}")
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mars_runtime.daemon")
    parser.add_argument("yaml_path", nargs="?", default="./agent.yaml")
    args = parser.parse_args(argv)

    listen_raw = os.environ.get("MARS_LISTEN", "tcp://127.0.0.1:8080")
    token_raw = os.environ.get("MARS_AUTH_TOKEN_FILE")
    if not token_raw:
        _token_error("MARS_AUTH_TOKEN_FILE is required")

    data_dir = _data_dir()

    ingest_secrets_fd()
    harden_broker()
    config = _load_agent_config(args.yaml_path)
    host, port = _parse_listen(listen_raw)
    bearer = _load_bearer_token(data_dir, Path(token_raw))

    sessions_dir = data_dir / "sessions"
    db_path = data_dir / "turns.db"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    conn = turns.connect(db_path)
    try:
        turns.init_db(conn)
        stale = turns.recover_stale_with_ids(conn)
    finally:
        conn.close()
    events_dir = data_dir / "session-events"
    for turn_id, sess_id in stale:
        try:
            replay.append_event(
                events_dir,
                sess_id,
                {
                    "type": "turn_aborted",
                    "reason": "daemon_restart",
                    "turn_id": turn_id,
                },
            )
        except Exception as exc:
            print(
                f"[daemon] replay append on recovery failed ({sess_id}): {exc}",
                file=sys.stderr,
                flush=True,
            )

    isolation.ensure_groups()
    isolation.setup_shared_permissions(data_dir / "shared")

    user_workspaces = data_dir / "user-workspaces"
    workspace_count = 0
    if user_workspaces.is_dir():
        workspace_count = sum(1 for p in user_workspaces.iterdir() if p.is_dir())
    import json as _json

    print(
        _json.dumps(
            {
                "event": "daemon_boot",
                "listen": listen_raw,
                "auth": "bearer_file",
                "data_dir": str(data_dir),
                "user_workspaces": workspace_count,
            }
        ),
        file=sys.stderr,
        flush=True,
    )

    from .app import create_app
    import uvicorn

    app = create_app(config=config, data_dir=data_dir, db_path=db_path, bearer=bearer)
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
