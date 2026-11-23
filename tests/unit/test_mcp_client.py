"""Unit tests for the external MCP client, transports, policy gates, and circuit breaker."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from agent_forge.agent.approvals import ApprovalGate, ApprovalRequest
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.cli import main
from agent_forge.config import MCPServerSettings, MCPSettings, load_config
from agent_forge.eval.deterministic import SubprocessEvalSandbox
from agent_forge.llm.base import LLMConfig, LLMProvider, LLMResponse, Message, TokenUsage, ToolCall
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.sandbox.base import SandboxConfig
from agent_forge.tools import (
    MCPCircuitBreaker,
    MCPClientManager,
    MCPServerClient,
    MCPTool,
    MCPToolAnnotations,
    MCPToolDescriptor,
    ToolRegistry,
    normalize_mcp_tool_name,
)

_FS_SERVER_SCRIPT = """
import json
import sys
import time
from pathlib import Path

root = Path(sys.argv[1]).resolve()
mode = sys.argv[2] if len(sys.argv) > 2 else "normal"

if mode == "startup-noise":
    print("banner before jsonrpc", flush=True)

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    req = json.loads(line)
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    if req_id is None:
        continue
    if method == "initialize":
        if mode == "hang-init":
            time.sleep(5.0)
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-fs", "version": "1.0.0"},
            },
        }
    elif method == "tools/list":
        cursor = params.get("cursor")
        if cursor is None:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "read_note",
                            "description": "Read a file from the MCP root.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"],
                            },
                            "annotations": {"readOnlyHint": True, "destructiveHint": False},
                        },
                        {
                            "name": "write_note",
                            "description": "Write a file in the MCP root.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "content": {"type": "string"},
                                },
                                "required": ["path", "content"],
                            },
                            "annotations": {"readOnlyHint": False, "destructiveHint": True},
                        },
                    ],
                    "nextCursor": "page-2",
                },
            }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "slow_op",
                            "description": "Slow or failing tool for isolation tests.",
                            "inputSchema": {"type": "object", "properties": {}},
                            "annotations": {"readOnlyHint": True},
                        },
                        {
                            "name": "blocked_op",
                            "description": "Filtered out by deny_tools.",
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                    ],
                },
            }
    elif method == "tools/call":
        tname = params.get("name")
        args = params.get("arguments") or {}
        if tname == "read_note":
            target = (root / args["path"]).resolve()
            if not target.is_file():
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": f"Missing {args['path']}"}],
                    },
                }
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": target.read_text(encoding="utf-8")}],
                    },
                }
        elif tname == "write_note":
            target = (root / args["path"]).resolve()
            target.write_text(args["content"], encoding="utf-8")
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"wrote {args['path']}"}],
                },
            }
        elif tname == "slow_op":
            time.sleep(float(args.get("sleep", 2.0)))
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "slow done"}]},
            }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool {tname}"},
            }
    else:
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method {method}"},
        }
    print(json.dumps(resp), flush=True)
"""


class _ScriptedLLM(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._idx = 0

    async def complete(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del messages, tools, config
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp

    async def stream(self, messages: list[Message], **kwargs: Any) -> Any:
        del kwargs
        yield await self.complete(messages)


def _write_server_script(tmp_path: Path) -> Path:
    script = tmp_path / "mcp_test_server.py"
    script.write_text(_FS_SERVER_SCRIPT, encoding="utf-8")
    return script


class TestMCPStdioAndHTTP:
    """Stdio and HTTP transports, pagination, filtering, and truncation."""

    @pytest.mark.asyncio
    async def test_stdio_transport_paginated_discovery_and_execution(self, tmp_path: Path) -> None:
        script = _write_server_script(tmp_path)
        fs_root = tmp_path / "fs"
        fs_root.mkdir()
        (fs_root / "note.txt").write_text("hello mcp", encoding="utf-8")

        settings = MCPSettings(
            servers={
                "local-fs": MCPServerSettings(
                    transport="stdio",
                    command=sys.executable,
                    args=[str(script), str(fs_root), "startup-noise"],
                    deny_tools=["blocked_op"],
                    max_output_chars=512,
                )
            }
        )
        manager = MCPClientManager.from_settings(settings)
        async with manager:
            statuses = manager.statuses()
            assert len(statuses) == 1
            assert statuses[0].connected is True
            assert statuses[0].tool_count == 3

            registry = ToolRegistry()
            registered = manager.register_tools(registry)
            assert registered == [
                "mcp__local-fs__read_note",
                "mcp__local-fs__write_note",
                "mcp__local-fs__slow_op",
            ]

            sandbox = SubprocessEvalSandbox()
            await sandbox.start(str(fs_root), SandboxConfig(backend="subprocess-eval"))
            try:
                read_tool = registry.get("mcp__local-fs__read_note")
                res = await read_tool.execute({"path": "note.txt"}, sandbox)
                assert res.exit_code == 0
                assert res.output == "hello mcp"

                err_res = await read_tool.execute({"path": "missing.txt"}, sandbox)
                assert err_res.exit_code == 1
                assert "Missing missing.txt" in (err_res.error or "")

                write_tool = registry.get("mcp__local-fs__write_note")
                big = "x" * 700
                wres = await write_tool.execute({"path": "out.txt", "content": big}, sandbox)
                assert wres.exit_code == 0
                assert (fs_root / "out.txt").read_text(encoding="utf-8") == big
            finally:
                await sandbox.stop()

    @pytest.mark.asyncio
    async def test_http_transport_json_sse_session_and_auth_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TEST_MCP_TOKEN", "secret-token")
        seen_headers: list[dict[str, str]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(dict(request.headers))
            if request.method == "DELETE":
                return httpx.Response(204)
            body = json.loads(request.content)
            method = body.get("method")
            req_id = body.get("id")
            if method == "initialize":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    },
                    headers={"MCP-Session-Id": "sess-42"},
                )
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "tools": [
                                {
                                    "name": "ping",
                                    "description": "Ping remote server",
                                    "inputSchema": {"type": "object", "properties": {}},
                                    "annotations": {"readOnlyHint": True},
                                }
                            ]
                        },
                    },
                )
            sse = (
                "event: message\n"
                + f'data: {{"jsonrpc": "2.0", "id": {req_id}, '
                + '"result": {"content": [{"type": "text", "text": "pong-sse"}]}}\n\n'
            )
            return httpx.Response(
                200,
                content=sse.encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )

        settings = MCPSettings(
            servers={
                "remote": MCPServerSettings(
                    transport="http",
                    url="https://mcp.example.com/rpc",
                    auth_env="TEST_MCP_TOKEN",
                )
            }
        )
        with respx.mock(assert_all_called=False) as router:
            router.route(url="https://mcp.example.com/rpc").mock(side_effect=_handler)
            manager = MCPClientManager.from_settings(settings)
            async with manager:
                registry = ToolRegistry()
                registered = manager.register_tools(registry)
                assert registered == ["mcp__remote__ping"]
                res = await registry.get("mcp__remote__ping").execute({}, SubprocessEvalSandbox())
                assert res.exit_code == 0
                assert res.output == "pong-sse"

        assert seen_headers[0]["authorization"] == "Bearer secret-token"
        assert seen_headers[-2]["mcp-session-id"] == "sess-42"
        assert any(req_h.get("mcp-session-id") == "sess-42" for req_h in seen_headers)


class TestMCPFailureIsolation:
    """Timeouts, broken startup isolation, and circuit breaker state transitions."""

    def test_circuit_breaker_transitions_and_recovery(self) -> None:
        now = 100.0
        breaker = MCPCircuitBreaker(
            failure_threshold=2,
            recovery_timeout_seconds=10.0,
            clock=lambda: now,
        )
        assert breaker.state == "closed"
        assert breaker.allow_request() is True
        breaker.record_failure()
        assert breaker.state == "closed"
        breaker.record_failure()
        assert breaker.state == "open"
        assert breaker.allow_request() is False
        assert breaker.retry_after_seconds == 10.0

        now += 11.0
        assert breaker.state == "half_open"
        assert breaker.allow_request() is True
        breaker.record_success()
        assert breaker.state == "closed"
        assert breaker.retry_after_seconds == 0.0

    @pytest.mark.asyncio
    async def test_hanging_server_times_out_and_trips_circuit_while_healthy_server_runs(
        self,
        tmp_path: Path,
    ) -> None:
        script = _write_server_script(tmp_path)
        fs_root = tmp_path / "fs"
        fs_root.mkdir()
        (fs_root / "ok.txt").write_text("alive", encoding="utf-8")

        settings = MCPSettings(
            servers={
                "healthy": MCPServerSettings(
                    transport="stdio",
                    command=sys.executable,
                    args=[str(script), str(fs_root), "normal"],
                    timeout_seconds=0.3,
                    failure_threshold=2,
                    recovery_timeout_seconds=60.0,
                ),
                "broken-startup": MCPServerSettings(
                    transport="stdio",
                    command=sys.executable,
                    args=[str(script), str(fs_root), "hang-init"],
                    startup_timeout_seconds=0.2,
                ),
            }
        )
        manager = MCPClientManager.from_settings(settings)
        async with manager:
            statuses = {st.name: st for st in manager.statuses()}
            assert statuses["healthy"].connected is True
            assert statuses["broken-startup"].connected is False
            assert statuses["broken-startup"].error is not None

            registry = ToolRegistry()
            manager.register_tools(registry)
            slow_tool = registry.get("mcp__healthy__slow_op")
            r1 = await slow_tool.execute({"sleep": 5.0}, SubprocessEvalSandbox())
            r2 = await slow_tool.execute({"sleep": 5.0}, SubprocessEvalSandbox())
            r3 = await slow_tool.execute({"sleep": 0.01}, SubprocessEvalSandbox())

            assert r1.exit_code == 1
            assert r2.exit_code == 1
            assert r3.exit_code == 1
            assert r3.metadata.get("mcp_circuit_open") is True

            ok_res = await registry.get("mcp__healthy__read_note").execute(
                {"path": "ok.txt"}, SubprocessEvalSandbox()
            )
            assert ok_res.exit_code == 1
            assert ok_res.metadata.get("mcp_circuit_open") is True


class TestMCPApprovalIntegration:
    """MCP tools are routed through ApprovalGate with no blanket trust."""

    def test_mcp_classification_no_blanket_trust_and_strictest_mode(self) -> None:
        descriptor_ro = MCPToolDescriptor(
            server_name="fs",
            remote_name="read_note",
            registered_name=normalize_mcp_tool_name("fs", "read_note"),
            description="Read note",
            input_schema={"type": "object", "properties": {}},
            annotations=MCPToolAnnotations(read_only=True, destructive=False),
        )
        client_default = MCPServerClient(
            "fs",
            MCPServerSettings(command="echo", allow_read_only_without_approval=False),
        )
        tool_default = MCPTool(descriptor_ro, client_default)
        assert tool_default.side_effecting is True

        client_opt_in = MCPServerClient(
            "fs",
            MCPServerSettings(command="echo", allow_read_only_without_approval=True),
        )
        tool_opt_in = MCPTool(descriptor_ro, client_opt_in)
        assert tool_opt_in.side_effecting is False

    @pytest.mark.asyncio
    async def test_react_loop_enforces_approval_gate_on_mcp_tools(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path / "home")
        monkeypatch.setattr("agent_forge.agent.core.save_summary", lambda *_a: tmp_path / "s.json")
        script = _write_server_script(tmp_path)
        fs_root = tmp_path / "fs"
        fs_root.mkdir()
        (fs_root / "target.txt").write_text("before", encoding="utf-8")

        settings = MCPSettings(
            servers={
                "fs": MCPServerSettings(
                    transport="stdio",
                    command=sys.executable,
                    args=[str(script), str(fs_root)],
                    approval_mode="prompt",
                    allow_read_only_without_approval=True,
                )
            }
        )
        decisions: list[ApprovalRequest] = []

        async def _approve_except_delete(req: ApprovalRequest) -> bool:
            decisions.append(req)
            return "write" not in req.tool_name

        events: list[Event] = []
        bus = EventBus()

        async def _capture(event: Event) -> None:
            events.append(event)

        await bus.subscribe(EventType.APPROVAL_DECIDED, _capture)

        manager = MCPClientManager.from_settings(settings)
        async with manager:
            registry = ToolRegistry()
            manager.register_tools(registry)
            sandbox = SubprocessEvalSandbox()
            await sandbox.start(str(fs_root), SandboxConfig(backend="subprocess-eval"))
            try:
                llm = _ScriptedLLM(
                    [
                        LLMResponse(
                            content=None,
                            tool_calls=[
                                ToolCall(
                                    id="c1",
                                    name="mcp__fs__read_note",
                                    arguments={"path": "target.txt"},
                                )
                            ],
                            finish_reason="tool_calls",
                            usage=TokenUsage(10, 5, 15),
                        ),
                        LLMResponse(
                            content=None,
                            tool_calls=[
                                ToolCall(
                                    id="c2",
                                    name="mcp__fs__write_note",
                                    arguments={"path": "target.txt", "content": "mutated"},
                                )
                            ],
                            finish_reason="tool_calls",
                            usage=TokenUsage(10, 5, 15),
                        ),
                        LLMResponse(
                            content="Done.",
                            tool_calls=[],
                            finish_reason="stop",
                            usage=TokenUsage(10, 5, 15),
                        ),
                    ]
                )
                run = AgentRun(
                    task="Read target and attempt write",
                    repo_path=str(fs_root),
                    config=AgentConfig(max_iterations=5, approval_mode="auto"),
                )
                await react_loop(
                    run,
                    llm,
                    registry,
                    sandbox,
                    event_bus=bus,
                    approval_gate=ApprovalGate(mode="auto", callback=_approve_except_delete),
                )
            finally:
                await sandbox.stop()

        assert run.state == RunState.COMPLETED
        assert (fs_root / "target.txt").read_text(encoding="utf-8") == "before"
        assert len(decisions) == 1
        assert decisions[0].category == "mcp_destructive"
        assert any(e.data.get("approved") is False for e in events)


class TestMCPConfigAndCLI:
    """agent-forge.toml server discovery and ``agent-forge mcp`` commands."""

    def test_load_toml_and_cli_mcp_list_and_inspect(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        script = _write_server_script(tmp_path)
        fs_root = tmp_path / "fs"
        fs_root.mkdir()
        toml_path = tmp_path / "agent-forge.toml"
        toml_path.write_text(
            f'[mcp.servers.files]\ntransport = "stdio"\n'
            f'command = "{sys.executable}"\n'
            f'args = ["{script}", "{fs_root}"]\n'
            'allow_tools = ["read_note"]\n',
            encoding="utf-8",
        )
        cfg = load_config(project_path=toml_path, user_path=tmp_path / "none.toml")
        assert "files" in cfg.mcp.servers
        assert cfg.mcp.servers["files"].allow_tools == ["read_note"]

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        list_res = runner.invoke(main, ["mcp", "list"])
        assert list_res.exit_code == 0, list_res.output
        assert "files" in list_res.output

        inspect_res = runner.invoke(main, ["mcp", "inspect", "--output-format", "json"])
        assert inspect_res.exit_code == 0, inspect_res.output
        data = json.loads(inspect_res.output)
        assert data["mcp_enabled"] is True
        assert data["servers"][0]["connected"] is True
        assert [t["name"] for t in data["servers"][0]["tools"]] == ["mcp__files__read_note"]

        monkeypatch.setenv("AGENT_FORGE_MCP_ENABLED", "false")
        cfg_disabled = load_config(project_path=toml_path, user_path=tmp_path / "none.toml")
        assert cfg_disabled.mcp.enabled is False
