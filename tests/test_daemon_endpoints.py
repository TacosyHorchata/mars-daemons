from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from mars_runtime.config import AgentConfig
from mars_runtime.daemon import turns
from mars_runtime.daemon.app import create_app

_SESSION_ID_RE = re.compile(r"^sess_[0-9a-f]{24}$")


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


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "turns.db"


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret-token"}


def test_post_sessions_with_bearer_returns_201_and_valid_session_id(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/v1/sessions", headers=_auth())

    assert response.status_code == 201
    assert _SESSION_ID_RE.fullmatch(response.json()["session_id"])


def test_get_session_returns_metadata(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/v1/sessions", headers=_auth())
    session_id = created.json()["session_id"]

    response = client.get(f"/v1/sessions/{session_id}", headers=_auth())

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert body["status"] == "idle"
    assert body["turn_count"] == 0
    assert "messages" not in body


def test_get_session_bad_id_returns_404(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/v1/sessions/bad", headers=_auth())

    assert response.status_code == 404


def test_post_messages_without_bearer_returns_401(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/v1/sessions", headers=_auth())
    session_id = created.json()["session_id"]

    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"turn_id": "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c", "text": "hola"},
    )

    assert response.status_code == 401


def test_post_messages_with_invalid_turn_id_returns_400(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/v1/sessions", headers=_auth())
    session_id = created.json()["session_id"]

    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        headers=_auth(),
        json={"turn_id": "not-a-uuid", "text": "hola"},
    )

    assert response.status_code == 400


def test_post_messages_duplicate_turn_id_returns_409_with_state(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/v1/sessions", headers=_auth())
    session_id = created.json()["session_id"]
    turn_id = "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c"

    conn = turns.connect(_db_path(tmp_path))
    try:
        turns.create_exclusive(conn, turn_id=turn_id, session_id=session_id)
    finally:
        conn.close()

    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        headers=_auth(),
        json={"turn_id": turn_id, "text": "hola"},
    )

    assert response.status_code == 409
    assert response.json() == {"turn_id": turn_id, "state": "accepted"}


def test_post_messages_with_active_turn_returns_409_session_busy(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = client.post("/v1/sessions", headers=_auth())
    session_id = created.json()["session_id"]

    conn = turns.connect(_db_path(tmp_path))
    try:
        turns.create_exclusive(
            conn,
            turn_id="8d3a7c14-2601-4ea6-90db-bfe93f10bb5c",
            session_id=session_id,
        )
    finally:
        conn.close()

    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        headers=_auth(),
        json={
            "turn_id": "4e2db7de-7ec0-489a-bfa6-767bf293b6f1",
            "text": "hola",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"error": "session_busy"}
