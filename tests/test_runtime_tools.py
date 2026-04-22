from __future__ import annotations

import asyncio

from mars_runtime.core.tools import AuthContext
from mars_runtime.host.stores.file_store import LocalFileStore
from mars_runtime.host.stores.workspace_store import LocalWorkspaceStore
from mars_runtime.tools.bash import BashTool, set_workspace_store as set_bash_workspace_store
from mars_runtime.tools.storage import StorageTool, set_storage_file_store
from mars_runtime.tools.workspace import WorkspaceTool, set_workspace_store as set_workspace_tool_store


def test_workspace_and_bash_tools_share_conversation_workspace(tmp_path):
    async def scenario() -> None:
        workspace_store = LocalWorkspaceStore(tmp_path)
        set_workspace_tool_store(workspace_store)
        set_bash_workspace_store(workspace_store)

        auth = AuthContext(org_id="org-1", user_id="user-1")
        state = {"conversation_id": "conv-1"}

        workspace_tool = WorkspaceTool()
        upload_result = await workspace_tool.execute(
            {"action": "upload", "path": "greeting.txt", "content": "hola mundo"},
            auth,
            state,
        )
        assert upload_result.success is True

        bash_tool = BashTool()
        bash_result = await bash_tool.execute({"command": "cat greeting.txt"}, auth, state)
        assert bash_result.success is True
        assert bash_result.data["stdout"] == "hola mundo"
        assert bash_result.data["exit_code"] == 0

        list_result = await workspace_tool.execute({"action": "list"}, auth, state)
        assert list_result.success is True
        assert list_result.data["entries"][0]["name"] == "greeting.txt"

    asyncio.run(scenario())


def test_storage_tool_lists_gets_url_and_deletes_files(tmp_path):
    async def scenario() -> None:
        file_store = LocalFileStore(tmp_path)
        set_storage_file_store(file_store)

        file_ref = await file_store.upload("conv-1", "note.txt", b"hola", "text/plain")
        state = {"files": [file_ref.to_dict()]}
        auth = AuthContext(org_id="org-1", user_id="user-1")

        tool = StorageTool()

        list_result = await tool.execute({"action": "list"}, auth, state)
        assert list_result.success is True
        assert list_result.data["count"] == 1

        url_result = await tool.execute({"action": "get_url", "key": file_ref.key}, auth, state)
        assert url_result.success is True
        assert url_result.data["url"].endswith(file_ref.key)

        delete_result = await tool.execute({"action": "delete", "key": file_ref.key}, auth, state)
        assert delete_result.success is True
        assert delete_result.data["deleted"] is True
        assert state["files"] == []

    asyncio.run(scenario())
