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


def _auth(owner: str | None = None, role: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer secret-token"}
    if owner is not None:
        headers["X-Owner-Subject"] = owner
    if role is not None:
        headers["X-Owner-Role"] = role
    return headers


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "turns.db"


# --- 1.1b ownership ---


def test_owner_can_access_session(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.get(f"/v1/sessions/{sid}", headers=_auth("user_A"))

    assert resp.status_code == 200


def test_foreign_owner_gets_404(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.get(f"/v1/sessions/{sid}", headers=_auth("user_B"))

    assert resp.status_code == 404
    assert resp.json() == {"detail": "session_not_found"}


def test_nonexistent_session_same_404(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid_a = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    foreign_resp = client.get(f"/v1/sessions/{sid_a}", headers=_auth("user_B"))
    missing_resp = client.get("/v1/sessions/sess_000000000000000000000000", headers=_auth("user_A"))

    assert foreign_resp.status_code == missing_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()


def test_no_owner_header_allows_access(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth()).json()["session_id"]

    resp = client.get(f"/v1/sessions/{sid}", headers=_auth("anyone"))

    assert resp.status_code == 200


def test_foreign_owner_messages_gets_404(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        headers=_auth("user_B"),
        json={"turn_id": "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c", "text": "hola"},
    )

    assert resp.status_code == 404


# --- 1.1c metadata GET ---


def test_get_session_returns_metadata_not_transcript(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.get(f"/v1/sessions/{sid}", headers=_auth("user_A"))

    body = resp.json()
    assert "messages" not in body
    assert "owner_subject" not in body
    assert "agent_config" not in body
    assert body["session_id"] == sid
    assert "created_at" in body
    assert "updated_at" in body


def test_get_session_returns_status_and_turn_count(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.get(f"/v1/sessions/{sid}", headers=_auth("user_A"))
    body = resp.json()
    assert body["status"] == "idle"
    assert body["turn_count"] == 0

    conn = turns.connect(_db_path(tmp_path))
    try:
        turns.create_exclusive(conn, turn_id="8d3a7c14-2601-4ea6-90db-bfe93f10bb5c", session_id=sid)
    finally:
        conn.close()

    resp2 = client.get(f"/v1/sessions/{sid}", headers=_auth("user_A"))
    assert resp2.json()["status"] == "running"

    conn = turns.connect(_db_path(tmp_path))
    try:
        turns.update_state(conn, turn_id="8d3a7c14-2601-4ea6-90db-bfe93f10bb5c", state="completed")
    finally:
        conn.close()

    resp3 = client.get(f"/v1/sessions/{sid}", headers=_auth("user_A"))
    assert resp3.json()["status"] == "idle"
    assert resp3.json()["turn_count"] == 1


# --- 1.1a role header ---


def test_role_header_parsed(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    resp = client.post("/v1/sessions", headers=_auth("user_A", role="admin"))

    assert resp.status_code == 201


def test_invalid_role_rejected(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    resp = client.post("/v1/sessions", headers=_auth("user_A", role="superuser"))

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_role"


def test_path_traversal_owner_subject_rejected(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    resp = client.post("/v1/sessions", headers=_auth("../../shared"))

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_owner_subject"


def test_assistant_id_path_traversal_rejected(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    resp = client.post(
        "/v1/sessions",
        headers=_auth("user_A"),
        json={"assistant_id": "../../../../tmp/evil"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] in ("invalid_assistant_id", "unknown_assistant")
