"""MCPTool — wraps a single remote MCP tool as a first-class BaseTool."""

from __future__ import annotations

import logging
import re
from typing import Any

from ..mcp.client import MCPClient, MCPClientError, MCPToolSpec
from ..core.tools import AuthContext, BaseTool, ToolResult

logger = logging.getLogger(__name__)

MCP_TOOL_NAME_PREFIX = "mcp_"


def make_mcp_tool_slug(server_name: str) -> str:
    lowered = (server_name or "").strip().lower()
    slugged = re.sub(r"[^a-z0-9_]+", "_", lowered).strip("_")
    return slugged or "server"


def make_mcp_tool_name(server_slug: str, tool_name: str) -> str:
    safe_tool = re.sub(r"[^a-zA-Z0-9_]+", "_", tool_name or "").strip("_") or "tool"
    return f"{MCP_TOOL_NAME_PREFIX}{server_slug}_{safe_tool}"


def _extract_text_content(raw_result: Any) -> Any:
    if raw_result is None:
        return None
    if isinstance(raw_result, (dict, list, str, int, float, bool)):
        return raw_result

    content = getattr(raw_result, "content", None)
    if content is None:
        dump = getattr(raw_result, "model_dump", None)
        if callable(dump):
            return dump()
        return str(raw_result)

    parts: list[Any] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", ""))
        else:
            dump = getattr(block, "model_dump", None)
            if callable(dump):
                parts.append(dump())
            else:
                parts.append(str(block))

    if parts and all(isinstance(p, str) for p in parts):
        return "".join(parts)
    return parts


class MCPTool(BaseTool):
    execution_mode: str = "parallel"

    def __init__(self, *, client: MCPClient, server_slug: str, spec: MCPToolSpec) -> None:
        self._client = client
        self._server_slug = server_slug
        self._remote_name = spec.name
        self.name = make_mcp_tool_name(server_slug, spec.name)
        self.description = spec.description or f"remote MCP tool '{spec.name}' on {server_slug}"
        self.input_schema = spec.input_schema or {"type": "object", "properties": {}}

    async def _execute(self, input: dict, auth: AuthContext, state: dict) -> ToolResult:
        try:
            raw = await self._client.invoke(self._remote_name, input or {})
        except MCPClientError as exc:
            return ToolResult(success=False, error=f"MCP tool '{self.name}' failed: {exc}")
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"MCP tool '{self.name}' raised {exc.__class__.__name__}: {exc}",
            )

        if getattr(raw, "isError", False):
            content = _extract_text_content(raw)
            return ToolResult(success=False, error=f"MCP tool '{self.name}' returned error: {content}")

        data = _extract_text_content(raw)
        return ToolResult(success=True, data={"content": data})
