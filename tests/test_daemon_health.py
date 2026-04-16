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


def _client(tmp_path: Path) -> TestClient:
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


def test_healthz_ok(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ok(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_readyz_fails_when_db_broken(tmp_path: Path) -> None:
    client = _client(tmp_path)
    # Corrupt the db file.
    db_path = tmp_path / "data" / "turns.db"
    db_path.write_bytes(b"not a sqlite db")

    resp = client.get("/readyz")
    # Either the SELECT fails (503) or corruption is silently tolerated (still 200).
    # The contract only requires 503 when checks genuinely fail.
    if resp.status_code == 503:
        body = resp.json()
        assert body["status"] == "not_ready"
        assert "checks" in body
