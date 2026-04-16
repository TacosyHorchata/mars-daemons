from __future__ import annotations

import grp
import hashlib
import os
import pwd
import subprocess
import sys
from pathlib import Path

MARS_GROUP = "mars"
MARS_ADMIN_GROUP = "mars-admin"
MARS_GID = 900
MARS_ADMIN_GID = 901


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _user_exists(uid: int) -> bool:
    try:
        pwd.getpwuid(uid)
        return True
    except KeyError:
        return False


def _user_is_ours(uid: int) -> bool:
    try:
        entry = pwd.getpwuid(uid)
    except KeyError:
        return False
    return entry.pw_name == f"mars_u{uid}"


def ensure_groups() -> None:
    if os.geteuid() != 0:
        return
    if not _group_exists(MARS_GROUP):
        _run(["groupadd", "-g", str(MARS_GID), MARS_GROUP])
    if not _group_exists(MARS_ADMIN_GROUP):
        _run(["groupadd", "-g", str(MARS_ADMIN_GID), MARS_ADMIN_GROUP])


def setup_shared_permissions(shared_dir: Path) -> None:
    if not shared_dir.exists():
        return
    if os.geteuid() != 0:
        return
    admin_gid = grp.getgrnam(MARS_ADMIN_GROUP).gr_gid
    os.chown(shared_dir, 0, admin_gid)
    os.chmod(shared_dir, 0o770)
    try:
        _run(["setfacl", "-R", "-m", f"g:{MARS_GROUP}:rx", str(shared_dir)])
        _run(["setfacl", "-R", "-d", "-m", f"g:{MARS_GROUP}:rx", str(shared_dir)])
    except FileNotFoundError:
        print("[daemon] setfacl not available; mars group may lack read on shared/", file=sys.stderr, flush=True)
    except subprocess.CalledProcessError as exc:
        print(f"[daemon] setfacl failed: {exc.stderr!r}", file=sys.stderr, flush=True)


def resolve_uid(owner_subject: str) -> int:
    digest = hashlib.sha256(owner_subject.encode("utf-8")).digest()
    n = int.from_bytes(digest[:8], "big")
    return 1000 + (n % 60000)


def resolve_gid(role: str) -> int:
    name = MARS_ADMIN_GROUP if role == "admin" else MARS_GROUP
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return MARS_ADMIN_GID if role == "admin" else MARS_GID


class UidCollision(RuntimeError):
    pass


def ensure_user(uid: int, gid: int) -> None:
    if os.geteuid() != 0:
        return
    if _user_is_ours(uid):
        return
    if _user_exists(uid):
        # UID collides with a pre-existing local account that is NOT our
        # mars_u{uid} naming convention. Using it would leave initgroups()
        # failing at spawn time. Fail loud so the operator notices.
        raise UidCollision(
            f"uid {uid} exists but is not mars_u{uid}; owner_subject cannot be provisioned"
        )
    _run(
        [
            "useradd",
            "-u",
            str(uid),
            "-g",
            str(gid),
            "-M",
            "-s",
            "/bin/false",
            f"mars_u{uid}",
        ]
    )


def ensure_workspace(
    data_dir: Path,
    owner_subject: str,
    uid: int,
    gid: int,
) -> Path:
    if owner_subject in {".", ".."} or "/" in owner_subject or "\x00" in owner_subject:
        raise ValueError(f"unsafe owner_subject: {owner_subject!r}")
    parent = (data_dir / "user-workspaces").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    ws = (parent / owner_subject).resolve()
    try:
        ws.relative_to(parent)
    except ValueError as exc:
        raise ValueError(f"owner_subject escapes user-workspaces/: {owner_subject!r}") from exc
    ws.mkdir(parents=True, exist_ok=True)
    subdirs = ["uploads", "output", "agents", "skills", "rules", "memory"]
    for name in subdirs:
        (ws / name).mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(ws, uid, gid)
        os.chmod(ws, 0o700)
        for name in subdirs:
            os.chown(ws / name, uid, gid)
    return ws
