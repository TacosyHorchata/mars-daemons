from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from mars_runtime.config import AgentConfig
from mars_runtime.daemon import runner, turns
from mars_runtime.daemon.app import create_app


def _config() -> AgentConfig:
    return AgentConfig(
        name="test-agent",
        description="test daemon",
        model="claude-opus-4-5",
        system_prompt_path="/tmp/CLAUDE.md",
        tools=["read"],
    )


def _make_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    db_path = data_dir / "turns.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = turns.connect(db_path)
    try:
        turns.init_db(conn)
    finally:
        conn.close()
    app = create_app(
        config=_config(),
        data_dir=data_dir,
        db_path=db_path,
        bearer="secret-token",
    )
    return TestClient(app)


def _auth(owner: str | None = None, role: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer secret-token"}
    if owner is not None:
        headers["X-Owner-Subject"] = owner
    if role is not None:
        headers["X-Owner-Role"] = role
    return headers


def test_two_users_parallel_locks() -> None:
    """Different users get distinct lock instances."""
    lock_a = runner._get_user_lock("alpha")
    lock_b = runner._get_user_lock("beta")
    assert lock_a is not lock_b
    # Neither holds the other.
    lock_a.acquire()
    try:
        assert lock_b.acquire(blocking=False)
        lock_b.release()
    finally:
        lock_a.release()


def test_same_user_serialized() -> None:
    """Same owner_subject returns the same lock instance."""
    lock1 = runner._get_user_lock("gamma")
    lock2 = runner._get_user_lock("gamma")
    assert lock1 is lock2

    acquired_by_second = threading.Event()
    released_by_first = threading.Event()

    def second() -> None:
        with lock2:
            acquired_by_second.set()

    with lock1:
        t = threading.Thread(target=second, daemon=True)
        t.start()
        time.sleep(0.05)
        assert not acquired_by_second.is_set()
        released_by_first.set()
    t.join(timeout=2)
    assert acquired_by_second.is_set()


def test_session_includes_assistant_id(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.get(f"/v1/sessions/{sid}", headers=_auth("user_A"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["assistant_id"] == "test-agent"


def test_session_persists_config_snapshot(tmp_path: Path) -> None:
    """Session stores agent_config dict at creation time — this is the snapshot."""
    from mars_runtime.storage import sessions

    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    data = sessions.load(tmp_path / "data" / "sessions", sid)
    assert data["agent_config"]["name"] == "test-agent"
    assert data["agent_config"]["model"] == "claude-opus-4-5"
    assert data["owner_subject"] == "user_A"
    assert data["assistant_id"] == "test-agent"
