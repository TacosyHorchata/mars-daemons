from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path

import httpx
import pytest

from mars_runtime.config import AgentConfig
from mars_runtime.daemon import runner, turns
from mars_runtime.daemon.app import create_app
from mars_runtime.storage import sessions


class _FakeLLM:
    pass


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeWorker:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = iter(
            [
                json.dumps({"rpc": "event", "event": {"type": "assistant_chunk", "delta": "ho"}}) + "\n",
                json.dumps({"rpc": "event", "event": {"type": "assistant_chunk", "delta": "la"}}) + "\n",
                json.dumps({"rpc": "event", "event": {"type": "assistant_chunk", "delta": "!"}}) + "\n",
                json.dumps({"rpc": "event", "event": {"type": "turn_completed"}}) + "\n",
            ]
        )

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        return None


def _config() -> AgentConfig:
    return AgentConfig(
        name="test-agent",
        description="test daemon",
        model="claude-opus-4-5",
        system_prompt_path="/tmp/CLAUDE.md",
        tools=["read"],
    )


def _build_app(tmp_path: Path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "turns.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = turns.connect(db_path)
    try:
        turns.init_db(conn)
    finally:
        conn.close()
    config = _config()
    session_id = "sess_" + "a" * 24
    sessions.save(
        data_dir / "sessions",
        session_id,
        config.name,
        config.model_dump(),
        messages=[],
        created_at=1,
    )
    app = create_app(config=config, data_dir=data_dir, db_path=db_path, bearer="secret-token")
    return app, db_path, session_id


@pytest.mark.anyio
async def test_sse_stream_emits_assistant_chunks_and_turn_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, db_path, session_id = _build_app(tmp_path)
    monkeypatch.setattr(runner.llm_client, "load_all", lambda: None)
    monkeypatch.setattr(runner.llm_client, "infer_provider", lambda model: "fake")
    monkeypatch.setattr(runner.llm_client, "get", lambda name: _FakeLLM())
    monkeypatch.setattr(runner, "spawn_worker", lambda *args, **kwargs: _FakeWorker())

    transport = httpx.ASGITransport(app=app)
    events: list[dict[str, object]] = []

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with client.stream(
            "POST",
            f"/v1/sessions/{session_id}/messages",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "turn_id": "8d3a7c14-2601-4ea6-90db-bfe93f10bb5c",
                "text": "hola",
            },
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    assert [event["type"] for event in events] == [
        "assistant_chunk",
        "assistant_chunk",
        "assistant_chunk",
        "turn_completed",
    ]
    assert [event["delta"] for event in events if event["type"] == "assistant_chunk"] == [
        "ho",
        "la",
        "!",
    ]

    deadline = time.time() + 1.0
    row = None
    while time.time() < deadline:
        conn = turns.connect(db_path)
        try:
            row = conn.execute(
                "SELECT state, error FROM turns WHERE turn_id = ?",
                ("8d3a7c14-2601-4ea6-90db-bfe93f10bb5c",),
            ).fetchone()
        finally:
            conn.close()
        if row == ("completed", None):
            break
        await asyncio.sleep(0.01)

    assert row == ("completed", None)
