"""MCP client wrapper — thin lifecycle manager around mcp.ClientSession."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class MCPClientError(RuntimeError):
    """Raised on any MCP-level protocol/transport failure."""


@dataclass
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class MCPClient:
    """SSE-based MCP client."""

    def __init__(
        self,
        url: str,
        *,
        auth_headers: dict[str, str] | None = None,
        timeout_seconds: float = 10.0,
        sse_read_timeout_seconds: float = 300.0,
        allowlist: list[str] | None = None,
    ) -> None:
        from ..tools.http_tool import resolve_and_validate_url

        self._allowlist = allowlist or []
        resolved = resolve_and_validate_url(url, allowlist=self._allowlist)
        self._url = resolved.normalized_url
        self._pinned_ip = resolved.ip
        self._pinned_hostname = resolved.hostname
        self._auth_headers = dict(auth_headers or {})
        self._timeout = timeout_seconds
        self._sse_read_timeout = sse_read_timeout_seconds
        self._session = None
        self._exit_stack: AsyncExitStack | None = None
        self._connected = False

    @property
    def url(self) -> str:
        return self._url

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._connected:
            return

        from ..tools.http_tool import SSRFValidationError, resolve_and_validate_url

        try:
            fresh = resolve_and_validate_url(self._url, allowlist=self._allowlist)
        except SSRFValidationError as exc:
            raise MCPClientError(f"URL failed SSRF validation at connect time: {exc}") from exc

        if self._pinned_ip and fresh.ip and fresh.ip != self._pinned_ip:
            logger.warning(
                "agents_v2.mcp.dns_rebinding_detected",
                extra={
                    "hostname": fresh.hostname,
                    "pinned_ip": self._pinned_ip,
                    "current_ip": fresh.ip,
                },
            )
            raise MCPClientError(
                f"DNS for {fresh.hostname} changed since MCPClient "
                f"construction ({self._pinned_ip} → {fresh.ip}); "
                f"refusing to connect (possible DNS rebinding attack)",
            )

        try:
            from mcp import ClientSession  # type: ignore[import-not-found]
            from mcp.client.sse import sse_client  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MCPClientError(
                "mcp SDK not installed — `pip install mcp` to use MCPClient",
            ) from exc

        self._exit_stack = AsyncExitStack()
        try:
            read, write = await self._exit_stack.enter_async_context(
                sse_client(
                    self._url,
                    headers=self._auth_headers or None,
                    timeout=self._timeout,
                    sse_read_timeout=self._sse_read_timeout,
                ),
            )
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read, write),
            )
            await self._session.initialize()
            self._connected = True
            logger.info("agents_v2.mcp.connected", extra={"url": self._url})
        except Exception as exc:
            await self._safe_close_stack()
            raise MCPClientError(
                f"failed to connect to MCP server {self._url}: {exc}",
            ) from exc

    async def list_tools(self) -> list[MCPToolSpec]:
        if not self._connected or self._session is None:
            raise MCPClientError("MCPClient.list_tools called before connect()")

        try:
            result = await self._session.list_tools()
        except Exception as exc:
            raise MCPClientError(f"list_tools failed for {self._url}: {exc}") from exc

        specs: list[MCPToolSpec] = []
        for tool in getattr(result, "tools", []) or []:
            specs.append(
                MCPToolSpec(
                    name=getattr(tool, "name", "") or "",
                    description=getattr(tool, "description", "") or "",
                    input_schema=dict(getattr(tool, "inputSchema", None) or {}),
                ),
            )
        return specs

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if not self._connected or self._session is None:
            raise MCPClientError("MCPClient.invoke called before connect()")

        try:
            return await self._session.call_tool(tool_name, arguments or {})
        except Exception as exc:
            raise MCPClientError(
                f"invoke({tool_name}) failed on {self._url}: {exc}",
            ) from exc

    async def close(self) -> None:
        await self._safe_close_stack()
        self._session = None
        self._connected = False

    async def _safe_close_stack(self) -> None:
        if self._exit_stack is None:
            return
        try:
            await self._exit_stack.aclose()
        except Exception as exc:
            logger.warning(
                "agents_v2.mcp.close_error",
                extra={"url": self._url, "error": str(exc)},
            )
        finally:
            self._exit_stack = None

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
