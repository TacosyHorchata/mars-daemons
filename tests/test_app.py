from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from mars_runtime.host import app as host_app
from mars_runtime.host import router as host_router


def test_create_and_read_conversation(monkeypatch, tmp_path):
    async def fake_run_turn(**_: object) -> None:
        return None

    monkeypatch.setenv("MARS_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(host_router, "_safe_run_turn", fake_run_turn)

    app = host_app.create_app(data_dir=tmp_path)
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token"}

    create_response = client.post(
        "/api/v1/agents/conversations",
        json={"message": "hola mundo"},
        headers=headers,
    )
    assert create_response.status_code == 200
    conversation_id = create_response.json()["conversation_id"]

    list_response = client.get("/api/v1/agents/conversations", headers=headers)
    assert list_response.status_code == 200
    assert list_response.json()["data"][0]["id"] == conversation_id

    detail_response = client.get(f"/api/v1/agents/conversations/{conversation_id}", headers=headers)
    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == conversation_id

    health_response = client.get("/healthz")
    assert health_response.status_code == 200

    ready_response = client.get("/readyz")
    assert ready_response.status_code == 200

    workspace_store = app.state.workspace_store
    asyncio.run(
        workspace_store.upload_content(
            "default",
            "anonymous",
            conversation_id,
            "artifact.txt",
            b"workspace file",
            "text/plain",
        ),
    )
    workspace_response = client.get(
        f"/api/v1/agents/conversations/{conversation_id}/workspace/artifact.txt",
        headers=headers,
    )
    assert workspace_response.status_code == 200
    assert workspace_response.text == "workspace file"
