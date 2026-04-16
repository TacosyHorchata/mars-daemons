from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from mars_runtime.config import AgentConfig
from mars_runtime.daemon import runner, turns


def _config() -> AgentConfig:
    return AgentConfig(
        name="test-agent",
        description="test daemon",
        model="claude-opus-4-5",
        system_prompt_path="/tmp/CLAUDE.md",
        tools=["read"],
    )


def _drain(agen) -> list[dict]:
    async def _collect():
        out = []
        async for item in agen:
            out.append(item)
        return out

    return asyncio.run(_collect())


def _make_ready_session(tmp_path: Path, owner: str, turn_id: str) -> tuple[Path, Path, str]:
    from mars_runtime.storage import sessions as sstore

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "turns.db"
    conn = turns.connect(db_path)
    try:
        turns.init_db(conn)
    finally:
        conn.close()

    ws = data_dir / "user-workspaces" / owner
    ws.mkdir(parents=True, exist_ok=True)

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

    conn = turns.connect(db_path)
    try:
        turns.create_exclusive(conn, turn_id=turn_id, session_id=sid)
    finally:
        conn.close()
    return data_dir, db_path, sid


class _HangingWorker:
    """Worker that never emits any events — simulates a hung model call."""

    def __init__(self) -> None:
        self._block = threading.Event()

        def _lines():
            # Block until .kill() is called; then yield EOF.
            self._block.wait(timeout=30)

        self.stdin = type("S", (), {"closed": False, "close": lambda self: None})()
        self.stdout = _HangingIter(self._block)
        self.returncode = None

    def kill(self) -> None:
        self._block.set()
        self.returncode = -9

    def terminate(self) -> None:
        self.kill()

    def wait(self, timeout=None):
        self._block.wait(timeout or 30)
        return self.returncode if self.returncode is not None else 0


class _HangingIter:
    def __init__(self, block: threading.Event) -> None:
        self._block = block

    def __iter__(self):
        return self

    def __next__(self):
        self._block.wait(timeout=30)
        raise StopIteration


def test_turn_timeout_kills_worker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MARS_TURN_TIMEOUT_S", "2")
    owner = "user_T"
    turn_id = "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c"
    data_dir, db_path, sid = _make_ready_session(tmp_path, owner, turn_id)

    worker = _HangingWorker()

    def _fake_spawn(config, session_id_, data_dir_, start_messages, **kwargs):
        return worker

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
        text="hi",
        owner_subject=owner,
        workspace_path=f"user-workspaces/{owner}",
    )
    events = _drain(agen)
    payloads = [json.loads(e["data"]) for e in events]
    aborted = [p for p in payloads if p.get("type") == "turn_aborted"]
    assert aborted
    assert aborted[-1].get("reason") == "timeout"

    # Verify state in db.
    conn = turns.connect(db_path)
    try:
        row = turns.load_turn(conn, turn_id)
    finally:
        conn.close()
    assert row is not None
    assert row[1] == "failed"


def test_turn_completes_before_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MARS_TURN_TIMEOUT_S", "60")
    owner = "user_F"
    turn_id = "4e2db7de-7ec0-489a-bfa6-767bf293b6f1"
    data_dir, db_path, sid = _make_ready_session(tmp_path, owner, turn_id)

    class _FastWorker:
        def __init__(self, events: list[dict]) -> None:
            self.stdin = type("S", (), {"closed": False, "close": lambda self: None})()
            self.stdout = iter(
                json.dumps({"rpc": "event", "event": e}) + "\n" for e in events
            )
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

        def terminate(self):
            pass

    def _fake_spawn(config, session_id_, data_dir_, start_messages, **kwargs):
        return _FastWorker([{"type": "turn_completed"}])

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
        text="hi",
        owner_subject=owner,
        workspace_path=f"user-workspaces/{owner}",
    )
    events = _drain(agen)
    payloads = [json.loads(e["data"]) for e in events]
    assert any(p.get("type") == "turn_completed" for p in payloads)

    conn = turns.connect(db_path)
    try:
        row = turns.load_turn(conn, turn_id)
    finally:
        conn.close()
    assert row is not None
    assert row[1] == "completed"
