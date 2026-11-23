"""Unit tests for Issue #46 — generic OpenAI-compatible endpoints and graceful degradation."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest
import respx
from click.testing import CliRunner

from agent_forge.agent.core import react_loop
from agent_forge.agent.journal import RunJournal
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.runner import run_agent_task
from agent_forge.cli import _create_configured_provider, main
from agent_forge.config import ForgeConfig, ModelProfileSettings, ProviderSettings, load_config
from agent_forge.eval.benchmark import _RecordingLLM
from agent_forge.llm import is_local_endpoint
from agent_forge.llm.base import LLMConfig, Message, Role, ToolDefinition
from agent_forge.llm.capabilities import resolve_capabilities
from agent_forge.llm.errors import LLMResponseError
from agent_forge.llm.factory import create_provider, provider_names
from agent_forge.llm.openai import OpenAIProvider
from agent_forge.llm.qualification import probe_endpoint_capabilities, probe_model
from agent_forge.llm.router import RoutingLLMProvider
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools.base import Tool, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers & Minimal Fakes
# ---------------------------------------------------------------------------


class _DummySandbox(Sandbox):
    async def start(self, repo_path: str, config: Any) -> None:
        del repo_path, config

    async def exec(
        self,
        command: str,
        timeout: int | None = None,
        workdir: str | None = None,
    ) -> ExecResult:
        del command, timeout, workdir
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str) -> str:
        del path
        return ""

    async def write_file(self, path: str, content: str) -> None:
        del path, content

    async def list_files(self, path: str = "/workspace") -> list[str]:
        del path
        return []

    async def stop(self) -> None:
        return None

    async def is_alive(self) -> bool:
        return True


class _ProbeReadTool(Tool):
    def __init__(self, *, delay: float = 0.02) -> None:
        self._delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def name(self) -> str:
        return "probe_read"

    @property
    def description(self) -> str:
        return "Read item"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"item": {"type": "string"}}}

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        del sandbox
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self._delay > 0:
                await asyncio.sleep(self._delay)
        finally:
            self.in_flight -= 1
        return ToolResult(output=f"read:{arguments.get('item', '')}", exit_code=0)


# ---------------------------------------------------------------------------
# 1. Factory, Config & Local Endpoint Detection
# ---------------------------------------------------------------------------


class TestFactoryAndConfig:
    def test_factory_registers_openai_compatible(self) -> None:
        assert "openai-compatible" in provider_names()
        provider = create_provider("openai-compatible", base_url="http://localhost:11434/v1/")
        assert isinstance(provider, OpenAIProvider)
        assert provider.api_mode == "chat_completions"
        caps = provider.capabilities("gemma3:12b")
        assert caps.api_protocol == "openai_chat_completions"
        assert caps.tool_calling is True
        assert caps.structured_output is True
        assert caps.parallel_tool_calls is False
        assert caps.prompt_caching is False
        assert caps.stateful_response_ids is False

    def test_load_config_preserves_builtin_providers_and_env_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_toml = tmp_path / "agent-forge.toml"
        project_toml.write_text(
            '[providers.openai-compatible]\nbase_url = "http://localhost:8000/v1"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "AGENT_FORGE_PROVIDERS_OPENAI_COMPATIBLE_DEFAULT_MODEL", "Qwen/Qwen3-Coder"
        )
        monkeypatch.setenv("AGENT_FORGE_PROVIDERS_OPENAI_COMPATIBLE_API_KEY_REQUIRED", "false")

        cfg = load_config(project_path=project_toml, user_path=tmp_path / "none.toml")
        assert "gemini" in cfg.providers
        assert "openai" in cfg.providers
        assert "anthropic" in cfg.providers
        compat = cfg.providers["openai-compatible"]
        assert compat.base_url == "http://localhost:8000/v1"
        assert compat.default_model == "Qwen/Qwen3-Coder"
        assert compat.api_key_required is False

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://localhost:11434/v1", True),
            ("http://127.0.0.1:8080/v1", True),
            ("http://[::1]:8000/v1", True),
            ("http://host.docker.internal:11434/v1", True),
            ("http://ollama.localhost:11434/v1", True),
            ("https://api.openai.com/v1", False),
            ("https://api.together.xyz/v1", False),
            ("", False),
        ],
    )
    def test_is_local_endpoint(self, url: str, expected: bool) -> None:
        assert is_local_endpoint(url) is expected


# ---------------------------------------------------------------------------
# 2. Keyless Auth, Wire Hardening & ConnectError Translation
# ---------------------------------------------------------------------------


class TestKeylessAndWireHardening:
    @respx.mock
    @pytest.mark.asyncio
    async def test_keyless_omits_auth_and_hardens_null_usage_missing_id_and_reasoning(self) -> None:
        route = respx.post("http://localhost:11434/v1/chat/completions").respond(
            200,
            json={
                "model": "gemma3:12b",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "Let me inspect the health file first.",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": ""},
                                }
                            ],
                        },
                    }
                ],
                "usage": None,
            },
        )

        provider = OpenAIProvider("", base_url="http://localhost:11434/v1/")
        resp = await provider.complete(
            [Message(role=Role.USER, content="Inspect")],
            config=LLMConfig(model="gemma3:12b", max_retries=0),
        )
        await provider.close()

        assert route.call_count == 1
        headers = {k.lower(): v for k, v in respx.calls[0].request.headers.items()}
        assert "authorization" not in headers
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id.startswith("call_")
        assert resp.tool_calls[0].name == "read_file"
        assert resp.tool_calls[0].arguments == {}
        assert resp.usage.total_tokens == 0
        assert resp.reasoning_blocks == [
            {"type": "reasoning_content", "text": "Let me inspect the health file first."}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_connect_error_translates_to_llm_response_error(self) -> None:
        respx.post("http://localhost:11434/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        provider = OpenAIProvider("", base_url="http://localhost:11434/v1")
        with pytest.raises(
            LLMResponseError, match="Could not connect to OpenAI-compatible endpoint"
        ):
            await provider.complete(
                [Message(role=Role.USER, content="hi")],
                config=LLMConfig(model="gemma3:12b", max_retries=0),
            )
        await provider.close()


# ---------------------------------------------------------------------------
# 3. Graceful Degradation When Capabilities Are Absent
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    @respx.mock
    @pytest.mark.asyncio
    async def test_parallel_tool_calls_omitted_and_sequential_in_react_loop(
        self, tmp_path: Path
    ) -> None:
        route = respx.post("http://localhost:8000/v1/chat/completions")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {
                                            "name": "probe_read",
                                            "arguments": '{"item":"a"}',
                                        },
                                    },
                                    {
                                        "id": "c2",
                                        "type": "function",
                                        "function": {
                                            "name": "probe_read",
                                            "arguments": '{"item":"b"}',
                                        },
                                    },
                                ],
                            },
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}
                    ]
                },
            ),
        ]

        provider = create_provider(
            "openai-compatible",
            base_url="http://localhost:8000/v1",
            capability_overrides={"parallel_tool_calls": False},
        )
        tool = _ProbeReadTool(delay=0.03)
        registry = ToolRegistry()
        registry.register(tool)
        run = AgentRun(
            task="Read two items",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=4, context_enabled=False),
        )
        journal = RunJournal(run.id, path=tmp_path / "journal.jsonl", fsync=False)
        await react_loop(run, provider, registry, _DummySandbox(), journal=journal)
        await provider.close()

        assert run.state == RunState.COMPLETED
        assert tool.max_in_flight == 1
        sent = json.loads(respx.calls[0].request.content)
        assert "parallel_tool_calls" not in sent

    @respx.mock
    @pytest.mark.asyncio
    async def test_parallel_tool_calls_sent_when_overridden_true(self) -> None:
        respx.post("http://localhost:8000/v1/chat/completions").respond(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]},
        )
        provider = create_provider(
            "openai-compatible",
            base_url="http://localhost:8000/v1",
            capability_overrides={"parallel_tool_calls": True},
        )
        await provider.complete(
            [Message(role=Role.USER, content="hi")],
            tools=[ToolDefinition(name="probe_read", description="Read", parameters={})],
            config=LLMConfig(model="vllm-model", parallel_tool_calls=True),
        )
        await provider.close()
        sent = json.loads(respx.calls[0].request.content)
        assert sent["parallel_tool_calls"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_structured_output_fallback_omits_response_format_and_injects_prompt(
        self,
    ) -> None:
        respx.post("http://localhost:11434/v1/chat/completions").respond(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]},
        )
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        provider = OpenAIProvider(
            "",
            base_url="http://localhost:11434/v1",
            capability_overrides={"structured_output": False},
            provider_name="openai-compatible",
        )
        await provider.complete(
            [Message(role=Role.USER, content="Return JSON")],
            config=LLMConfig(model="gemma3:12b", response_schema=schema),
        )
        await provider.close()

        sent = json.loads(respx.calls[0].request.content)
        assert "response_format" not in sent
        assert sent["messages"][0]["role"] == "system"
        assert "conforming strictly to this schema" in sent["messages"][0]["content"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_calling_false_uses_prompt_emulation_in_react_loop(
        self, tmp_path: Path
    ) -> None:
        route = respx.post("http://localhost:11434/v1/chat/completions")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": (
                                    "I will read the item.\n```tool_call\n"
                                    '{"name": "probe_read", "arguments": {"item": "cfg"}}\n```'
                                ),
                            },
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "Finished reading."},
                        }
                    ]
                },
            ),
        ]

        provider = create_provider(
            "openai-compatible",
            base_url="http://localhost:11434/v1",
            capability_overrides={"tool_calling": False},
        )
        registry = ToolRegistry()
        registry.register(_ProbeReadTool(delay=0.0))
        run = AgentRun(
            task="Inspect config",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=4, context_enabled=False),
        )
        journal = RunJournal(run.id, path=tmp_path / "journal.jsonl", fsync=False)
        await react_loop(run, provider, registry, _DummySandbox(), journal=journal)
        await provider.close()

        assert run.state == RunState.COMPLETED
        assert len(run.tool_invocations) == 1
        assert run.tool_invocations[0].result.output == "read:cfg"
        first_body = json.loads(respx.calls[0].request.content)
        assert "tools" not in first_body
        assert "tool_choice" not in first_body
        assert "Tool Invocation Format" in first_body["messages"][0]["content"]
        second_body = json.loads(respx.calls[1].request.content)
        assert any("[Tool Result" in str(m.get("content", "")) for m in second_body["messages"])


# ---------------------------------------------------------------------------
# 4. Capability Overrides Propagation Across Wrappers
# ---------------------------------------------------------------------------


class TestCapabilityPropagation:
    @pytest.mark.asyncio
    async def test_overrides_flow_through_recording_cli_router_runner_and_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inner = create_provider(
            "openai-compatible",
            base_url="http://localhost:11434/v1",
            capability_overrides={"parallel_tool_calls": True, "tool_calling": False},
        )
        recording = _RecordingLLM(inner)
        assert recording.capabilities("gemma3:12b").parallel_tool_calls is True
        assert recording.capabilities("gemma3:12b").tool_calling is False
        await inner.close()

        cfg = ForgeConfig(
            agent={"default_provider": "openai-compatible", "default_model": "gemma3:12b"},
            providers={
                **ForgeConfig().providers,
                "openai-compatible": ProviderSettings(
                    base_url="http://localhost:11434/v1",
                    api_mode="chat_completions",
                    api_key_required=False,
                    capabilities={"parallel_tool_calls": True},
                ),
            },
            model_profiles={
                "local": ModelProfileSettings(
                    provider="openai-compatible",
                    model="gemma3:12b",
                    base_url="http://localhost:11434/v1",
                    capabilities={"structured_output": False},
                )
            },
        )
        cli_provider = _create_configured_provider(cfg, "openai-compatible", "")
        assert cli_provider.capabilities("gemma3:12b").parallel_tool_calls is True
        await cli_provider.close()

        router = RoutingLLMProvider.from_config(cfg)
        assert router.capabilities("local").parallel_tool_calls is True
        assert router.capabilities("local").structured_output is False
        await router.close()

        monkeypatch.setattr("agent_forge.agent.runner.execute_agent_run", _fake_execute_agent_run)
        created = await run_agent_task(
            "Task",
            str(tmp_path),
            cfg,
            AgentConfig(model="gemma3:12b", provider="openai-compatible"),
            "openai-compatible",
            "",
            tools_factory=ToolRegistry,
            sandbox_factory=lambda _c: _DummySandbox(),
        )
        assert created.state == RunState.PENDING


async def _fake_execute_agent_run(
    agent_run: AgentRun,
    sandbox_config: SandboxConfig,
    llm: Any,
    tools: ToolRegistry,
    sandbox: Sandbox,
    **kwargs: Any,
) -> AgentRun:
    del sandbox_config, tools, sandbox, kwargs
    assert llm.capabilities("gemma3:12b").parallel_tool_calls is True
    await llm.close()
    return agent_run


# ---------------------------------------------------------------------------
# 5. Live Capability Probing & Keyless CLI/Qualification Flows
# ---------------------------------------------------------------------------


class TestProbingAndCLI:
    @respx.mock
    @pytest.mark.asyncio
    async def test_probe_endpoint_capabilities_downgrades_and_respects_overrides(self) -> None:
        respx.get("http://localhost:11434/v1/models").respond(
            200, json={"data": [{"id": "gemma3:12b"}]}
        )
        route = respx.post("http://localhost:11434/v1/chat/completions")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {"name": "read_probe_value", "arguments": "{}"},
                                    }
                                ]
                            },
                        }
                    ]
                },
            ),
            httpx.Response(400, json={"error": {"message": "json_schema unsupported"}}),
            httpx.Response(400, json={"error": {"message": "parallel_tool_calls unsupported"}}),
        ]

        cfg = ForgeConfig()
        profile = ModelProfileSettings(
            provider="openai-compatible",
            model="gemma3:12b",
            base_url="http://localhost:11434/v1",
            capabilities={"structured_output": True},
        )
        res = await probe_endpoint_capabilities(cfg, profile)
        assert res.reachable is True
        assert res.model_listed is True
        assert res.checks == {
            "tool_calling": "supported",
            "structured_output": "unsupported",
            "parallel_tool_calls": "unsupported",
        }
        assert res.capabilities.tool_calling is True
        assert res.capabilities.parallel_tool_calls is False
        # Explicit config override takes precedence over probe result
        assert res.capabilities.structured_output is True

    @respx.mock
    def test_cli_models_capabilities_probe_and_keyless_run_and_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "agent-forge.toml").write_text(
            '[model_profiles.local-ollama]\nprovider = "openai-compatible"\n'
            'model = "gemma3:12b"\nbase_url = "http://localhost:11434/v1"\n\n'
            '[model_profiles.remote-gateway]\nprovider = "openai-compatible"\n'
            'model = "openrouter/auto"\nbase_url = "https://openrouter.ai/api/v1"\n'
            'api_key_env = "OPENROUTER_API_KEY"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        respx.get("http://localhost:11434/v1/models").respond(
            200, json={"data": [{"id": "gemma3:12b"}]}
        )
        chat_route = respx.post("http://localhost:11434/v1/chat/completions")
        chat_route.side_effect = [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "p1",
                                        "type": "function",
                                        "function": {"name": "read_probe_value", "arguments": "{}"},
                                    }
                                ]
                            },
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "p2",
                                        "type": "function",
                                        "function": {"name": "read_probe_value", "arguments": "{}"},
                                    }
                                ]
                            },
                        }
                    ]
                },
            ),
        ]

        runner = CliRunner()
        probe_cli = runner.invoke(
            main, ["models", "capabilities", "local-ollama", "--probe", "--output-format", "json"]
        )
        assert probe_cli.exit_code == 0, probe_cli.output
        parsed = json.loads(probe_cli.stdout)
        assert parsed["local-ollama"]["probe"]["reachable"] is True
        assert parsed["local-ollama"]["probe"]["model_listed"] is True
        assert parsed["local-ollama"]["parallel_tool_calls"] is True

        # Remote gateway still enforces its configured API key env
        bad_remote = runner.invoke(
            main, ["run", "--task", "Fix", "--repo", ".", "--model-profile", "remote-gateway"]
        )
        assert bad_remote.exit_code != 0

        # Missing base_url for openai-compatible exits non-zero cleanly
        bad_missing_url = runner.invoke(
            main,
            [
                "run",
                "--task",
                "Fix",
                "--repo",
                ".",
                "--provider",
                "openai-compatible",
                "--model",
                "x",
            ],
        )
        assert bad_missing_url.exit_code != 0

        # Keyless CLI run via --provider + --base-url AND via --model-profile succeeds
        with (
            patch("agent_forge.cli._run_agent") as mock_run,
            patch("agent_forge.cli._emit_run_output"),
        ):
            r1 = runner.invoke(
                main,
                [
                    "run",
                    "--task",
                    "Fix",
                    "--repo",
                    ".",
                    "--provider",
                    "openai-compatible",
                    "--base-url",
                    "http://localhost:11434/v1",
                    "--model",
                    "gemma3:12b",
                ],
            )
            assert r1.exit_code == 0, r1.output
            assert mock_run.call_args.args[3] == "openai-compatible"
            assert mock_run.call_args.args[2].providers["openai-compatible"].base_url == (
                "http://localhost:11434/v1"
            )

            r2 = runner.invoke(
                main, ["run", "--task", "Fix", "--repo", ".", "--model-profile", "local-ollama"]
            )
            assert r2.exit_code == 0, r2.output
            assert mock_run.call_args.args[2].agent.default_model == "gemma3:12b"

    @respx.mock
    @pytest.mark.asyncio
    async def test_probe_model_keyless_localhost_and_resolve_capabilities_alias(self) -> None:
        route = respx.post("http://localhost:11434/v1/chat/completions")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "k1",
                                        "type": "function",
                                        "function": {"name": "read_probe_value", "arguments": "{}"},
                                    }
                                ]
                            },
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "forge-probe-7391"}}
                    ]
                },
            ),
        ]
        res = await probe_model(
            ForgeConfig(),
            ModelProfileSettings(
                provider="openai-compatible",
                model="gemma3:12b",
                base_url="http://localhost:11434/v1",
            ),
        )
        assert res.status == "passed"
        caps = resolve_capabilities("openai-compatible", "gemma3:12b")
        assert caps.parallel_tool_calls is False
        assert caps.api_protocol == "openai_chat_completions"
