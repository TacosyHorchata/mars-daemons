"""storage — inspect and manage conversation attachments."""

from __future__ import annotations

from typing import Any

from ..core.tools import AuthContext, BaseTool, ToolResult

_file_store: Any | None = None


def set_storage_file_store(store: Any) -> None:
    global _file_store
    _file_store = store


class StorageTool(BaseTool):
    name = "storage"
    description = (
        "Inspect or manage files attached to the current conversation. "
        "Actions: list, get_url, delete."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get_url", "delete"],
                "description": "The storage action to perform.",
            },
            "key": {
                "type": "string",
                "description": "The file key from the attachment list.",
            },
        },
        "required": ["action"],
    }

    async def _execute(self, input: dict, auth: AuthContext, state: dict) -> ToolResult:
        if _file_store is None:
            return ToolResult(success=False, error="File storage not configured")

        action = str(input.get("action", "")).strip()
        files = list(state.get("files") or [])

        if action == "list":
            return ToolResult(
                success=True,
                data={
                    "files": [
                        {
                            "key": entry.get("key", ""),
                            "filename": entry.get("filename", ""),
                            "mimetype": entry.get("mimetype", "application/octet-stream"),
                            "size": int(entry.get("size", 0) or 0),
                        }
                        for entry in files
                    ],
                    "count": len(files),
                },
            )

        key = str(input.get("key", "")).strip()
        if not key:
            return ToolResult(success=False, error="File key is required")

        file_entry = next((entry for entry in files if str(entry.get("key", "")) == key), None)
        if file_entry is None:
            return ToolResult(success=False, error=f"File key '{key}' is not attached to this conversation")

        if action == "get_url":
            try:
                url = await _file_store.get_url(key)
                return ToolResult(success=True, data={"url": url, "key": key})
            except Exception as exc:
                return ToolResult(success=False, error=f"Failed to get URL: {exc}")

        if action == "delete":
            try:
                deleted = await _file_store.delete(key)
                if deleted:
                    state["files"] = [entry for entry in files if str(entry.get("key", "")) != key]
                return ToolResult(success=True, data={"deleted": deleted, "key": key})
            except Exception as exc:
                return ToolResult(success=False, error=f"Failed to delete: {exc}")

        return ToolResult(success=False, error=f"Unknown action '{action}'. Use 'list', 'get_url', or 'delete'.")
