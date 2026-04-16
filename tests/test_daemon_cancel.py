from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mars_runtime.config import AgentConfig
from mars_runtime.daemon import turns
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


def _auth(owner: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer secret-token"}
    if owner is not None:
        headers["X-Owner-Subject"] = owner
    return headers


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "turns.db"


def test_cancel_running_turn(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]
    turn_id = "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c"

    conn = turns.connect(_db_path(tmp_path))
    try:
        turns.create_exclusive(conn, turn_id=turn_id, session_id=sid)
    finally:
        conn.close()

    resp = client.post(
        f"/v1/sessions/{sid}/turns/{turn_id}/cancel",
        headers=_auth("user_A"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["turn_id"] == turn_id
    # No worker registered → endpoint marks the turn failed directly so the
    # session isn't wedged on the partial-unique live-turn index.
    assert body["state"] == "failed"

    # DB state reflects the cancellation.
    conn = turns.connect(_db_path(tmp_path))
    try:
        row = turns.load_turn(conn, turn_id)
    finally:
        conn.close()
    assert row is not None
    assert row[1] == "failed"


def test_cancel_completed_turn_idempotent(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]
    turn_id = "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c"

    conn = turns.connect(_db_path(tmp_path))
    try:
        turns.create_exclusive(conn, turn_id=turn_id, session_id=sid)
        turns.update_state(conn, turn_id=turn_id, state="completed")
    finally:
        conn.close()

    resp = client.post(
        f"/v1/sessions/{sid}/turns/{turn_id}/cancel",
        headers=_auth("user_A"),
    )
    assert resp.status_code == 200
    assert resp.json() == {"turn_id": turn_id, "state": "completed"}


def test_cancel_unknown_turn(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]
    turn_id = "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c"

    resp = client.post(
        f"/v1/sessions/{sid}/turns/{turn_id}/cancel",
        headers=_auth("user_A"),
    )
    assert resp.status_code == 404


def test_cancel_requires_auth(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]
    turn_id = "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c"

    resp = client.post(
        f"/v1/sessions/{sid}/turns/{turn_id}/cancel",
    )
    assert resp.status_code == 401


def test_cancel_foreign_session(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]
    turn_id = "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c"

    resp = client.post(
        f"/v1/sessions/{sid}/turns/{turn_id}/cancel",
        headers=_auth("user_B"),
    )
    assert resp.status_code == 404


def test_cancel_does_not_force_db_when_worker_registered(tmp_path: Path) -> None:
    """When cancel_turn() finds a registered worker, the endpoint must not
    race-update the DB — run_turn's finalizer owns the terminal write."""
    from mars_runtime.daemon import runner
    import subprocess

    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]
    turn_id = "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c"

    conn = turns.connect(_db_path(tmp_path))
    try:
        turns.create_exclusive(conn, turn_id=turn_id, session_id=sid)
    finally:
        conn.close()

    # Register a no-op worker so cancel_turn() returns True.
    class _NoopWorker:
        returncode = 0

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    runner._register_worker(turn_id, _NoopWorker())  # type: ignore[arg-type]
    try:
        resp = client.post(
            f"/v1/sessions/{sid}/turns/{turn_id}/cancel",
            headers=_auth("user_A"),
        )
    finally:
        runner._unregister_worker(turn_id)
        # This test bypasses run_turn, which is what normally discards the
        # _CANCELLED_TURNS flag. Clean up explicitly so later tests using
        # the same turn_id don't see a stale cancel signal.
        runner.discard_cancelled(turn_id)

    # Endpoint returns 200; state reflects whatever run_turn (or DB) says.
    assert resp.status_code == 200
    # DB state not force-updated by the endpoint since a worker was found.
    conn = turns.connect(_db_path(tmp_path))
    try:
        row = turns.load_turn(conn, turn_id)
    finally:
        conn.close()
    # State is still 'accepted' because no real run_turn was driving the worker.
    # The important invariant: the endpoint did NOT clobber the DB to 'failed'
    # when a worker was signalled — that decision is deferred to run_turn.
    assert row is not None
    assert row[1] == "accepted"
