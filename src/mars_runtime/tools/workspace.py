"""workspace — structured file operations for the conversation workspace."""

from __future__ import annotations

import base64
import logging
from typing import Any

from ..core.tools import AuthContext, BaseTool, ToolResult

logger = logging.getLogger(__name__)

_workspace_store: Any | None = None


def set_workspace_store(store: Any) -> None:
    global _workspace_store
    _workspace_store = store


class WorkspaceTool(BaseTool):
    name = "workspace"
    description = (
        "Manage files in the current conversation workspace. "
        "Actions: list, read_url, upload, create_folder, delete."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read_url", "upload", "create_folder", "delete"],
            },
            "path": {
                "type": "string",
                "description": "Path inside the conversation workspace.",
                "default": "",
            },
            "content": {
                "type": "string",
                "description": "UTF-8 text content to write during upload.",
            },
            "content_base64": {
                "type": "string",
                "description": "Base64-encoded file content to write during upload.",
            },
            "mimetype": {
                "type": "string",
                "description": "MIME type for upload.",
                "default": "application/octet-stream",
            },
        },
        "required": ["action"],
    }

    async def _execute(self, input: dict, auth: AuthContext, state: dict) -> ToolResult:
        if _workspace_store is None:
            return ToolResult(success=False, error="Workspace not configured")

        action = str(input.get("action", "")).strip()
        path = str(input.get("path", "")).strip()
        conversation_id = str(state.get("conversation_id") or "").strip()
        if not conversation_id:
            return ToolResult(success=False, error="Conversation context is required")

        user_id = auth.user_id or "anonymous"

        try:
            if action == "list":
                entries = await _workspace_store.list_objects(auth.org_id, user_id, conversation_id, path)
                workspace_root = _workspace_store.workspace_root(auth.org_id, user_id, conversation_id)
                return ToolResult(
                    success=True,
                    data={
                        "path": path or "/",
                        "workspace_root": str(workspace_root),
                        "entries": [entry.to_dict() for entry in entries],
                        "count": len(entries),
                    },
                )

            if action == "read_url":
                if not path:
                    return ToolResult(success=False, error="Path is required for read_url")
                url = await _workspace_store.get_url(auth.org_id, user_id, conversation_id, path)
                return ToolResult(success=True, data={"url": url, "path": path})

            if action == "upload":
                if not path:
                    return ToolResult(success=False, error="Path is required for upload")
                if "content" in input:
                    content = str(input.get("content", "")).encode("utf-8")
                    mimetype = str(input.get("mimetype") or "text/plain; charset=utf-8")
                else:
                    content_b64 = str(input.get("content_base64", "")).strip()
                    if not content_b64:
                        return ToolResult(
                            success=False,
                            error="content or content_base64 is required for upload",
                        )
                    try:
                        content = base64.b64decode(content_b64)
                    except Exception:
                        return ToolResult(success=False, error="Invalid base64 content")
                    mimetype = str(input.get("mimetype") or "application/octet-stream")
                entry = await _workspace_store.upload_content(
                    auth.org_id,
                    user_id,
                    conversation_id,
                    path,
                    content,
                    mimetype,
                )
                return ToolResult(success=True, data=entry.to_dict())

            if action == "create_folder":
                if not path:
                    return ToolResult(success=False, error="Path is required for create_folder")
                entry = await _workspace_store.create_folder(auth.org_id, user_id, conversation_id, path)
                return ToolResult(success=True, data=entry.to_dict())

            if action == "delete":
                if not path:
                    return ToolResult(success=False, error="Path is required for delete")
                deleted = await _workspace_store.delete(auth.org_id, user_id, conversation_id, path)
                return ToolResult(success=True, data={"deleted": deleted, "path": path})

            return ToolResult(success=False, error=f"Unknown action '{action}'")
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            logger.error("Workspace tool error: %s", exc, exc_info=True)
            return ToolResult(success=False, error=f"Workspace error: {exc}")
