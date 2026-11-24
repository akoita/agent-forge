"""Unit tests for declarative tool approval gates and domain-allowlisted egress proxy."""

from __future__ import annotations

import asyncio
import base64
import secrets
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path
from click.testing import CliRunner

from agent_forge.agent.approvals import (
    ApprovalGate,
    ApprovalRequest,
    classify_destructive_tool_call,
)
from agent_forge.agent.core import _can_execute_batch_in_parallel, react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.runner import build_sandbox_config
from agent_forge.agent.verification import VerificationWorkspace
from agent_forge.cli import main
from agent_forge.config import ForgeConfig, SandboxSettings, load_config
from agent_forge.llm.base import LLMResponse, TokenUsage, ToolCall
from agent_forge.llm.errors import SandboxStartupError
from agent_forge.orchestration.events import EventBus, EventType
from agent_forge.sandbox import (
    EgressProxyServer,
    SandboxConfig,
    is_domain_allowed,
    validate_egress_domain,
)
from agent_forge.sandbox.base import ExecResult
from agent_forge.sandbox.bwrap import BwrapSandbox
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.sandbox.egress_proxy import _safe_open_connection
from agent_forge.tools.base import Tool, ToolRegistry, ToolResult


class _PlainReadTool(Tool):
    @property
    def name(self) -> str:
        return "plain_read"

    @property
    def description(self) -> str:
        return "Read helper without approval"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"target": {"type": "string"}}}

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Any) -> ToolResult:
        del arguments, sandbox
        return ToolResult(output="plain ok")


class _CloudDeployTool(Tool):
    def __init__(self, *, side_effecting: bool = True) -> None:
        self._side_effecting = side_effecting
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "cloud_deploy"

    @property
    def description(self) -> str:
        return "Deploy service artifact to cloud target"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "region": {"type": "string"},
            },
            "required": ["project"],
        }

    @property
    def side_effecting(self) -> bool:
        return self._side_effecting

    @property
    def requires_approval(self) -> bool:
        return True

    @property
    def approval_category(self) -> str:
        return "cloud_deploy"

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        project = arguments.get("project", "<unknown>")
        region = arguments.get("region", "us-central1")
        return f"Deploy release to project '{project}' in region '{region}'."

    async def execute(self, arguments: dict[str, Any], sandbox: Any) -> ToolResult:
        del sandbox
        self.calls.append(dict(arguments))
        return ToolResult(output=f"deployed:{arguments.get('project')}")


class TestDeclarativeToolApprovals:
    """Declarative approval properties on Tool ABC and ApprovalGate / react_loop integration."""

    @pytest.mark.asyncio
    async def test_tool_defaults_and_custom_declarative_classification(self) -> None:
        plain = _PlainReadTool()
        assert plain.requires_approval is False
        assert plain.approval_category == "tool_action"
        assert "arguments: target" in plain.approval_summary({"target": "x"})
        assert classify_destructive_tool_call("plain_read", {"target": "x"}, tool=plain) is None

        deploy = _CloudDeployTool()
        req = classify_destructive_tool_call(
            "cloud_deploy", {"project": "prod-1", "region": "europe-west1"}, tool=deploy
        )
        assert req is not None
        assert req.category == "cloud_deploy"
        assert req.reason == "Deploy release to project 'prod-1' in region 'europe-west1'."

        prompts: list[ApprovalRequest] = []

        async def _approve(r: ApprovalRequest) -> bool:
            prompts.append(r)
            return True

        gate = ApprovalGate(mode="prompt", callback=_approve)
        res_req, decision = await gate.evaluate("cloud_deploy", {"project": "prod-1"}, tool=deploy)
        assert res_req is not None and len(prompts) == 1
        assert decision is not None and decision.approved is True

        deny_req, deny_dec = await ApprovalGate(mode="prompt", callback=None).evaluate(
            "cloud_deploy", {"project": "prod-1"}, tool=deploy
        )
        assert deny_req is not None
        assert deny_dec is not None and deny_dec.approved is False

    def test_requires_approval_blocks_parallel_batch_even_when_read_only(self) -> None:
        registry = ToolRegistry()
        registry.register(_PlainReadTool())
        registry.register(_CloudDeployTool(side_effecting=False))

        indexed = [
            (0, ToolCall(id="c1", name="plain_read", arguments={"target": "a"})),
            (1, ToolCall(id="c2", name="cloud_deploy", arguments={"project": "p"})),
        ]
        pending = [(0, indexed[0][1], "k1"), (1, indexed[1][1], "k2")]
        assert (
            _can_execute_batch_in_parallel(
                indexed,
                pending,
                registry,
                allow_parallel_tools=True,
                approval_gate=None,
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_react_loop_gates_declarative_tool_and_emits_custom_category(
        self, tmp_path: Path
    ) -> None:
        deploy_tool = _CloudDeployTool()
        registry = ToolRegistry()
        registry.register(deploy_tool)

        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_dep_1",
                        name="cloud_deploy",
                        arguments={"project": "demo-app", "region": "us-east1"},
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Blocked",
                tool_calls=[],
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            ),
        ]

        sandbox = AsyncMock()
        bus = EventBus()
        events: list[tuple[EventType, dict[str, Any]]] = []

        async def _capture(evt: Any) -> None:
            events.append((evt.type, dict(evt.data)))

        for et in (EventType.APPROVAL_REQUESTED, EventType.APPROVAL_DECIDED):
            await bus.subscribe(et, _capture)

        async def _deny_cb(_req: ApprovalRequest) -> bool:
            return False

        run_denied = AgentRun(
            task="Deploy release",
            repo_path=str(tmp_path),
            config=AgentConfig(
                max_iterations=4,
                context_enabled=False,
            ),
        )
        res_denied = await react_loop(
            run_denied,
            llm,
            registry,
            sandbox,
            event_bus=bus,
            approval_gate=ApprovalGate(mode="prompt", callback=_deny_cb),
        )
        assert res_denied.state == RunState.COMPLETED
        assert deploy_tool.calls == []
        requested_evts = [data for et, data in events if et == EventType.APPROVAL_REQUESTED]
        assert requested_evts[0]["category"] == "cloud_deploy"
        assert "demo-app" in requested_evts[0]["reason"]

        # Approved run executes tool
        llm2 = AsyncMock()
        llm2.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_dep_2",
                        name="cloud_deploy",
                        arguments={"project": "demo-app", "region": "us-east1"},
                    )
                ],
                usage=TokenUsage(10, 5, 15),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Deployed",
                tool_calls=[],
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            ),
        ]

        async def _allow_cb(_req: ApprovalRequest) -> bool:
            return True

        run_ok = AgentRun(
            task="Deploy release",
            repo_path=str(tmp_path),
            config=AgentConfig(
                max_iterations=4,
                context_enabled=False,
            ),
        )
        res_ok = await react_loop(
            run_ok,
            llm2,
            registry,
            sandbox,
            approval_gate=ApprovalGate(mode="prompt", callback=_allow_cb),
        )
        assert res_ok.state == RunState.COMPLETED
        assert deploy_tool.calls == [{"project": "demo-app", "region": "us-east1"}]


class TestEgressDomainValidationAndProxyServer:
    """Domain validation, wildcard matching, auth, HTTP forwarding, CONNECT, and SSRF protection."""

    def test_validate_and_match_egress_domains(self) -> None:
        assert validate_egress_domain("PyPI.org.") == "pypi.org"
        assert validate_egress_domain("*.GoogleAPIs.com") == "*.googleapis.com"

        for bad in (
            "",
            "*",
            "*.com",
            "localhost",
            "127.0.0.1",
            "169.254.169.254",
            "::1",
            "https://pypi.org",
            "pypi.org:443",
            "pypi.org/simple",
            "bad domain.com",
            "*pypi.org",
            "internal",
        ):
            with pytest.raises(ValueError, match=r"."):
                validate_egress_domain(bad)

        rules = ["pypi.org", "*.googleapis.com"]
        assert is_domain_allowed("pypi.org", rules) is True
        assert is_domain_allowed("storage.googleapis.com", rules) is True
        assert is_domain_allowed("googleapis.com", rules) is False
        assert is_domain_allowed("evil-googleapis.com", rules) is False
        assert is_domain_allowed("localhost", rules) is False
        assert is_domain_allowed("169.254.169.254", rules) is False

    @pytest.mark.asyncio
    async def test_proxy_forwards_allowed_http_and_blocks_disallowed_or_unauth(self) -> None:
        async def _handle_upstream(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(_handle_upstream, host="127.0.0.1", port=0)
        up_port = int(upstream.sockets[0].getsockname()[1])

        async def _connector(
            _host: str, _port: int
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            return await asyncio.open_connection("127.0.0.1", up_port)

        token_value = secrets.token_hex(8)
        proxy = EgressProxyServer(
            ["pypi.org"],
            auth_token=token_value,
            connector=_connector,
        )
        await proxy.start()
        try:
            assert proxy.bound_port is not None
            # Missing auth -> 407
            r1, w1 = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
            w1.write(b"GET http://pypi.org/simple HTTP/1.1\r\nHost: pypi.org\r\n\r\n")
            await w1.drain()
            resp1 = await r1.read()
            w1.close()
            assert b"407 Proxy Authentication Required" in resp1

            auth_b64 = base64.b64encode(f"af:{token_value}".encode()).decode()
            # Allowed domain -> 200 OK
            r2, w2 = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
            w2.write(
                (
                    "GET http://pypi.org/simple HTTP/1.1\r\n"
                    "Host: pypi.org\r\n"
                    f"Proxy-Authorization: Basic {auth_b64}\r\n\r\n"
                ).encode()
            )
            await w2.drain()
            resp2 = await r2.read()
            w2.close()
            assert b"200 OK" in resp2 and resp2.endswith(b"ok")

            # Blocked domain -> 403 Forbidden
            r3, w3 = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
            w3.write(
                (
                    "GET http://evil.example.com/secret HTTP/1.1\r\n"
                    "Host: evil.example.com\r\n"
                    f"Proxy-Authorization: Basic {auth_b64}\r\n\r\n"
                ).encode()
            )
            await w3.drain()
            resp3 = await r3.read()
            w3.close()
            assert b"403 Forbidden" in resp3
            assert "pypi.org" in proxy.allowed_requests
            assert "evil.example.com" in proxy.blocked_requests
        finally:
            await proxy.stop()
            upstream.close()
            await upstream.wait_closed()

    @pytest.mark.asyncio
    async def test_proxy_connect_tunnel_and_ssrf_block(self) -> None:
        async def _echo_upstream(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            data = await reader.read(1024)
            writer.write(b"ECHO:" + data)
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(_echo_upstream, host="127.0.0.1", port=0)
        up_port = int(upstream.sockets[0].getsockname()[1])

        async def _connector(
            _host: str, _port: int
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            return await asyncio.open_connection("127.0.0.1", up_port)

        proxy = EgressProxyServer(["*.googleapis.com"], connector=_connector)
        await proxy.start()
        try:
            assert proxy.bound_port is not None
            r1, w1 = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
            w1.write(
                b"CONNECT storage.googleapis.com:443 HTTP/1.1\r\n"
                b"Host: storage.googleapis.com:443\r\n\r\n"
            )
            await w1.drain()
            head = await r1.readuntil(b"\r\n\r\n")
            assert b"200 Connection Established" in head
            w1.write(b"hello-tls")
            await w1.drain()
            w1.write_eof()
            echoed = await r1.read()
            w1.close()
            assert echoed == b"ECHO:hello-tls"

            # SSRF defense: private/loopback DNS resolution rejected by _safe_open_connection
            fake_loop = MagicMock()
            fake_loop.getaddrinfo = AsyncMock(
                return_value=[(2, 1, 6, "", ("169.254.169.254", 443))]
            )
            with (
                patch(
                    "agent_forge.sandbox.egress_proxy.asyncio.get_running_loop",
                    return_value=fake_loop,
                ),
                pytest.raises(PermissionError, match="non-public"),
            ):
                await _safe_open_connection("storage.googleapis.com", 443)
        finally:
            await proxy.stop()
            upstream.close()
            await upstream.wait_closed()


class TestProxyConfigCliDockerBwrapAndVerification:
    """Config/CLI wiring and Docker/bwrap/VerificationWorkspace egress proxy enforcement."""

    def test_config_cli_and_runner_wire_allowed_egress_domains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "AGENT_FORGE_SANDBOX_ALLOWED_EGRESS_DOMAINS", "pypi.org,*.googleapis.com"
        )
        cfg = load_config(project_path=tmp_path / "no.toml", user_path=tmp_path / "no-u.toml")
        assert cfg.sandbox.allowed_egress_domains == ["pypi.org", "*.googleapis.com"]

        sandbox_cfg = build_sandbox_config(cfg, repo_path=tmp_path)
        assert sandbox_cfg.allowed_egress_domains == ["pypi.org", "*.googleapis.com"]

        with pytest.raises(ValueError, match="Invalid egress domain"):
            ForgeConfig(sandbox=SandboxSettings(allowed_egress_domains=["https://pypi.org"]))

        fake_cfg = MagicMock()
        fake_cfg.agent.default_provider = "gemini"
        fake_cfg.providers = {"gemini": MagicMock(api_key_env="GEMINI_API_KEY")}
        with (
            patch("agent_forge.cli.load_config", return_value=fake_cfg) as mock_load,
            patch("agent_forge.cli._run_agent"),
            patch("agent_forge.cli._emit_run_output"),
        ):
            res = CliRunner().invoke(
                main,
                [
                    "run",
                    "--task",
                    "Deploy",
                    "--repo",
                    str(tmp_path),
                    "--allow-domain",
                    "pypi.org",
                    "--allow-domain",
                    "*.googleapis.com",
                ],
                env={"GEMINI_API_KEY": "test-key"},
            )
        assert res.exit_code == 0, res.output
        mock_load.assert_called_once_with(
            cli_overrides={
                "sandbox.allowed_egress_domains": ["pypi.org", "*.googleapis.com"],
            }
        )

    @pytest.mark.asyncio
    async def test_docker_and_bwrap_start_and_stop_egress_proxy(self, tmp_path: Path) -> None:
        container = MagicMock()
        container.id = "proxy-c1"
        container.short_id = "proxyc1"
        client = MagicMock()
        client.containers.run.return_value = container

        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = client
            ds = DockerSandbox()
            await ds.start(
                str(tmp_path),
                SandboxConfig(network_enabled=False, allowed_egress_domains=["pypi.org"]),
            )
            assert ds._egress_proxy is not None
            port = ds._egress_proxy.bound_port
            assert port is not None
            kwargs = client.containers.run.call_args.kwargs
            assert "network_mode" not in kwargs
            assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}
            assert f"host.docker.internal:{port}" in kwargs["environment"]["HTTPS_PROXY"]
            await ds.stop()
            assert ds._egress_proxy is None

        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            bs = BwrapSandbox()
            with pytest.raises(SandboxStartupError, match="localhost"):
                await bs.start(
                    str(tmp_path),
                    SandboxConfig(backend="bwrap", allowed_egress_domains=["localhost"]),
                )

            await bs.start(
                str(tmp_path),
                SandboxConfig(
                    backend="bwrap",
                    network_enabled=False,
                    allowed_egress_domains=["pypi.org"],
                ),
            )
            assert bs._egress_proxy is not None
            bwrap_port = bs._egress_proxy.bound_port
            argv = bs._build_bwrap_argv("echo ok")
            assert "--unshare-net" not in argv
            assert any(f"127.0.0.1:{bwrap_port}" in item for item in argv)
            await bs.stop()
            assert bs._egress_proxy is None

    @pytest.mark.asyncio
    async def test_verification_workspace_clears_allowed_egress_domains(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        captured: list[SandboxConfig] = []
        verifier = AsyncMock()
        verifier.start.side_effect = lambda _path, cfg: captured.append(cfg)
        verifier.exec.return_value = ExecResult(0, "ok", "")
        candidate = AsyncMock()

        with VerificationWorkspace(str(tmp_path), "pytest -q") as ws:
            (ws.workspace / "app.py").write_text("x = 2\n", encoding="utf-8")
            result = await ws.verify(
                SandboxConfig(allowed_egress_domains=["pypi.org"]),
                candidate,
                sandbox_factory=lambda _c: verifier,
            )

        assert result.passed is True
        assert captured[0].allowed_egress_domains == []
        assert captured[0].network_enabled is False
