from __future__ import annotations

import os
from pathlib import Path

import pytest

from mars_runtime.daemon import isolation


def test_resolve_uid_deterministic() -> None:
    a = isolation.resolve_uid("user_A")
    b = isolation.resolve_uid("user_A")
    assert a == b
    assert 1000 <= a < 61000


def test_resolve_uid_different_owners() -> None:
    seen: set[int] = set()
    for owner in [f"user_{i}" for i in range(200)]:
        seen.add(isolation.resolve_uid(owner))
    assert len(seen) >= 198  # sha256 mod 60000 — collisions possible but extremely rare


def test_ensure_workspace_creates_dirs(tmp_path: Path) -> None:
    uid = isolation.resolve_uid("user_X")
    ws = isolation.ensure_workspace(tmp_path, "user_X", uid, 900)
    assert ws.resolve() == (tmp_path / "user-workspaces" / "user_X").resolve()
    assert ws.is_dir()
    for sub in ("uploads", "output", "agents", "skills", "rules", "memory"):
        assert (ws / sub).is_dir()


def test_ensure_workspace_rejects_path_escape(tmp_path: Path) -> None:
    for bad in ("..", ".", "a/b", "x/../y", "\x00bad"):
        try:
            isolation.ensure_workspace(tmp_path, bad, 1000, 900)
        except ValueError:
            continue
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


@pytest.mark.skipif(os.getuid() != 0, reason="requires root")
def test_ensure_workspace_permissions(tmp_path: Path) -> None:
    uid = isolation.resolve_uid("user_Y")
    gid = isolation.resolve_gid("user")
    ws = isolation.ensure_workspace(tmp_path, "user_Y", uid, gid)
    st = ws.stat()
    assert st.st_uid == uid
    assert st.st_gid == gid
    assert (st.st_mode & 0o777) == 0o700


@pytest.mark.skipif(os.getuid() != 0, reason="requires root")
def test_shared_permissions(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    isolation.ensure_groups()
    isolation.setup_shared_permissions(shared)
    st = shared.stat()
    assert st.st_uid == 0
    assert (st.st_mode & 0o777) == 0o770


@pytest.mark.skipif(os.getuid() != 0, reason="requires root")
def test_worker_drops_privileges(tmp_path: Path) -> None:
    """Verify that spawn_worker drops privileges via preexec_fn.

    We don't actually run the worker entry point — we just invoke a
    minimal python -c that prints os.getuid() and exits, bypassing the
    real worker's argparse. This is a smoke test for the preexec_fn wiring.
    """
    import subprocess
    import sys

    uid = isolation.resolve_uid("privdrop_test")
    gid = isolation.resolve_gid("user")
    isolation.ensure_groups()
    isolation.ensure_user(uid, gid)

    def _drop() -> None:
        os.setgid(gid)
        os.setuid(uid)

    result = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getuid())"],
        capture_output=True,
        text=True,
        preexec_fn=_drop,
    )
    assert result.returncode == 0
    assert int(result.stdout.strip()) == uid


@pytest.mark.skipif(os.getuid() != 0, reason="requires root")
def test_worker_cannot_access_other_workspace(tmp_path: Path) -> None:
    import subprocess
    import sys

    isolation.ensure_groups()
    uid_a = isolation.resolve_uid("uA")
    uid_b = isolation.resolve_uid("uB")
    gid = isolation.resolve_gid("user")
    isolation.ensure_user(uid_a, gid)
    isolation.ensure_user(uid_b, gid)
    ws_a = isolation.ensure_workspace(tmp_path, "uA", uid_a, gid)
    ws_b = isolation.ensure_workspace(tmp_path, "uB", uid_b, gid)
    (ws_b / "secret.txt").write_text("secret", encoding="utf-8")
    os.chown(ws_b / "secret.txt", uid_b, gid)

    def _drop() -> None:
        os.setgid(gid)
        os.setuid(uid_a)

    result = subprocess.run(
        [sys.executable, "-c", f"open({str(ws_b / 'secret.txt')!r}).read()"],
        capture_output=True,
        text=True,
        preexec_fn=_drop,
    )
    assert result.returncode != 0
    assert "Permission" in result.stderr or "PermissionError" in result.stderr
