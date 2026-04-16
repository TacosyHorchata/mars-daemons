from __future__ import annotations

import asyncio
import json
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


# --- Upload ---


def test_upload_success(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.post(
        f"/v1/sessions/{sid}/files",
        headers=_auth("user_A"),
        files={"file": ("hello.txt", b"hi world", "text/plain")},
    )

    assert resp.status_code == 201
    assert resp.json() == {"path": "uploads/hello.txt"}

    on_disk = tmp_path / "data" / "user-workspaces" / "user_A" / "uploads" / "hello.txt"
    assert on_disk.read_bytes() == b"hi world"


def test_upload_path_traversal_rejected(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.post(
        f"/v1/sessions/{sid}/files",
        headers=_auth("user_A"),
        files={"file": ("../../etc/passwd", b"evil", "text/plain")},
    )

    # Sanitize strips `..` → basename `passwd` — accepted under uploads/ (safe).
    # Result: written as uploads/passwd, not escape. Verify it stays inside.
    # If logic changes to reject entirely, adapt.
    if resp.status_code == 201:
        assert resp.json() == {"path": "uploads/passwd"}
        on_disk = tmp_path / "data" / "user-workspaces" / "user_A" / "uploads" / "passwd"
        assert on_disk.is_file()
    else:
        assert resp.status_code == 400


def test_upload_too_large(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MARS_MAX_UPLOAD_BYTES", "100")
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.post(
        f"/v1/sessions/{sid}/files",
        headers=_auth("user_A"),
        files={"file": ("big.bin", b"x" * 500, "application/octet-stream")},
    )

    assert resp.status_code == 413


def test_upload_requires_auth(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.post(
        f"/v1/sessions/{sid}/files",
        files={"file": ("a.txt", b"hi", "text/plain")},
    )

    assert resp.status_code == 401


def test_upload_foreign_session(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.post(
        f"/v1/sessions/{sid}/files",
        headers=_auth("user_B"),
        files={"file": ("a.txt", b"hi", "text/plain")},
    )

    assert resp.status_code == 404


# --- Download ---


def test_download_success(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]
    client.post(
        f"/v1/sessions/{sid}/files",
        headers=_auth("user_A"),
        files={"file": ("round.txt", b"roundtrip", "text/plain")},
    )

    resp = client.get(
        f"/v1/sessions/{sid}/files/uploads/round.txt",
        headers=_auth("user_A"),
    )

    assert resp.status_code == 200
    assert resp.content == b"roundtrip"


def test_download_traversal_rejected(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.get(
        f"/v1/sessions/{sid}/files/..%2F..%2Fetc%2Fpasswd",
        headers=_auth("user_A"),
    )

    assert resp.status_code in (400, 404)


def test_download_nonexistent(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.get(
        f"/v1/sessions/{sid}/files/uploads/nope.txt",
        headers=_auth("user_A"),
    )

    assert resp.status_code == 404


def test_download_requires_auth(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]

    resp = client.get(f"/v1/sessions/{sid}/files/uploads/whatever.txt")

    assert resp.status_code == 401


def test_download_scoped_to_user_workspace(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    sid_a = client.post("/v1/sessions", headers=_auth("user_A")).json()["session_id"]
    sid_b = client.post("/v1/sessions", headers=_auth("user_B")).json()["session_id"]
    client.post(
        f"/v1/sessions/{sid_a}/files",
        headers=_auth("user_A"),
        files={"file": ("a.txt", b"A", "text/plain")},
    )

    # B asking for A's session → 404 (ownership check).
    resp = client.get(
        f"/v1/sessions/{sid_a}/files/uploads/a.txt",
        headers=_auth("user_B"),
    )
    assert resp.status_code == 404

    # B asking on their own session for a file that doesn't exist there → 404.
    resp2 = client.get(
        f"/v1/sessions/{sid_b}/files/uploads/a.txt",
        headers=_auth("user_B"),
    )
    assert resp2.status_code == 404


# --- files_changed diff ---


def _drain(agen) -> list[dict]:
    async def _collect():
        out = []
        async for item in agen:
            out.append(item)
        return out

    return asyncio.run(_collect())


def test_files_changed_in_turn_completed(tmp_path: Path, monkeypatch) -> None:
    """Stub spawn_worker and provider to inject a synthetic turn_completed
    event while a file is created in the workspace. Verify files_changed
    is attached to the final event.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "turns.db"
    conn = turns.connect(db_path)
    try:
        turns.init_db(conn)
    finally:
        conn.close()

    owner = "user_A"
    ws = data_dir / "user-workspaces" / owner
    (ws / "uploads").mkdir(parents=True, exist_ok=True)

    # Pre-create session file so sessions.load works.
    from mars_runtime.storage import sessions as sstore
    sid = sstore.new_id()
    sstore.save(
        data_dir / "sessions",
        sid,
        "test-agent",
        _config().model_dump(),
        messages=[],
        created_at=int(time.time()),
        owner_subject=owner,
        workspace_path=f"user-workspaces/{owner}",
    )

    turn_id = "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c"
    conn = turns.connect(db_path)
    try:
        turns.create_exclusive(conn, turn_id=turn_id, session_id=sid)
    finally:
        conn.close()

    class _FakeWorker:
        def __init__(self, events: list[dict]) -> None:
            self._events = events
            self.stdin = type("S", (), {"closed": False, "close": lambda self: None})()
            self.stdout = iter(
                json.dumps({"rpc": "event", "event": e}) + "\n" for e in events
            )
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def _fake_spawn(config, session_id_, data_dir_, start_messages, **kwargs):
        # Simulate the worker creating a file during the turn.
        (ws / "uploads" / "generated.txt").write_text("created during turn")
        return _FakeWorker([{"type": "turn_completed"}])

    monkeypatch.setattr(runner, "spawn_worker", _fake_spawn)
    monkeypatch.setattr(runner, "send_to_worker", lambda *a, **kw: None)

    # Stub LLM client loader so it's not called.
    from mars_runtime import providers as llm_client

    monkeypatch.setattr(llm_client, "load_all", lambda: None)
    monkeypatch.setattr(llm_client, "infer_provider", lambda model: "anthropic")
    monkeypatch.setattr(llm_client, "get", lambda name: object())

    agen = runner.stream_turn(
        config=_config(),
        data_dir=data_dir,
        db_path=db_path,
        session_id=sid,
        turn_id=turn_id,
        text="hola",
        owner_subject=owner,
        workspace_path=f"user-workspaces/{owner}",
    )
    events = _drain(agen)
    payloads = [json.loads(e["data"]) for e in events]
    completed = [p for p in payloads if p.get("type") == "turn_completed"]
    assert completed
    assert "files_changed" in completed[-1]
    assert "uploads/generated.txt" in completed[-1]["files_changed"]


def test_files_changed_empty_when_no_changes(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "turns.db"
    conn = turns.connect(db_path)
    try:
        turns.init_db(conn)
    finally:
        conn.close()

    owner = "user_B"
    ws = data_dir / "user-workspaces" / owner
    (ws / "uploads").mkdir(parents=True, exist_ok=True)

    from mars_runtime.storage import sessions as sstore

    sid = sstore.new_id()
    sstore.save(
        data_dir / "sessions",
        sid,
        "test-agent",
        _config().model_dump(),
        messages=[],
        created_at=int(time.time()),
        owner_subject=owner,
        workspace_path=f"user-workspaces/{owner}",
    )

    turn_id = "4e2db7de-7ec0-489a-bfa6-767bf293b6f1"
    conn = turns.connect(db_path)
    try:
        turns.create_exclusive(conn, turn_id=turn_id, session_id=sid)
    finally:
        conn.close()

    class _FakeWorker:
        def __init__(self, events: list[dict]) -> None:
            self.stdin = type("S", (), {"closed": False, "close": lambda self: None})()
            self.stdout = iter(
                json.dumps({"rpc": "event", "event": e}) + "\n" for e in events
            )

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def _fake_spawn(config, session_id_, data_dir_, start_messages, **kwargs):
        return _FakeWorker([{"type": "turn_completed"}])

    monkeypatch.setattr(runner, "spawn_worker", _fake_spawn)
    monkeypatch.setattr(runner, "send_to_worker", lambda *a, **kw: None)

    from mars_runtime import providers as llm_client

    monkeypatch.setattr(llm_client, "load_all", lambda: None)
    monkeypatch.setattr(llm_client, "infer_provider", lambda model: "anthropic")
    monkeypatch.setattr(llm_client, "get", lambda name: object())

    agen = runner.stream_turn(
        config=_config(),
        data_dir=data_dir,
        db_path=db_path,
        session_id=sid,
        turn_id=turn_id,
        text="ping",
        owner_subject=owner,
        workspace_path=f"user-workspaces/{owner}",
    )
    events = _drain(agen)
    payloads = [json.loads(e["data"]) for e in events]
    completed = [p for p in payloads if p.get("type") == "turn_completed"]
    assert completed
    assert completed[-1]["files_changed"] == []
