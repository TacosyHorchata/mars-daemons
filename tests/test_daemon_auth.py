from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mars_runtime.config import AgentConfig
from mars_runtime.daemon.__main__ import _load_bearer_token
from mars_runtime.daemon.app import create_app


def _config() -> AgentConfig:
    return AgentConfig(
        name="test-agent",
        description="test daemon",
        model="claude-opus-4-5",
        system_prompt_path="/tmp/CLAUDE.md",
        tools=["read"],
    )


def _write_token(path: Path, value: str = "secret-token", mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)
    return path


def _client(tmp_path: Path, bearer: str = "secret-token") -> TestClient:
    data_dir = tmp_path / "data"
    db_path = data_dir / "turns.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(config=_config(), data_dir=data_dir, db_path=db_path, bearer=bearer)
    return TestClient(app)


def test_token_path_inside_mars_data_dir_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    token_path = _write_token(data_dir / "token.txt")

    with pytest.raises(SystemExit) as exc:
        _load_bearer_token(data_dir, token_path)

    assert exc.value.code == 2


def test_token_symlink_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = _write_token(tmp_path / "real-token.txt")
    symlink = tmp_path / "token-link.txt"
    symlink.symlink_to(target)

    with pytest.raises(SystemExit) as exc:
        _load_bearer_token(data_dir, symlink)

    assert exc.value.code == 2


def test_token_mode_0644_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    token_path = _write_token(tmp_path / "token.txt", mode=0o644)

    with pytest.raises(SystemExit) as exc:
        _load_bearer_token(data_dir, token_path)

    assert exc.value.code == 2


def test_wrong_http_bearer_returns_401(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/v1/sessions",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


def test_missing_or_malformed_authorization_header_returns_401(tmp_path: Path) -> None:
    client = _client(tmp_path)

    missing = client.post("/v1/sessions")
    malformed = client.post(
        "/v1/sessions",
        headers={"Authorization": "Token secret-token"},
    )

    assert missing.status_code == 401
    assert malformed.status_code == 401
