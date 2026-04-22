"""HttpToolTemplate — custom HTTP endpoint wrapper with SSRF protection."""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from ..core.tools import AuthContext, BaseTool, ToolResult

logger = logging.getLogger(__name__)

# ─── SSRF protection ─────────────────────────────────────────────────────

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTNAME_SUFFIXES = (
    ".internal",
    ".local",
    ".localhost",
)
_CLOUD_METADATA_HOSTS = frozenset({
    "169.254.169.254",
    "100.100.100.200",
    "metadata.google.internal",
    "metadata.goog",
})

DEFAULT_RESPONSE_CAP_BYTES = 50 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0


class SSRFValidationError(ValueError):
    """Raised when the target URL fails SSRF validation."""


def _iter_resolved_addresses(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return []
    return [info[4][0] for info in infos]


def _is_blocked_address(addr_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(addr_str)
    except ValueError:
        return False
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _normalize_hostname(raw: str) -> str:
    host = (raw or "").strip().lower()
    while host.endswith("."):
        host = host[:-1]
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        pass
    return host


@dataclass
class ResolvedTarget:
    normalized_url: str
    hostname: str
    ip: str
    port: int
    scheme: str


def resolve_and_validate_url(
    url: str,
    *,
    allowlist: list[str] | None = None,
) -> ResolvedTarget:
    if not url or not isinstance(url, str):
        raise SSRFValidationError("URL must be a non-empty string")

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFValidationError(
            f"scheme '{scheme}' not allowed; only http/https permitted",
        )

    raw_host = parsed.hostname or ""
    host = _normalize_hostname(raw_host)
    if not host:
        raise SSRFValidationError("URL must have a hostname")

    if "@" in (parsed.netloc or ""):
        raise SSRFValidationError("URLs must not contain userinfo")

    allowlist_normalized = {_normalize_hostname(h) for h in (allowlist or [])}
    in_allowlist = host in allowlist_normalized

    if not in_allowlist:
        if host in _CLOUD_METADATA_HOSTS:
            raise SSRFValidationError(f"cloud metadata host '{host}' is blocked")
        for suffix in _BLOCKED_HOSTNAME_SUFFIXES:
            if host.endswith(suffix) or host == suffix.lstrip("."):
                raise SSRFValidationError(f"hostname suffix '{suffix}' is blocked")
        if _is_blocked_address(host):
            raise SSRFValidationError(f"IP address '{host}' is in a blocked range")

    resolved_addresses = _iter_resolved_addresses(host)

    is_literal_ip = False
    if not resolved_addresses:
        try:
            ipaddress.ip_address(host)
            resolved_addresses = [host]
            is_literal_ip = True
        except ValueError:
            # Fail closed: if hostname can't be resolved and isn't allowlisted,
            # refuse the connection. A hostname that doesn't resolve now could
            # resolve to an internal IP later.
            if not in_allowlist:
                raise SSRFValidationError(
                    f"hostname '{host}' could not be resolved and is not allowlisted"
                )
            resolved_addresses = []

    if not in_allowlist and resolved_addresses:
        for addr in resolved_addresses:
            if _is_blocked_address(addr):
                raise SSRFValidationError(
                    f"hostname '{host}' resolves to blocked address '{addr}'",
                )

    pinned_ip = resolved_addresses[0] if resolved_addresses else ""
    port = parsed.port or (443 if scheme == "https" else 80)
    normalized_url = url.strip()
    return ResolvedTarget(
        normalized_url=normalized_url,
        hostname=host,
        ip=pinned_ip,
        port=port,
        scheme=scheme,
    )


def validate_url_for_ssrf(url: str, *, allowlist: list[str] | None = None) -> str:
    resolved = resolve_and_validate_url(url, allowlist=allowlist)
    return resolved.normalized_url


# ─── HttpToolTemplate ────────────────────────────────────────────────────

_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_AUTH_TYPES = frozenset({"none", "bearer", "header"})


def _scrub_response_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in {"set-cookie", "set-cookie2"}
    }


class HttpToolTemplate(BaseTool):
    execution_mode: str = "parallel"

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict,
        url: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        auth_type: str = "none",
        auth_token: str | None = None,
        auth_header_name: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        response_cap_bytes: int = DEFAULT_RESPONSE_CAP_BYTES,
        allowlist: list[str] | None = None,
    ) -> None:
        method_upper = (method or "POST").upper()
        if method_upper not in _ALLOWED_METHODS:
            raise ValueError(f"method '{method}' not allowed")
        if auth_type not in _AUTH_TYPES:
            raise ValueError(f"auth_type '{auth_type}' not allowed")
        if auth_type == "header" and not auth_header_name:
            raise ValueError("auth_type='header' requires auth_header_name")
        if auth_type in ("bearer", "header") and not auth_token:
            raise ValueError(f"auth_type='{auth_type}' requires auth_token")

        resolved = resolve_and_validate_url(url, allowlist=allowlist)
        timeout = max(0.1, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))

        self.name = name
        self.description = description
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        self._url = resolved.normalized_url
        self._pinned_ip = resolved.ip
        self._pinned_hostname = resolved.hostname
        self._method = method_upper
        self._headers = dict(headers or {})
        self._auth_type = auth_type
        self._auth_token = auth_token
        self._auth_header_name = auth_header_name
        self._timeout = timeout
        self._response_cap = max(1024, int(response_cap_bytes))
        self._allowlist = list(allowlist or [])

    def _build_request_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "agents-http-tool/1.0",
            "Accept": "application/json",
            **self._headers,
        }
        if self._auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self._auth_token}"
        elif self._auth_type == "header":
            headers[self._auth_header_name] = self._auth_token  # type: ignore[index]
        return headers

    async def _execute(
        self,
        input: dict,
        auth: AuthContext,
        state: dict,
    ) -> ToolResult:
        try:
            fresh = resolve_and_validate_url(self._url, allowlist=self._allowlist)
        except SSRFValidationError as exc:
            return ToolResult(success=False, error=f"URL failed SSRF validation: {exc}")

        if self._pinned_ip and fresh.ip and fresh.ip != self._pinned_ip:
            logger.warning(
                "agents_v2.http_tool.dns_rebinding_detected",
                extra={
                    "tool": self.name,
                    "hostname": fresh.hostname,
                    "pinned_ip": self._pinned_ip,
                    "current_ip": fresh.ip,
                },
            )
            return ToolResult(
                success=False,
                error=(
                    f"DNS for {fresh.hostname} changed since tool registration "
                    f"({self._pinned_ip} → {fresh.ip}); refusing to connect "
                    f"(possible DNS rebinding attack)"
                ),
            )

        request_headers = self._build_request_headers()
        request_kwargs: dict[str, Any] = {
            "method": self._method,
            "url": self._url,
            "headers": request_headers,
            "timeout": self._timeout,
            "follow_redirects": False,
        }
        if self._method in ("POST", "PUT", "PATCH"):
            request_kwargs["json"] = input
        else:
            request_kwargs["params"] = input

        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(**request_kwargs)
        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                error=f"Request to {self._url} timed out after {self._timeout:.1f}s",
            )
        except httpx.RequestError as exc:
            return ToolResult(
                success=False,
                error=f"Request to {self._url} failed: {exc.__class__.__name__}: {exc}",
            )

        if response.status_code >= 400:
            return ToolResult(
                success=False,
                error=f"HTTP {response.status_code} from {self._url}: {response.text[:500]}",
                data={
                    "status_code": response.status_code,
                    "headers": _scrub_response_headers(dict(response.headers)),
                },
            )

        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" not in content_type:
            return ToolResult(
                success=False,
                error=(
                    f"unexpected content-type '{content_type}' from {self._url} "
                    f"(HttpToolTemplate requires JSON responses)"
                ),
            )

        raw_bytes = response.content
        truncated = False
        if len(raw_bytes) > self._response_cap:
            raw_bytes = raw_bytes[: self._response_cap]
            truncated = True

        try:
            import json
            parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"failed to parse JSON response from {self._url}: {exc}",
            )

        result_data: dict[str, Any] = {
            "status_code": response.status_code,
            "body": parsed,
            "headers": _scrub_response_headers(dict(response.headers)),
        }
        if truncated:
            result_data["truncated"] = True
            result_data["truncation_note"] = (
                f"response body capped at {self._response_cap} bytes "
                f"(full size was {len(response.content)} bytes)"
            )

        return ToolResult(success=True, data=result_data)
