"""External Model Context Protocol (MCP) client and ToolRegistry bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import httpx

from agent_forge.observability import get_logger
from agent_forge.tools.base import Tool, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_forge.config import MCPServerSettings, MCPSettings
    from agent_forge.sandbox.base import Sandbox

logger = get_logger("mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "agent-forge", "version": "0.6.0"}
_SAFE_CHILD_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "USER")
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_IDENT_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_LIST_PAGES = 20
_STREAM_READER_LIMIT = 4 * 1024 * 1024


class MCPError(RuntimeError):
    """Base error for MCP client and transport operations."""


class MCPTransportError(MCPError):
    """Raised when an MCP transport connection or RPC request fails."""


class MCPTimeoutError(MCPTransportError):
    """Raised when an MCP startup or tool invocation exceeds its timeout."""


class MCPCircuitOpenError(MCPError):
    """Raised when a server's circuit breaker rejects calls while open."""


@dataclass(frozen=True)
class MCPToolAnnotations:
    """Safety and behavioral hints advertised by an MCP server tool."""

    read_only: bool = False
    destructive: bool | None = None
    idempotent: bool = False
    open_world: bool = True
    title: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> MCPToolAnnotations:
        """Parse an optional MCP ``annotations`` object conservatively."""
        if not isinstance(raw, dict):
            return cls()
        destructive_raw = raw.get("destructiveHint")
        destructive = destructive_raw if isinstance(destructive_raw, bool) else None
        return cls(
            read_only=raw.get("readOnlyHint") is True,
            destructive=destructive,
            idempotent=raw.get("idempotentHint") is True,
            open_world=raw.get("openWorldHint") is not False,
            title=str(raw.get("title", "") or ""),
        )


@dataclass(frozen=True)
class MCPToolDescriptor:
    """Typed metadata describing one discovered remote MCP tool."""

    server_name: str
    remote_name: str
    registered_name: str
    description: str
    input_schema: dict[str, Any]
    annotations: MCPToolAnnotations = field(default_factory=MCPToolAnnotations)


@dataclass
class MCPServerStatus:
    """Connection and circuit-breaker status summary for one MCP server."""

    name: str
    transport: str
    connected: bool
    tool_count: int = 0
    circuit_state: str = "closed"
    error: str | None = None


def _sanitize_identifier(raw: str) -> str:
    cleaned = _IDENT_RE.sub("_", raw.strip()).strip("_")
    return cleaned or "item"


def normalize_mcp_tool_name(server_name: str, remote_name: str) -> str:
    """Build a collision-resistant ToolRegistry name for an MCP tool."""
    return f"mcp__{_sanitize_identifier(server_name)}__{_sanitize_identifier(remote_name)}"


def _normalize_input_schema(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("type"), str):
        schema = dict(raw)
        if schema.get("type") == "object" and "properties" not in schema:
            schema["properties"] = {}
        return schema
    if isinstance(raw, dict) and "properties" in raw:
        return {"type": "object", **raw}
    return {"type": "object", "properties": {}}


def _expand_env_value(raw: str) -> str:
    return _ENV_PLACEHOLDER_RE.sub(lambda m: os.environ.get(m.group(1), ""), raw)


def _build_stdio_env(settings: MCPServerSettings) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _SAFE_CHILD_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    for key in settings.env_passthrough:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    for key, raw_val in settings.env.items():
        env[key] = _expand_env_value(raw_val)
    return env


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    suffix = f"\n... [truncated {len(text) - max_chars} chars]"
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix, True


def _format_content_item(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    kind = str(item.get("type", ""))
    if kind == "text":
        return str(item.get("text", ""))
    if kind == "resource":
        res = item.get("resource")
        if isinstance(res, dict):
            uri = str(res.get("uri", ""))
            body = res.get("text")
            if isinstance(body, str):
                return f"[{uri}]\n{body}" if uri else body
            return f"[resource: {uri or 'binary'}]"
    if kind in ("image", "audio"):
        mime = str(item.get("mimeType", "application/octet-stream"))
        return f"[{kind}: {mime}]"
    return json.dumps(item, ensure_ascii=False)


def _format_tool_result_payload(payload: dict[str, Any], max_chars: int) -> tuple[str, bool, bool]:
    parts: list[str] = []
    content = payload.get("content")
    if isinstance(content, list):
        parts.extend(_format_content_item(item) for item in content)
    structured = payload.get("structuredContent")
    if structured is not None:
        parts.append(json.dumps(structured, indent=2, ensure_ascii=False))
    raw_text = "\n".join(part for part in parts if part)
    text, truncated = _truncate_text(raw_text, max_chars)
    is_error = payload.get("isError") is True
    return text, is_error, truncated


def _extract_jsonrpc_message(
    raw_payload: dict[str, Any],
    req_id: int,
) -> dict[str, Any]:
    if "error" in raw_payload and raw_payload["error"] is not None:
        err = raw_payload["error"]
        if isinstance(err, dict):
            code = err.get("code", -32000)
            message = err.get("message", "Unknown MCP JSON-RPC error")
            msg = f"MCP JSON-RPC error {code}: {message}"
        else:
            msg = f"MCP JSON-RPC error: {err}"
        raise MCPTransportError(msg)
    if raw_payload.get("id") not in (None, req_id):
        msg = f"Mismatched JSON-RPC response id {raw_payload.get('id')} (expected {req_id})"
        raise MCPTransportError(msg)
    result = raw_payload.get("result")
    return result if isinstance(result, dict) else {"value": result}


def _extract_jsonrpc_from_sse(body: str, req_id: int) -> dict[str, Any]:
    last_matching: dict[str, Any] | None = None
    data_lines: list[str] = []
    for raw_line in [*body.splitlines(), ""]:
        line = raw_line.strip()
        if not line:
            if data_lines:
                candidate = "\n".join(data_lines).strip()
                data_lines.clear()
                parsed = _try_parse_json_dict(candidate)
                if parsed is not None and parsed.get("id") == req_id:
                    last_matching = parsed
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if last_matching is None:
        msg = f"No JSON-RPC response matching id={req_id} in SSE stream"
        raise MCPTransportError(msg)
    return _extract_jsonrpc_message(last_matching, req_id)


def _try_parse_json_dict(text: str) -> dict[str, Any] | None:
    if not text or not text.lstrip().startswith("{"):
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


class MCPCircuitBreaker:
    """Three-state circuit breaker isolating failing MCP servers."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._recovery_timeout_seconds = max(0.001, recovery_timeout_seconds)
        self._clock = clock
        self._state: Literal["closed", "open", "half_open"] = "closed"
        self._failure_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        """Current effective state accounting for recovery cooldown expiry."""
        if (
            self._state == "open"
            and self._opened_at is not None
            and (self._clock() - self._opened_at) >= self._recovery_timeout_seconds
        ):
            return "half_open"
        return self._state

    @property
    def retry_after_seconds(self) -> float:
        """Remaining cooldown seconds while open, else ``0.0``."""
        if self._state != "open" or self._opened_at is None:
            return 0.0
        remaining = self._recovery_timeout_seconds - (self._clock() - self._opened_at)
        return max(0.0, round(remaining, 3))

    def allow_request(self) -> bool:
        """Return whether a request is permitted right now."""
        current = self.state
        if current == "open":
            return False
        if current == "half_open":
            self._state = "half_open"
        return True

    def record_success(self) -> None:
        """Reset failure counters and close the circuit."""
        self._state = "closed"
        self._failure_count = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """Record a transport or timeout failure and trip open when threshold hits."""
        self._failure_count += 1
        if self._state == "half_open" or self._failure_count >= self._failure_threshold:
            self._state = "open"
            self._opened_at = self._clock()


class MCPTransport(ABC):
    """Abstract JSON-RPC 2.0 transport for an external MCP server."""

    @abstractmethod
    async def connect(self, *, timeout: float) -> None:
        """Open the underlying transport connection."""

    @abstractmethod
    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await its result payload."""

    @abstractmethod
    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a one-way JSON-RPC notification."""

    @abstractmethod
    async def close(self) -> None:
        """Close the transport and release subprocess/network resources."""


class StdioMCPTransport(MCPTransport):
    """Line-delimited JSON-RPC 2.0 MCP client transport over subprocess stdio."""

    def __init__(self, settings: MCPServerSettings) -> None:
        self._settings = settings
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    async def connect(self, *, timeout: float) -> None:
        """Spawn the configured MCP subprocess."""
        if self._process is not None and self._process.returncode is None:
            return
        command = self._settings.command.strip()
        if not command:
            msg = "Stdio MCP server requires a non-empty 'command'"
            raise MCPTransportError(msg)
        cwd = self._settings.cwd.strip() or None
        child_env = _build_stdio_env(self._settings)
        try:
            self._process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    command,
                    *self._settings.args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=child_env,
                    limit=_STREAM_READER_LIMIT,
                ),
                timeout=timeout,
            )
        except TimeoutError as exc:
            msg = f"Timed out starting MCP server '{command}' after {timeout}s"
            raise MCPTimeoutError(msg) from exc
        except OSError as exc:
            msg = f"Failed to start MCP server '{command}': {exc}"
            raise MCPTransportError(msg) from exc

    async def _write_message(self, payload: dict[str, Any]) -> None:
        proc = self._process
        if proc is None or proc.stdin is None or proc.returncode is not None:
            msg = "Stdio MCP server process is not running"
            raise MCPTransportError(msg)
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(line)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            msg = f"Failed writing to MCP server stdin: {exc}"
            raise MCPTransportError(msg) from exc

    async def _read_response(self, req_id: int, timeout: float) -> dict[str, Any]:
        proc = self._process
        if proc is None or proc.stdout is None:
            msg = "Stdio MCP server stdout is unavailable"
            raise MCPTransportError(msg)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = f"MCP request id={req_id} timed out after {timeout}s"
                raise MCPTimeoutError(msg)
            try:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except TimeoutError as exc:
                msg = f"MCP request id={req_id} timed out after {timeout}s"
                raise MCPTimeoutError(msg) from exc
            if not raw_line:
                detail = await self._read_stderr_preview()
                msg = f"MCP server closed stdout unexpectedly{f': {detail}' if detail else ''}"
                raise MCPTransportError(msg)
            parsed = _try_parse_json_dict(raw_line.decode("utf-8", errors="replace").strip())
            if parsed is not None and parsed.get("id") == req_id:
                return _extract_jsonrpc_message(parsed, req_id)

    async def _read_stderr_preview(self) -> str:
        proc = self._process
        if proc is None or proc.stderr is None:
            return ""
        with contextlib.suppress(TimeoutError, OSError):
            chunk = await asyncio.wait_for(proc.stderr.read(1024), timeout=0.1)
            return chunk.decode("utf-8", errors="replace").strip()
        return ""

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Send one JSON-RPC request and await the matching response line."""
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                payload["params"] = params
            await self._write_message(payload)
            return await self._read_response(req_id, timeout)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Write a one-way JSON-RPC notification over stdin."""
        async with self._lock:
            payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            await self._write_message(payload)

    async def close(self) -> None:
        """Close stdin and terminate the stdio MCP subprocess cleanly."""
        proc = self._process
        self._process = None
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(OSError, RuntimeError):
                proc.stdin.close()
                await proc.stdin.wait_closed()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(OSError):
                    await proc.wait()


class HttpMCPTransport(MCPTransport):
    """Streamable HTTP JSON-RPC 2.0 MCP client transport (JSON + SSE)."""

    def __init__(
        self,
        settings: MCPServerSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._external_client = http_client is not None
        self._client = http_client
        self._session_id: str | None = None
        self._next_id = 0

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.timeout_seconds)
        return self._client

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._settings.headers,
        }
        if self._settings.auth_env:
            token = os.environ.get(self._settings.auth_env, "").strip()
            if not token:
                from agent_forge.auth import resolve_auth_env_fallback

                token = resolve_auth_env_fallback(self._settings.auth_env) or ""
            if not token:
                msg = f"Missing required auth env var '{self._settings.auth_env}' for MCP HTTP"
                raise MCPTransportError(msg)
            headers["Authorization"] = f"{self._settings.auth_scheme} {token}".strip()
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        return headers

    async def connect(self, *, timeout: float) -> None:
        """Validate URL configuration and ensure an HTTP client exists."""
        del timeout
        if not self._settings.url.strip():
            msg = "HTTP MCP server requires a non-empty 'url'"
            raise MCPTransportError(msg)
        self._ensure_client()

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """POST a JSON-RPC request and decode a JSON or SSE response."""
        self._next_id += 1
        req_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        client = self._ensure_client()
        try:
            response = await client.post(
                self._settings.url,
                json=payload,
                headers=self._build_headers(),
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            msg = f"HTTP MCP request '{method}' timed out after {timeout}s"
            raise MCPTimeoutError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"HTTP MCP request '{method}' failed: {exc}"
            raise MCPTransportError(msg) from exc

        sid = response.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if response.status_code >= 400:
            msg = f"HTTP MCP server returned status {response.status_code}: {response.text[:300]}"
            raise MCPTransportError(msg)
        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            return _extract_jsonrpc_from_sse(response.text, req_id)
        try:
            parsed = response.json()
        except ValueError as exc:
            msg = f"Invalid JSON response from HTTP MCP server: {exc}"
            raise MCPTransportError(msg) from exc
        if not isinstance(parsed, dict):
            msg = "HTTP MCP server response must be a JSON object"
            raise MCPTransportError(msg)
        return _extract_jsonrpc_message(parsed, req_id)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """POST a JSON-RPC notification over HTTP."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        client = self._ensure_client()
        try:
            response = await client.post(
                self._settings.url,
                json=payload,
                headers=self._build_headers(),
                timeout=self._settings.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            msg = f"HTTP MCP notification '{method}' timed out"
            raise MCPTimeoutError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"HTTP MCP notification '{method}' failed: {exc}"
            raise MCPTransportError(msg) from exc
        if response.status_code >= 400:
            msg = f"HTTP MCP notification failed with status {response.status_code}"
            raise MCPTransportError(msg)

    async def close(self) -> None:
        """Delete any active session and close the underlying HTTP client if owned."""
        client = self._client
        if client is None:
            return
        if self._session_id:
            with contextlib.suppress(httpx.HTTPError, MCPTransportError):
                await client.delete(
                    self._settings.url,
                    headers=self._build_headers(),
                    timeout=5.0,
                )
            self._session_id = None
        if not self._external_client:
            await client.aclose()
            self._client = None


def _build_transport(
    settings: MCPServerSettings,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> MCPTransport:
    if settings.transport == "http":
        return HttpMCPTransport(settings, http_client=http_client)
    return StdioMCPTransport(settings)


class MCPServerClient:
    """Managed connection to a single external MCP server with circuit breaking."""

    def __init__(
        self,
        name: str,
        settings: MCPServerSettings,
        *,
        transport: MCPTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.settings = settings
        self._transport = transport or _build_transport(settings, http_client=http_client)
        self._breaker = MCPCircuitBreaker(
            failure_threshold=settings.failure_threshold,
            recovery_timeout_seconds=settings.recovery_timeout_seconds,
            clock=clock,
        )
        self._tools: list[MCPToolDescriptor] = []
        self._connected = False
        self._last_error: str | None = None

    @property
    def tools(self) -> list[MCPToolDescriptor]:
        """Discovered and policy-filtered tool descriptors for this server."""
        return list(self._tools)

    @property
    def status(self) -> MCPServerStatus:
        """Current health and tool-count snapshot for this server."""
        return MCPServerStatus(
            name=self.name,
            transport=self.settings.transport,
            connected=self._connected,
            tool_count=len(self._tools),
            circuit_state=self._breaker.state,
            error=self._last_error,
        )

    def _is_tool_allowed(self, remote_name: str) -> bool:
        if remote_name in self.settings.deny_tools:
            return False
        if self.settings.allow_tools:
            return remote_name in self.settings.allow_tools
        return True

    def _parse_tool_descriptors(self, raw_tools: Any) -> list[MCPToolDescriptor]:
        if not isinstance(raw_tools, list):
            return []
        descriptors: list[MCPToolDescriptor] = []
        for entry in raw_tools:
            if not isinstance(entry, dict):
                continue
            remote_name = str(entry.get("name", "")).strip()
            if not remote_name or not self._is_tool_allowed(remote_name):
                continue
            desc = str(entry.get("description", "") or f"MCP tool '{remote_name}' on {self.name}")
            schema = _normalize_input_schema(entry.get("inputSchema"))
            annotations = MCPToolAnnotations.from_raw(entry.get("annotations"))
            descriptors.append(
                MCPToolDescriptor(
                    server_name=self.name,
                    remote_name=remote_name,
                    registered_name=normalize_mcp_tool_name(self.name, remote_name),
                    description=f"[MCP:{self.name}] {desc}",
                    input_schema=schema,
                    annotations=annotations,
                )
            )
        return descriptors

    async def _fetch_all_tools(self) -> list[MCPToolDescriptor]:
        discovered: list[MCPToolDescriptor] = []
        cursor: str | None = None
        timeout = self.settings.startup_timeout_seconds
        for _ in range(_MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self._transport.request("tools/list", params, timeout=timeout)
            discovered.extend(self._parse_tool_descriptors(result.get("tools")))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return discovered

    async def connect(self) -> list[MCPToolDescriptor]:
        """Perform MCP initialization handshake and ingest tool schemas."""
        timeout = self.settings.startup_timeout_seconds
        try:
            await self._transport.connect(timeout=timeout)
            await self._transport.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
                timeout=timeout,
            )
            await self._transport.notify("notifications/initialized")
            self._tools = await self._fetch_all_tools()
            self._connected = True
            self._last_error = None
            self._breaker.record_success()
            return self.tools
        except (MCPError, OSError) as exc:
            self._connected = False
            self._last_error = str(exc)
            self._breaker.record_failure()
            with contextlib.suppress(Exception):
                await self._transport.close()
            raise MCPError(f"MCP server '{self.name}' failed to connect: {exc}") from exc

    async def call_tool(self, remote_name: str, arguments: dict[str, Any]) -> ToolResult:
        """Invoke a remote MCP tool with circuit breaking and bounded errors."""
        if not self._breaker.allow_request():
            retry_after = self._breaker.retry_after_seconds
            return ToolResult(
                output="",
                error=(
                    f"MCP server '{self.name}' circuit breaker is open "
                    f"(retry after {retry_after}s)."
                ),
                exit_code=1,
                metadata={
                    "mcp_server": self.name,
                    "mcp_remote_tool": remote_name,
                    "mcp_circuit_open": True,
                    "mcp_circuit_state": self._breaker.state,
                },
            )
        try:
            payload = await self._transport.request(
                "tools/call",
                {"name": remote_name, "arguments": arguments},
                timeout=self.settings.timeout_seconds,
            )
        except (MCPError, OSError) as exc:
            self._breaker.record_failure()
            self._last_error = str(exc)
            logger.warning(
                "mcp_tool_call_failed",
                server=self.name,
                tool=remote_name,
                circuit_state=self._breaker.state,
                error=str(exc),
            )
            return ToolResult(
                output="",
                error=f"MCP server '{self.name}' call failed: {exc}",
                exit_code=1,
                metadata={
                    "mcp_server": self.name,
                    "mcp_remote_tool": remote_name,
                    "mcp_circuit_state": self._breaker.state,
                },
            )

        self._breaker.record_success()
        self._last_error = None
        text, is_error, truncated = _format_tool_result_payload(
            payload,
            self.settings.max_output_chars,
        )
        metadata: dict[str, Any] = {
            "mcp_server": self.name,
            "mcp_remote_tool": remote_name,
            "mcp_truncated": truncated,
        }
        if is_error:
            return ToolResult(
                output="",
                error=text or f"MCP tool '{remote_name}' returned an error.",
                exit_code=1,
                metadata=metadata,
            )
        return ToolResult(output=text, exit_code=0, metadata=metadata)

    async def close(self) -> None:
        """Close the server transport connection."""
        self._connected = False
        await self._transport.close()


class MCPTool(Tool):
    """Adapter exposing a remote MCP tool through Agent Forge's typed Tool ABC."""

    is_mcp_tool: bool = True

    def __init__(self, descriptor: MCPToolDescriptor, client: MCPServerClient) -> None:
        self._descriptor = descriptor
        self._client = client

    @property
    def name(self) -> str:
        """Registered ToolRegistry name (``mcp__<server>__<tool>``)."""
        return self._descriptor.registered_name

    @property
    def description(self) -> str:
        """Human-readable description with server attribution."""
        return self._descriptor.description

    @property
    def parameters(self) -> dict[str, Any]:
        """Normalized JSON Schema parameters object."""
        return self._descriptor.input_schema

    @property
    def server_name(self) -> str:
        """Configured server identifier."""
        return self._descriptor.server_name

    @property
    def remote_name(self) -> str:
        """Unprefixed tool name exposed by the remote MCP server."""
        return self._descriptor.remote_name

    @property
    def annotations(self) -> MCPToolAnnotations:
        """MCP tool safety annotations."""
        return self._descriptor.annotations

    @property
    def approval_mode(self) -> str:
        """Per-server approval override (``inherit|auto|prompt|deny``)."""
        return self._client.settings.approval_mode

    @property
    def allow_read_only_without_approval(self) -> bool:
        """Whether explicitly annotated read-only tools skip approval prompts."""
        return self._client.settings.allow_read_only_without_approval

    @property
    def side_effecting(self) -> bool:
        """Default to True unless read-only hint AND server opt-in both hold."""
        ann = self._descriptor.annotations
        return not (
            ann.read_only and ann.destructive is not True and self.allow_read_only_without_approval
        )

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Execute the remote MCP tool via its circuit-broken client."""
        del sandbox
        started = time.monotonic()
        result = await self._client.call_tool(self._descriptor.remote_name, arguments)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return ToolResult(
            output=result.output,
            error=result.error,
            exit_code=result.exit_code,
            execution_time_ms=elapsed_ms,
            metadata=result.metadata,
        )


class MCPClientManager:
    """Connects all configured MCP servers and registers tools safely."""

    def __init__(self, clients: dict[str, MCPServerClient] | None = None) -> None:
        self._clients: dict[str, MCPServerClient] = dict(clients or {})

    @classmethod
    def from_settings(
        cls,
        settings: MCPSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        http_client: httpx.AsyncClient | None = None,
    ) -> MCPClientManager:
        """Build a manager for all enabled servers in *settings*."""
        clients: dict[str, MCPServerClient] = {}
        if not settings.enabled:
            return cls(clients)
        for name, server_cfg in settings.servers.items():
            if server_cfg.enabled:
                clients[name] = MCPServerClient(
                    name,
                    server_cfg,
                    clock=clock,
                    http_client=http_client,
                )
        return cls(clients)

    @property
    def clients(self) -> dict[str, MCPServerClient]:
        """Configured active MCP server clients keyed by server name."""
        return dict(self._clients)

    def statuses(self) -> list[MCPServerStatus]:
        """Return health and tool-count snapshots for every configured server."""
        return [client.status for client in self._clients.values()]

    async def _connect_one(self, client: MCPServerClient) -> MCPServerStatus:
        try:
            await client.connect()
        except (MCPError, OSError) as exc:
            logger.warning("mcp_server_unavailable", server=client.name, error=str(exc))
        return client.status

    async def connect_all(self) -> list[MCPServerStatus]:
        """Connect to all enabled MCP servers concurrently with failure isolation."""
        if not self._clients:
            return []
        tasks = [self._connect_one(client) for client in self._clients.values()]
        return list(await asyncio.gather(*tasks))

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        """Register all discovered MCP tools into *registry*, skipping collisions."""
        registered: list[str] = []
        for client in self._clients.values():
            for descriptor in client.tools:
                tool = MCPTool(descriptor, client)
                try:
                    registry.register(tool)
                except ValueError:
                    logger.warning(
                        "mcp_tool_name_collision",
                        server=client.name,
                        tool=tool.name,
                    )
                    continue
                registered.append(tool.name)
        return registered

    async def close(self) -> None:
        """Close every managed MCP server connection cleanly."""
        if not self._clients:
            return
        await asyncio.gather(
            *(client.close() for client in self._clients.values()),
            return_exceptions=True,
        )

    async def __aenter__(self) -> MCPClientManager:
        await self.connect_all()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
