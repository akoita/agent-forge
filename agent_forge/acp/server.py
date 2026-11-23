"""Bidirectional JSON-RPC 2.0 ACP server exposing Agent Forge to editors."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from agent_forge.acp.protocol import (
    ACP_PROTOCOL_VERSION,
    AGENT_INFO,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
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
from agent_forge.config import ForgeConfig, MCPServerSettings, load_config
from agent_forge.llm.base import Role
from agent_forge.observability import get_logger
from agent_forge.orchestration.event_stream import (
    JournalEventSink,
    StreamEvent,
    sanitize_event_data,
)
from agent_forge.orchestration.events import Event, EventBus, EventType

logger = get_logger("acp")


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
    active_run_task: asyncio.Task[None] | None = None
    cancellation_token: CancellationToken | None = None
    pending_permission_ids: set[int] = field(default_factory=set)


RunExecutor = Callable[
    [ACPSession, str, ForgeConfig, AgentConfig, EventBus, ApprovalGate, CancellationToken],
    Awaitable[AgentRun],
]


def _parse_permission_option(
    response_payload: dict[str, Any],
) -> tuple[bool, bool]:
    """Return ``(approved, remember_always)`` from ``session/request_permission``."""
    outcome = response_payload.get("outcome")
    if not isinstance(outcome, dict):
        return False, False
    if outcome.get("outcome") != "selected":
        return False, False
    option_id = str(outcome.get("optionId", "")).strip()
    if option_id == "allow_always":
        return True, True
    if option_id == "allow_once":
        return True, False
    return False, False


def _extract_last_assistant_message(run: AgentRun) -> str:
    for msg in reversed(run.messages):
        if msg.role == Role.ASSISTANT and msg.content and msg.content.strip():
            return msg.content
    return ""


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
        run_executor: RunExecutor | None = None,
        sandbox_factory: Any = None,
        provider_factory: Any = None,
        tools_factory: Any = None,
    ) -> None:
        self._config = config or load_config()
        self._approval_mode = approval_mode
        self._provider_override = provider_override
        self._model_override = model_override
        self._sandbox_backend_override = sandbox_backend_override
        self._max_iterations_override = max_iterations_override
        self._permission_timeout_seconds = permission_timeout_seconds
        self._sandbox_factory = sandbox_factory
        self._provider_factory = provider_factory
        self._tools_factory = tools_factory
        self._run_executor = run_executor or self._default_run_executor
        self._sessions: dict[str, ACPSession] = {}
        self._pending_client_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_outbound_id = 0
        self._write_lock = asyncio.Lock()
        self._writer: AsyncLineWriter | None = None
        self._initialized = False

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
        from agent_forge.agent.runner import run_agent_task
        from agent_forge.llm.qualification import is_api_key_required

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
            sandbox_factory=self._sandbox_factory,
            provider_factory=self._provider_factory,
            tools_factory=self._tools_factory,
        )

    def _build_session_config(
        self,
        session: ACPSession,
    ) -> tuple[ForgeConfig, AgentConfig]:
        cfg = self._config.model_copy(deep=True)
        if session.mcp_servers:
            cfg.mcp.servers.update(session.mcp_servers)
        if self._sandbox_backend_override:
            cfg.sandbox.backend = self._sandbox_backend_override
        provider = self._provider_override or cfg.agent.default_provider
        model = self._model_override or cfg.agent.default_model
        max_iter = self._max_iterations_override or cfg.agent.max_iterations
        agent_cfg = AgentConfig(
            max_iterations=max_iter,
            max_tokens_per_run=cfg.agent.max_tokens_per_run,
            model=model,
            provider=provider,
            temperature=cfg.agent.temperature,
            approval_mode=self._approval_mode,
            max_cost_usd=cfg.agent.max_cost_usd,
            max_duration_seconds=cfg.agent.max_duration_seconds,
            budget_action=cfg.agent.budget_action,
            context_enabled=cfg.context.enabled,
        )
        return cfg, agent_cfg

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
            future.set_result({"outcome": {"outcome": "cancelled"}})
            return
        result = msg.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    async def _handle_initialize(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        del params
        self._initialized = True
        await self._send_json(
            make_response(
                req_id,
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "agentCapabilities": {"loadSession": True},
                    "agentInfo": dict(AGENT_INFO),
                },
            )
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
        self._sessions[session_id] = ACPSession(
            session_id=session_id,
            cwd=resolved_cwd,
            mcp_servers=mcp_servers,
        )
        await self._send_json(make_response(req_id, {"sessionId": session_id}))

    async def _handle_session_load(
        self,
        req_id: int | str | None,
        params: dict[str, Any],
    ) -> None:
        session_id = str(params.get("sessionId", "")).strip()
        raw_cwd = str(params.get("cwd", "")).strip()
        if not session_id or not raw_cwd or not Path(raw_cwd).is_dir():
            await self._send_json(
                make_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    "session/load requires valid 'sessionId' and existing 'cwd'",
                )
            )
            return
        existing = self._sessions.get(session_id)
        if existing is None:
            self._sessions[session_id] = ACPSession(
                session_id=session_id,
                cwd=str(Path(raw_cwd).resolve()),
                mcp_servers=translate_acp_mcp_servers(params.get("mcpServers")),
            )
        else:
            existing.cwd = str(Path(raw_cwd).resolve())
        await self._send_json(make_response(req_id, {}))

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

        return ApprovalGate(mode=self._approval_mode, callback=_confirm)

    async def _attach_acp_event_forwarder(
        self,
        session: ACPSession,
        event_bus: EventBus,
    ) -> list[str]:
        seq_counter = 0
        pending_call_ids: dict[str, list[str]] = {}
        sub_ids: list[str] = []

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
            call_id = self._resolve_tool_call_id(event, seq_counter, pending_call_ids)
            for payload in map_stream_event_to_acp_updates(
                stream_ev,
                session.session_id,
                tool_call_id=call_id,
            ):
                await self._send_json(make_notification("session/update", payload))

        for etype in EventType:
            sub_ids.append(await event_bus.subscribe(etype, _forward))
        return sub_ids

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
        sub_ids = await self._attach_acp_event_forwarder(session, event_bus)
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
            final_text = _extract_last_assistant_message(run)
            if final_text:
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
            "session/new": self._handle_session_new,
            "session/load": self._handle_session_load,
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
