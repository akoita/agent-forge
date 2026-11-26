"""Bidirectional JSON-RPC 2.0 ACP server exposing Agent Forge to editors."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import posixpath
import sys
import uuid
from collections.abc import Awaitable, Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol

from agent_forge.acp.protocol import (
    ACP_MODE_IDS,
    ACP_PROTOCOL_VERSION,
    AGENT_INFO,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    build_agent_capabilities,
    build_available_commands_update,
    build_config_option_update,
    build_current_mode_update,
    build_replay_updates,
    build_session_config_options,
    build_session_modes,
    classify_tool_kind,
    extract_prompt_text,
    make_error,
    make_notification,
    make_request,
    make_response,
    map_run_state_to_stop_reason,
    map_stream_event_to_acp_updates,
    translate_acp_mcp_servers,
)
from agent_forge.agent.approvals import ApprovalGate, ApprovalRequest
from agent_forge.agent.budget import CancellationToken
from agent_forge.agent.models import AgentConfig, AgentRun
from agent_forge.agent.session import DevSession, SessionStore, SessionSummary, validate_session_id
from agent_forge.config import ForgeConfig, MCPServerSettings, load_config
from agent_forge.observability import get_logger
from agent_forge.orchestration.event_stream import (
    JournalEventSink,
    StreamEvent,
    sanitize_event_data,
)
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.tools.base import WORKSPACE_ROOT, Tool, ToolRegistry, ToolResult, validate_path

if TYPE_CHECKING:
    from agent_forge.agent.context import ContextEngine
    from agent_forge.sandbox.base import Sandbox

logger = get_logger("acp")

_LIST_PAGE_SIZE = 50
_VALID_APPROVAL_MODES = frozenset({"prompt", "auto", "deny"})
_VALID_EXECUTION_MODES = frozenset({"tool_calling", "code_mode"})


class AsyncLineReader(Protocol):
    """Minimal async line reader interface satisfied by ``asyncio.StreamReader``."""

    async def readline(self) -> bytes:
        """Read one newline-terminated line or ``b''`` on EOF."""


class AsyncLineWriter(Protocol):
    """Minimal async writer interface satisfied by ``asyncio.StreamWriter``."""

    def write(self, data: bytes) -> Any:
        """Buffer outbound encoded JSON-RPC bytes."""

    async def drain(self) -> None:
        """Flush buffered outbound bytes."""

    def close(self) -> None:
        """Close the outbound writer."""


class _StdioWriterAdapter:
    """Thread-safe writer targeting the captured binary stdout stream."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def write(self, data: bytes) -> None:
        self._stream.write(data)
        self._stream.flush()

    async def drain(self) -> None:
        await asyncio.to_thread(self._stream.flush)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._stream.flush()


class _StdioReaderAdapter:
    """Async line reader wrapping a binary input stream without epoll assumptions."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    async def readline(self) -> bytes:
        return await asyncio.to_thread(self._stream.readline)


@dataclass
class ACPSession:
    """Active ACP session state shared across turns."""

    session_id: str
    cwd: str
    mcp_servers: dict[str, MCPServerSettings] = field(default_factory=dict)
    allowed_categories: set[str] = field(default_factory=set)
    provider: str = ""
    model: str = ""
    approval_mode: str = "prompt"
    execution_mode: str = "tool_calling"
    mode_id: str = "code"
    dev_session: DevSession | None = None
    context_engine: ContextEngine | None = None
    active_run_task: asyncio.Task[None] | None = None
    cancellation_token: CancellationToken | None = None
    pending_permission_ids: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.dev_session is None:
            self.dev_session = DevSession(repo_path=self.cwd, id=self.session_id)


RunExecutor = Callable[
    [ACPSession, str, ForgeConfig, AgentConfig, EventBus, ApprovalGate, CancellationToken],
    Awaitable[AgentRun],
]


class _ClientBufferReadFileTool(Tool):
    """Wrap sandbox ``read_file`` to consult the ACP client's unsaved editor buffers first."""

    def __init__(
        self,
        inner: Tool,
        read_client_fn: Callable[[str], Awaitable[str | None]],
    ) -> None:
        self._inner = inner
        self._read_client_fn = read_client_fn

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def side_effecting(self) -> bool:
        return self._inner.side_effecting

    @property
    def description(self) -> str:
        return self._inner.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._inner.parameters

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        raw_path = str(arguments.get("path", "") or "").strip()
        if raw_path:
            try:
                resolved_ws = validate_path(raw_path)
            except ValueError:
                resolved_ws = ""
            if resolved_ws:
                client_text = await self._read_client_fn(raw_path)
                if client_text is not None:
                    return ToolResult(
                        output=client_text,
                        exit_code=0,
                        metadata={
                            "locations": [{"path": resolved_ws, "line": 1}],
                            "source": "client_fs",
                        },
                    )
        return await self._inner.execute(arguments, sandbox)


def _parse_permission_option(
    response_payload: dict[str, Any],
) -> tuple[bool, bool]:
    """Return ``(approved, remember_always)`` from ``session/request_permission``."""
    outcome = response_payload.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("outcome") != "selected":
        return False, False
    option_id = str(outcome.get("optionId", "")).strip()
    if option_id == "allow_always":
        return True, True
    if option_id == "allow_once":
        return True, False
    return False, False


def _clamp_protocol_version(raw_version: Any) -> int:
    if isinstance(raw_version, int) and not isinstance(raw_version, bool) and raw_version >= 1:
        return min(raw_version, ACP_PROTOCOL_VERSION)
    return ACP_PROTOCOL_VERSION


def _derive_mode_id(approval_mode: str, execution_mode: str) -> str:
    if execution_mode == "code_mode":
        return "code_mode"
    if approval_mode == "auto":
        return "auto"
    if approval_mode == "deny":
        return "ask"
    return "code"


def _apply_mode_selection(session: ACPSession, mode_id: str) -> None:
    session.mode_id = mode_id
    if mode_id == "code_mode":
        session.execution_mode = "code_mode"
        if session.approval_mode == "deny":
            session.approval_mode = "prompt"
    elif mode_id == "auto":
        session.execution_mode = "tool_calling"
        session.approval_mode = "auto"
    elif mode_id == "ask":
        session.execution_mode = "tool_calling"
        session.approval_mode = "deny"
    else:
        session.execution_mode = "tool_calling"
        session.approval_mode = "prompt"


def _map_session_summary(summary: SessionSummary) -> dict[str, Any]:
    return {
        "sessionId": summary.id,
        "cwd": summary.repo_path,
        "title": summary.title or "Untitled session",
        "updatedAt": summary.updated_at.isoformat(),
        "_meta": {
            "provider": summary.provider,
            "model": summary.model,
            "turnCount": summary.turn_count,
        },
    }


def _normalize_client_path(cwd: str, raw_path: str) -> str | None:
    cleaned = raw_path.strip()
    if not cleaned:
        return None
    if cleaned == WORKSPACE_ROOT or cleaned.startswith(WORKSPACE_ROOT + "/"):
        rel = posixpath.relpath(cleaned, WORKSPACE_ROOT)
        candidate = Path(cwd, rel)
    elif os.path.isabs(cleaned):
        candidate = Path(cleaned)
    else:
        candidate = Path(cwd, cleaned)
    root = Path(cwd).resolve()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root):
        return None
    return str(resolved)


class ACPServer:
    """JSON-RPC 2.0 ACP agent server bridged to Agent Forge runs and approvals."""

    def __init__(
        self,
        *,
        config: ForgeConfig | None = None,
        approval_mode: str = "prompt",
        provider_override: str | None = None,
        model_override: str | None = None,
        sandbox_backend_override: str | None = None,
        max_iterations_override: int | None = None,
        permission_timeout_seconds: float = 300.0,
        session_store: SessionStore | None = None,
        run_executor: RunExecutor | None = None,
        sandbox_factory: Any = None,
        provider_factory: Any = None,
        tools_factory: Any = None,
        auth_methods: list[dict[str, Any]] | None = None,
        enable_client_fs_read: bool = True,
    ) -> None:
        self._config = config or load_config()
        self._approval_mode = approval_mode
        self._provider_override = provider_override
        self._model_override = model_override
        self._sandbox_backend_override = sandbox_backend_override
        self._max_iterations_override = max_iterations_override
        self._permission_timeout_seconds = permission_timeout_seconds
        self._session_store = session_store or SessionStore()
        self._sandbox_factory = sandbox_factory
        self._provider_factory = provider_factory
        self._tools_factory = tools_factory
        self._auth_methods = [dict(item) for item in (auth_methods or [])]
        self._enable_client_fs_read = enable_client_fs_read
        self._run_executor = run_executor or self._default_run_executor
        self._sessions: dict[str, ACPSession] = {}
        self._pending_client_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_outbound_id = 0
        self._write_lock = asyncio.Lock()
        self._writer: AsyncLineWriter | None = None
        self._initialized = False
        self._client_capabilities: dict[str, Any] = {}
        self._client_info: dict[str, Any] = {}
        self._authenticated_method: str | None = None

    def client_supports_fs_read(self) -> bool:
        """Return True when both server and client enable ``fs/read_text_file``."""
        if not self._enable_client_fs_read:
            return False
        fs_caps = self._client_capabilities.get("fs")
        return isinstance(fs_caps, dict) and bool(fs_caps.get("readTextFile"))

    async def read_client_text_file(
        self,
        session: ACPSession,
        path: str,
        *,
        line: int | None = None,
        limit: int | None = None,
    ) -> str | None:
        """Request workspace-contained file content from the ACP client's editor buffers."""
        if not self.client_supports_fs_read() or self._writer is None:
            return None
        resolved = _normalize_client_path(session.cwd, path)
        if resolved is None:
            return None
        params: dict[str, Any] = {"sessionId": session.session_id, "path": resolved}
        if line is not None:
            params["line"] = line
        if limit is not None:
            params["limit"] = limit
        try:
            resp = await self._request_client(
                "fs/read_text_file",
                params,
                session=session,
                timeout=min(self._permission_timeout_seconds, 10.0),
            )
        except (TimeoutError, asyncio.CancelledError):
            return None
        if "_error" in resp:
            return None
        content = resp.get("content")
        return content if isinstance(content, str) else None

    def _wrapped_tools_factory(self, session: ACPSession) -> Callable[[], ToolRegistry]:
        base_factory = self._tools_factory

        def _factory() -> ToolRegistry:
            if base_factory is not None:
                registry: ToolRegistry = base_factory()
            else:
                from agent_forge.tools import create_default_registry

                registry = create_default_registry()
            if self.client_supports_fs_read() and registry.has("read_file"):
                inner = registry.get("read_file")
                registry.register(
                    _ClientBufferReadFileTool(
                        inner,
                        lambda path: self.read_client_text_file(session, path),
                    ),
                    replace=True,
                )
            return registry

        return _factory

    async def _default_run_executor(
        self,
        session: ACPSession,
        prompt_text: str,
        cfg: ForgeConfig,
        agent_cfg: AgentConfig,
        event_bus: EventBus,
        approval_gate: ApprovalGate,
        cancellation_token: CancellationToken,
    ) -> AgentRun:
        from agent_forge.agent.runner import _resolve_context_engine_for_config, run_agent_task
        from agent_forge.llm.qualification import is_api_key_required

        assert session.dev_session is not None  # noqa: S101
        provider_name = agent_cfg.provider
        api_key = ""
        if self._provider_factory is None:
            provider_cfg = cfg.providers.get(provider_name)
            env_name = provider_cfg.api_key_env if provider_cfg is not None else ""
            base_url = provider_cfg.base_url if provider_cfg is not None else ""
            api_key = os.environ.get(env_name, "") if env_name else ""
            if is_api_key_required(cfg, provider_name, base_url=base_url) and not api_key:
                msg = f"Missing API key environment variable '{env_name}' for '{provider_name}'"
                raise RuntimeError(msg)

        if session.context_engine is None and cfg.context.enabled:
            session.context_engine = _resolve_context_engine_for_config(
                session.cwd,
                prompt_text,
                cfg,
                agent_cfg,
                dev_session=session.dev_session,
            )

        tools_factory = (
            self._wrapped_tools_factory(session)
            if self.client_supports_fs_read()
            else self._tools_factory
        )
        return await run_agent_task(
            prompt_text,
            session.cwd,
            cfg,
            agent_cfg,
            provider_name,
            api_key,
            event_bus=event_bus,
            cancellation_token=cancellation_token,
            approval_gate=approval_gate,
            context_engine=session.context_engine,
            dev_session=session.dev_session,
            sandbox_factory=self._sandbox_factory,
            provider_factory=self._provider_factory,
            tools_factory=tools_factory,
            stream_tokens=True,
        )

    def _default_session_provider(self) -> str:
        return self._provider_override or self._config.agent.default_provider

    def _default_session_model(self) -> str:
        return self._model_override or self._config.agent.default_model

    def _build_session_config(
        self,
        session: ACPSession,
    ) -> tuple[ForgeConfig, AgentConfig]:
        cfg = self._config.model_copy(deep=True)
        if session.mcp_servers:
            cfg.mcp.servers.update(session.mcp_servers)
        if self._sandbox_backend_override:
            cfg.sandbox.backend = self._sandbox_backend_override
        provider = session.provider or self._default_session_provider()
        model = session.model or self._default_session_model()
        approval_mode = session.approval_mode or self._approval_mode
        execution_mode = session.execution_mode or cfg.agent.execution_mode
        max_iter = self._max_iterations_override or cfg.agent.max_iterations
        agent_cfg = AgentConfig(
            max_iterations=max_iter,
            max_tokens_per_run=cfg.agent.max_tokens_per_run,
            model=model,
            provider=provider,
            temperature=cfg.agent.temperature,
            approval_mode=approval_mode,
            execution_mode=execution_mode,
            post_edit_diagnostics=cfg.agent.post_edit_diagnostics,
            max_cost_usd=cfg.agent.max_cost_usd,
            max_duration_seconds=cfg.agent.max_duration_seconds,
            budget_action=cfg.agent.budget_action,
            context_enabled=cfg.context.enabled,
        )
        return cfg, agent_cfg

    def _session_modes_and_config(self, session: ACPSession) -> dict[str, Any]:
        provider = session.provider or self._default_session_provider()
        model = session.model or self._default_session_model()
        approval_mode = session.approval_mode or self._approval_mode
        execution_mode = session.execution_mode or self._config.agent.execution_mode
        mode_id = session.mode_id or _derive_mode_id(approval_mode, execution_mode)
        return {
            "modes": build_session_modes(mode_id),
            "configOptions": build_session_config_options(
                mode_id=mode_id,
                provider=provider,
                model=model,
                approval_mode=approval_mode,
                execution_mode=execution_mode,
                config=self._config,
            ),
        }

    async def _send_json(self, message: dict[str, Any]) -> None:
        writer = self._writer
        if writer is None:
            return
        encoded = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            writer.write(encoded)
            await writer.drain()

    async def _request_client(
        self,
        method: str,
        params: dict[str, Any],
        *,
        session: ACPSession,
        timeout: float,
    ) -> dict[str, Any]:
        self._next_outbound_id += 1
        req_id = self._next_outbound_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_client_requests[req_id] = future
        session.pending_permission_ids.add(req_id)
        try:
            await self._send_json(make_request(req_id, method, params))
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_client_requests.pop(req_id, None)
            session.pending_permission_ids.discard(req_id)

    def _resolve_client_response(self, msg: dict[str, Any]) -> None:
        raw_id = msg.get("id")
        if not isinstance(raw_id, int):
            return
        future = self._pending_client_requests.get(raw_id)
        if future is None or future.done():
            return
        if "error" in msg and msg["error"] is not None:
            future.set_result({"outcome": {"outcome": "cancelled"}, "_error": msg["error"]})
            return
        result = msg.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    async def _handle_initialize(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        self._initialized = True
        caps = params.get("clientCapabilities")
        self._client_capabilities = dict(caps) if isinstance(caps, dict) else {}
        info = params.get("clientInfo")
        self._client_info = dict(info) if isinstance(info, dict) else {}
        version = _clamp_protocol_version(params.get("protocolVersion"))
        await self._send_json(
            make_response(
                req_id,
                {
                    "protocolVersion": version,
                    "agentCapabilities": build_agent_capabilities(),
                    "agentInfo": dict(AGENT_INFO),
                    "authMethods": [dict(item) for item in self._auth_methods],
                },
            )
        )

    async def _handle_authenticate(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        method_id = str(params.get("methodId", "")).strip()
        advertised = {str(item.get("id", "")) for item in self._auth_methods}
        if not method_id or method_id not in advertised:
            await self._send_json(
                make_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    f"Unsupported authentication method: {method_id!r}",
                )
            )
            return
        self._authenticated_method = method_id
        await self._send_json(make_response(req_id, {}))

    def _persist_acp_session(self, session: ACPSession) -> None:
        if session.dev_session is None:
            return
        session.dev_session.repo_path = session.cwd
        session.dev_session.provider = session.provider or self._default_session_provider()
        session.dev_session.model = session.model or self._default_session_model()
        session.dev_session.approval_mode = session.approval_mode or self._approval_mode
        session.dev_session.metadata["acp"] = {
            "allowed_categories": sorted(session.allowed_categories),
            "execution_mode": session.execution_mode,
            "mode_id": session.mode_id,
            "mcp_servers": {
                name: srv.model_dump(mode="json") for name, srv in session.mcp_servers.items()
            },
        }
        self._session_store.save(session.dev_session)

    def _create_acp_session(
        self,
        *,
        session_id: str,
        cwd: str,
        mcp_servers: dict[str, MCPServerSettings],
    ) -> ACPSession:
        provider = self._default_session_provider()
        model = self._default_session_model()
        execution_mode = self._config.agent.execution_mode
        mode_id = _derive_mode_id(self._approval_mode, execution_mode)
        dev_session = DevSession(
            repo_path=cwd,
            id=session_id,
            provider=provider,
            model=model,
            approval_mode=self._approval_mode,
            sandbox_backend=self._sandbox_backend_override or self._config.sandbox.backend,
            sandbox_image=self._config.sandbox.image,
        )
        session = ACPSession(
            session_id=session_id,
            cwd=cwd,
            mcp_servers=mcp_servers,
            provider=provider,
            model=model,
            approval_mode=self._approval_mode,
            execution_mode=execution_mode,
            mode_id=mode_id,
            dev_session=dev_session,
        )
        self._sessions[session_id] = session
        self._persist_acp_session(session)
        return session

    def _restore_acp_session(
        self,
        *,
        session_id: str,
        raw_cwd: str,
        incoming_mcp: dict[str, MCPServerSettings],
    ) -> ACPSession | None:
        dev_session = self._session_store.load(session_id)
        target_cwd = str(Path(raw_cwd).resolve()) if raw_cwd else dev_session.repo_path
        if not target_cwd or not Path(target_cwd).is_dir():
            return None
        acp_meta = dev_session.metadata.get("acp")
        allowed_categories, mcp_servers, execution_mode, mode_id = self._unpack_acp_meta(
            acp_meta,
            dev_session.approval_mode,
        )
        if incoming_mcp:
            mcp_servers.update(incoming_mcp)
        session = ACPSession(
            session_id=session_id,
            cwd=target_cwd,
            mcp_servers=mcp_servers,
            allowed_categories=allowed_categories,
            provider=self._provider_override or dev_session.provider,
            model=self._model_override or dev_session.model,
            approval_mode=dev_session.approval_mode or self._approval_mode,
            execution_mode=execution_mode,
            mode_id=mode_id,
            dev_session=dev_session,
        )
        self._sessions[session_id] = session
        return session

    def _unpack_acp_meta(
        self,
        acp_meta: Any,
        approval_mode: str,
    ) -> tuple[set[str], dict[str, MCPServerSettings], str, str]:
        allowed_categories: set[str] = set()
        mcp_servers: dict[str, MCPServerSettings] = {}
        execution_mode: str = self._config.agent.execution_mode
        mode_id = _derive_mode_id(approval_mode, execution_mode)
        if not isinstance(acp_meta, dict):
            return allowed_categories, mcp_servers, execution_mode, mode_id
        allowed_categories = {
            str(cat) for cat in acp_meta.get("allowed_categories", []) if str(cat)
        }
        raw_saved = acp_meta.get("mcp_servers")
        if isinstance(raw_saved, dict):
            mcp_servers = {
                str(name): MCPServerSettings(**srv)
                for name, srv in raw_saved.items()
                if isinstance(srv, dict)
            }
        raw_exec = str(acp_meta.get("execution_mode", execution_mode))
        if raw_exec in _VALID_EXECUTION_MODES:
            execution_mode = raw_exec
        raw_mode = str(acp_meta.get("mode_id", ""))
        mode_id = (
            raw_mode if raw_mode in ACP_MODE_IDS else _derive_mode_id(approval_mode, execution_mode)
        )
        return allowed_categories, mcp_servers, execution_mode, mode_id

    async def _send_available_commands(self, session_id: str) -> None:
        if self._initialized:
            await self._send_json(
                make_notification("session/update", build_available_commands_update(session_id))
            )

    async def _handle_session_new(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        raw_cwd = str(params.get("cwd", "")).strip()
        if not raw_cwd or not Path(raw_cwd).is_dir():
            await self._send_json(
                make_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    f"Session 'cwd' must be an existing directory: {raw_cwd!r}",
                )
            )
            return
        resolved_cwd = str(Path(raw_cwd).resolve())
        session_id = f"sess_{uuid.uuid4().hex[:16]}"
        mcp_servers = translate_acp_mcp_servers(params.get("mcpServers"))
        session = self._create_acp_session(
            session_id=session_id,
            cwd=resolved_cwd,
            mcp_servers=mcp_servers,
        )
        await self._send_json(
            make_response(
                req_id,
                {"sessionId": session_id, **self._session_modes_and_config(session)},
            )
        )
        await self._send_available_commands(session_id)

    async def _resolve_session_for_load(
        self,
        req_id: int | str | None,
        session_id: str,
        raw_cwd: str,
        incoming_mcp: dict[str, MCPServerSettings],
    ) -> ACPSession | None:
        session = self._sessions.get(session_id)
        if session is not None:
            if raw_cwd:
                session.cwd = str(Path(raw_cwd).resolve())
            if incoming_mcp:
                session.mcp_servers.update(incoming_mcp)
            return session
        if self._session_store.exists(session_id):
            restored = self._restore_acp_session(
                session_id=session_id,
                raw_cwd=raw_cwd,
                incoming_mcp=incoming_mcp,
            )
            if restored is None:
                await self._send_json(
                    make_error(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        "Persisted session directory no longer exists",
                    )
                )
            return restored
        if not raw_cwd:
            await self._send_json(
                make_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    "session/load requires existing 'cwd' when session is not stored on disk",
                )
            )
            return None
        return self._create_acp_session(
            session_id=session_id,
            cwd=str(Path(raw_cwd).resolve()),
            mcp_servers=incoming_mcp,
        )

    async def _handle_session_load(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        raw_id = str(params.get("sessionId", "")).strip()
        try:
            session_id = validate_session_id(raw_id)
        except ValueError:
            await self._send_json(
                make_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    f"Invalid 'sessionId' for session/load: {raw_id!r}",
                )
            )
            return

        raw_cwd = str(params.get("cwd", "")).strip()
        if raw_cwd and not Path(raw_cwd).is_dir():
            await self._send_json(
                make_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    f"Session 'cwd' must be an existing directory: {raw_cwd!r}",
                )
            )
            return

        incoming_mcp = translate_acp_mcp_servers(params.get("mcpServers"))
        session = await self._resolve_session_for_load(req_id, session_id, raw_cwd, incoming_mcp)
        if session is None:
            return

        assert session.dev_session is not None  # noqa: S101
        for payload in build_replay_updates(session.dev_session):
            await self._send_json(make_notification("session/update", payload))
        await self._send_json(make_response(req_id, self._session_modes_and_config(session)))
        await self._send_available_commands(session_id)

    async def _handle_session_list(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        raw_cursor = params.get("cursor")
        offset = 0
        if raw_cursor is not None:
            try:
                offset = int(str(raw_cursor))
                if offset < 0:
                    raise ValueError
            except ValueError:
                await self._send_json(
                    make_error(req_id, JSONRPC_INVALID_PARAMS, f"Invalid cursor: {raw_cursor!r}")
                )
                return

        raw_cwd = str(params.get("cwd", "") or "").strip()
        cwd_filter = Path(raw_cwd).resolve() if raw_cwd else None
        summaries = self._session_store.list_sessions(repo_path=cwd_filter)
        page = summaries[offset : offset + _LIST_PAGE_SIZE]
        result: dict[str, Any] = {"sessions": [_map_session_summary(item) for item in page]}
        if offset + _LIST_PAGE_SIZE < len(summaries):
            result["nextCursor"] = str(offset + _LIST_PAGE_SIZE)
        await self._send_json(make_response(req_id, result))

    def _set_session_provider(self, session: ACPSession, value: str) -> str | None:
        if value not in self._config.providers:
            return f"Unknown provider: {value!r}"
        session.provider = value
        prov_default = self._config.providers[value].default_model
        if prov_default:
            session.model = prov_default
        return None

    def _set_session_mode_value(self, session: ACPSession, value: str) -> str | None:
        if value not in ACP_MODE_IDS:
            return f"Unsupported mode: {value!r}"
        _apply_mode_selection(session, value)
        return None

    def _set_session_approval(self, session: ACPSession, value: str) -> str | None:
        if value not in _VALID_APPROVAL_MODES:
            return f"Unsupported approval_mode: {value!r}"
        session.approval_mode = value
        session.mode_id = _derive_mode_id(session.approval_mode, session.execution_mode)
        return None

    def _set_session_execution(self, session: ACPSession, value: str) -> str | None:
        if value not in _VALID_EXECUTION_MODES:
            return f"Unsupported execution_mode: {value!r}"
        session.execution_mode = value
        session.mode_id = _derive_mode_id(session.approval_mode, session.execution_mode)
        return None

    def _apply_config_change(self, session: ACPSession, config_id: str, value: str) -> str | None:
        if not value:
            return f"Configuration value for '{config_id}' must not be empty"
        if config_id == "model":
            session.model = value
            return None
        handlers: dict[str, Callable[[ACPSession, str], str | None]] = {
            "mode": self._set_session_mode_value,
            "provider": self._set_session_provider,
            "approval_mode": self._set_session_approval,
            "execution_mode": self._set_session_execution,
        }
        handler = handlers.get(config_id)
        if handler is None:
            return f"Unknown configId: {config_id!r}"
        return handler(session, value)

    async def _handle_session_set_config_option(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        session_id = str(params.get("sessionId", "")).strip()
        session = self._sessions.get(session_id)
        if session is None:
            await self._send_json(
                make_error(req_id, JSONRPC_INVALID_PARAMS, f"Unknown sessionId: {session_id!r}")
            )
            return
        config_id = str(params.get("configId", "")).strip()
        value = str(params.get("value", "")).strip()
        prev_mode = session.mode_id
        error_msg = self._apply_config_change(session, config_id, value)
        if error_msg is not None:
            await self._send_json(make_error(req_id, JSONRPC_INVALID_PARAMS, error_msg))
            return

        self._persist_acp_session(session)
        state = self._session_modes_and_config(session)
        config_options = state["configOptions"]
        await self._send_json(make_response(req_id, {"configOptions": config_options}))
        await self._send_json(
            make_notification(
                "session/update", build_config_option_update(session_id, config_options)
            )
        )
        if session.mode_id != prev_mode:
            await self._send_json(
                make_notification(
                    "session/update", build_current_mode_update(session_id, session.mode_id)
                )
            )

    async def _handle_session_set_mode(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        session_id = str(params.get("sessionId", "")).strip()
        session = self._sessions.get(session_id)
        if session is None:
            await self._send_json(
                make_error(req_id, JSONRPC_INVALID_PARAMS, f"Unknown sessionId: {session_id!r}")
            )
            return
        mode_id = str(params.get("modeId", "")).strip()
        if mode_id not in ACP_MODE_IDS:
            await self._send_json(
                make_error(req_id, JSONRPC_INVALID_PARAMS, f"Unsupported modeId: {mode_id!r}")
            )
            return

        _apply_mode_selection(session, mode_id)
        self._persist_acp_session(session)
        config_options = self._session_modes_and_config(session)["configOptions"]
        await self._send_json(make_response(req_id, {}))
        await self._send_json(
            make_notification("session/update", build_current_mode_update(session_id, mode_id))
        )
        await self._send_json(
            make_notification(
                "session/update", build_config_option_update(session_id, config_options)
            )
        )

    async def _handle_session_cancel(self, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId", "")).strip()
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.cancellation_token is not None:
            session.cancellation_token.cancel("Cancelled by ACP client")
        for req_id in list(session.pending_permission_ids):
            fut = self._pending_client_requests.get(req_id)
            if fut is not None and not fut.done():
                fut.set_result({"outcome": {"outcome": "cancelled"}})

    def _make_permission_gate(self, session: ACPSession) -> ApprovalGate:
        async def _confirm(request: ApprovalRequest) -> bool:
            if request.category in session.allowed_categories:
                return True
            call_id = f"perm_{uuid.uuid4().hex[:12]}"
            params = {
                "sessionId": session.session_id,
                "toolCall": {
                    "toolCallId": call_id,
                    "title": request.tool_name,
                    "kind": classify_tool_kind(request.tool_name),
                    "status": "pending",
                    "rawInput": request.arguments,
                },
                "options": [
                    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                    {
                        "optionId": "allow_always",
                        "name": "Always allow category",
                        "kind": "allow_always",
                    },
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            }
            try:
                resp = await self._request_client(
                    "session/request_permission",
                    params,
                    session=session,
                    timeout=self._permission_timeout_seconds,
                )
            except (TimeoutError, asyncio.CancelledError):
                return False
            approved, remember = _parse_permission_option(resp)
            if approved and remember:
                session.allowed_categories.add(request.category)
            return approved

        return ApprovalGate(mode=session.approval_mode or self._approval_mode, callback=_confirm)

    async def _attach_acp_event_forwarder(
        self,
        session: ACPSession,
        event_bus: EventBus,
    ) -> tuple[list[str], dict[str, bool]]:
        seq_counter = 0
        pending_call_ids: dict[str, list[str]] = {}
        sub_ids: list[str] = []
        stream_state = {"saw_assistant_delta": False}

        async def _forward(event: Event) -> None:
            nonlocal seq_counter
            clean_data, truncated = sanitize_event_data(event.data)
            stream_ev = StreamEvent(
                seq=seq_counter,
                type=event.type.value,
                run_id=event.run_id,
                timestamp=event.timestamp.isoformat(),
                data=clean_data,
                truncated=truncated,
            )
            seq_counter += 1
            if event.type == EventType.ASSISTANT_DELTA and str(clean_data.get("delta", "") or ""):
                stream_state["saw_assistant_delta"] = True
            call_id = self._resolve_tool_call_id(event, seq_counter, pending_call_ids)
            for payload in map_stream_event_to_acp_updates(
                stream_ev,
                session.session_id,
                tool_call_id=call_id,
            ):
                await self._send_json(make_notification("session/update", payload))

        for etype in EventType:
            sub_ids.append(await event_bus.subscribe(etype, _forward))
        return sub_ids, stream_state

    def _resolve_tool_call_id(
        self,
        event: Event,
        seq: int,
        pending_call_ids: dict[str, list[str]],
    ) -> str | None:
        tool_name = str(event.data.get("tool_name", "") or "")
        if event.type == EventType.TOOL_CALLED and tool_name:
            cid = f"call-{seq}"
            pending_call_ids.setdefault(tool_name, []).append(cid)
            return cid
        if event.type == EventType.TOOL_COMPLETED and tool_name:
            stack = pending_call_ids.get(tool_name)
            if stack:
                return stack.pop(0)
            return f"call-{seq}"
        return None

    async def _execute_prompt_turn(
        self,
        req_id: int | str | None,
        session: ACPSession,
        prompt_text: str,
    ) -> None:
        cfg, agent_cfg = self._build_session_config(session)
        event_bus = EventBus()
        sink = JournalEventSink()
        token = CancellationToken()
        session.cancellation_token = token
        gate = self._make_permission_gate(session)
        await sink.attach(event_bus)
        sub_ids, stream_state = await self._attach_acp_event_forwarder(session, event_bus)
        assert session.dev_session is not None  # noqa: S101
        turns_before = len(session.dev_session.turns)
        try:
            run = await self._run_executor(
                session,
                prompt_text,
                cfg,
                agent_cfg,
                event_bus,
                gate,
                token,
            )
            if len(session.dev_session.turns) == turns_before:
                session.dev_session.record_turn(
                    prompt=prompt_text,
                    run=run,
                    context_engine=session.context_engine,
                )
            self._persist_acp_session(session)
            final_text = session.dev_session.turns[-1].response
            if not stream_state["saw_assistant_delta"] and final_text and final_text.strip():
                await self._send_json(
                    make_notification(
                        "session/update",
                        {
                            "sessionId": session.session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": final_text},
                            },
                        },
                    )
                )
            await self._send_json(
                make_response(
                    req_id,
                    {
                        "stopReason": map_run_state_to_stop_reason(run.state),
                        "_meta": {"runId": run.id, "runState": run.state.value},
                    },
                )
            )
        except Exception as exc:
            logger.exception("acp_prompt_failed", session_id=session.session_id, exc_info=exc)
            await self._send_json(make_error(req_id, JSONRPC_INTERNAL_ERROR, str(exc)))
        finally:
            for sid in sub_ids:
                await event_bus.unsubscribe(sid)
            await sink.detach(event_bus)
            session.active_run_task = None
            session.cancellation_token = None

    async def _handle_session_prompt(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        session_id = str(params.get("sessionId", "")).strip()
        session = self._sessions.get(session_id)
        if session is None:
            await self._send_json(
                make_error(req_id, JSONRPC_INVALID_PARAMS, f"Unknown sessionId: {session_id!r}")
            )
            return
        if session.active_run_task is not None and not session.active_run_task.done():
            await self._send_json(
                make_error(req_id, JSONRPC_INVALID_REQUEST, "Session already has an active turn")
            )
            return
        prompt_text = extract_prompt_text(params.get("prompt"))
        if not prompt_text:
            await self._send_json(
                make_error(req_id, JSONRPC_INVALID_PARAMS, "Prompt content must not be empty")
            )
            return
        session.active_run_task = asyncio.create_task(
            self._execute_prompt_turn(req_id, session, prompt_text)
        )

    async def _dispatch_message(self, msg: dict[str, Any]) -> None:
        if "method" not in msg:
            self._resolve_client_response(msg)
            return
        method = str(msg.get("method", ""))
        req_id = msg.get("id")
        raw_params = msg.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}

        handlers: dict[str, Callable[[int | str | None, dict[str, Any]], Awaitable[None]]] = {
            "initialize": self._handle_initialize,
            "authenticate": self._handle_authenticate,
            "session/new": self._handle_session_new,
            "session/load": self._handle_session_load,
            "session/list": self._handle_session_list,
            "session/set_config_option": self._handle_session_set_config_option,
            "session/set_mode": self._handle_session_set_mode,
            "session/prompt": self._handle_session_prompt,
        }
        if method == "session/cancel":
            await self._handle_session_cancel(params)
            return
        handler = handlers.get(method)
        if handler is None:
            if req_id is not None:
                await self._send_json(
                    make_error(req_id, JSONRPC_METHOD_NOT_FOUND, f"Unknown method: {method}")
                )
            return
        await handler(req_id, params)

    async def _shutdown_sessions(self) -> None:
        active_tasks: list[asyncio.Task[None]] = []
        for session in self._sessions.values():
            if session.cancellation_token is not None:
                session.cancellation_token.cancel("ACP server shutting down")
            for req_id in list(session.pending_permission_ids):
                fut = self._pending_client_requests.get(req_id)
                if fut is not None and not fut.done():
                    fut.set_result({"outcome": {"outcome": "cancelled"}})
            if session.active_run_task is not None and not session.active_run_task.done():
                active_tasks.append(session.active_run_task)
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

    async def serve(self, reader: AsyncLineReader, writer: AsyncLineWriter) -> None:
        """Process newline-delimited JSON-RPC 2.0 ACP traffic until EOF."""
        self._writer = writer
        try:
            while True:
                raw_line = await reader.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    await self._send_json(make_error(None, JSONRPC_PARSE_ERROR, str(exc)))
                    continue
                if not isinstance(msg, dict):
                    await self._send_json(
                        make_error(None, JSONRPC_INVALID_REQUEST, "Message must be a JSON object")
                    )
                    continue
                await self._dispatch_message(msg)
        finally:
            await self._shutdown_sessions()
            self._writer = None

    async def serve_stdio(self) -> None:
        """Serve ACP over process ``sys.stdin`` / ``sys.stdout``, routing logs to stderr."""
        reader = _StdioReaderAdapter(sys.stdin.buffer)
        writer = _StdioWriterAdapter(sys.stdout.buffer)
        try:
            with redirect_stdout(sys.stderr):
                await self.serve(reader, writer)
        finally:
            writer.close()
