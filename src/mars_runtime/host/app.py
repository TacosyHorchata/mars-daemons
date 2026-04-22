"""FastAPI app wiring for the standalone mars-daemons backend."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import litellm
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .. import AgentConfig, SSEEventSink, setup_agents, shutdown_agents
from ..tools.bash import BashTool, set_workspace_store as set_bash_workspace_store
from ..tools.edit_memory import EditMemoryTool
from ..tools.read_memory import ReadMemoryTool
from ..tools.storage import StorageTool, set_storage_file_store
from ..tools.use_skill import UseSkillTool
from ..tools.workspace import WorkspaceTool, set_workspace_store as set_workspace_tool_store
from .auth import StaticBearerAuthProvider
from .router import configure_router, router
from .stores import (
    FileMemoryStore,
    FileRulesStore,
    FileSkillsStore,
    LocalConversationStore,
    LocalFileStore,
    LocalWorkspaceStore,
)

litellm.redact_messages_in_exceptions = True


def _resolve_data_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    return Path(os.getenv("MARS_DATA_DIR", ".mars-data")).resolve()


def _read_prompt_from_env(var_name: str) -> str:
    file_var = f"{var_name}_FILE"
    path = os.getenv(file_var)
    if path:
        return Path(path).read_text(encoding="utf-8")
    return os.getenv(var_name, "")


@asynccontextmanager
async def _lifespan(_: FastAPI):
    try:
        yield
    finally:
        await shutdown_agents()


def create_app(*, data_dir: Path | None = None) -> FastAPI:
    root = _resolve_data_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)

    conversation_store = LocalConversationStore(root)
    memory_store = FileMemoryStore(root)
    rules_store = FileRulesStore(root)
    skills_store = FileSkillsStore(root)
    file_store = LocalFileStore(root)
    workspace_store = LocalWorkspaceStore(root)
    auth_provider = StaticBearerAuthProvider()

    config = AgentConfig(
        model=os.getenv("MARS_MODEL", "azure_ai/Kimi-K2.5"),
        api_key=os.getenv("MARS_API_KEY") or None,
        api_base=os.getenv("MARS_API_BASE") or None,
        temperature=float(os.getenv("MARS_TEMPERATURE", "0.3")),
        base_prompt=_read_prompt_from_env("MARS_BASE_PROMPT"),
    )
    agent_prompt = _read_prompt_from_env("MARS_AGENT_PROMPT")

    set_bash_workspace_store(workspace_store)
    set_workspace_tool_store(workspace_store)
    set_storage_file_store(file_store)

    setup_agents(
        config=config,
        store=conversation_store,
        sink=SSEEventSink(),
        tools=[
            ReadMemoryTool(),
            EditMemoryTool(),
            UseSkillTool(),
            StorageTool(),
            WorkspaceTool(),
            BashTool(),
        ],
        skills_provider=skills_store,
        rules_provider=rules_store,
        memory_provider=memory_store,
    )

    configure_router(
        auth_provider=auth_provider,
        default_agent_prompt=agent_prompt,
        file_store=file_store,
        workspace_store=workspace_store,
    )

    app = FastAPI(title="mars-daemons", version="0.4.0", lifespan=_lifespan)
    app.state.data_dir = root
    app.state.conversation_store = conversation_store
    app.state.file_store = file_store
    app.state.workspace_store = workspace_store
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        return JSONResponse(status_code=200, content={"status": "ready"})

    return app
