"""Unit tests for M3 Provider API v2: capabilities, adapters, routing, and ReAct adaptation."""

from __future__ import annotations

import asyncio
import json
import warnings
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from click.testing import CliRunner

from agent_forge.agent.context import ContextEngine, ContextPolicy
from agent_forge.agent.core import react_loop
from agent_forge.agent.journal import RunJournal
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.persistence import load_run, save_run
from agent_forge.cli import main
from agent_forge.config import ForgeConfig, ModelProfileSettings, load_config
from agent_forge.llm.anthropic import AnthropicProvider
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    Role,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_forge.llm.capabilities import (
    ProviderCapabilities,
    query_provider_capabilities,
    resolve_capabilities,
)
from agent_forge.llm.gemini import GeminiProvider
from agent_forge.llm.openai import OpenAIProvider
from agent_forge.llm.router import (
    ModelCandidate,
    RoutingLLMProvider,
    RoutingPolicy,
    TurnRoutingContext,
    select_route,
)
from agent_forge.observability.cost import _estimate_cost
from agent_forge.sandbox.base import ExecResult, Sandbox
from agent_forge.tools.base import Tool, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Lightweight Fakes (No Docker, No Network)
# ---------------------------------------------------------------------------


class _MinimalLLM(LLMProvider):
    """Minimal LLMProvider subclass without overriding capabilities()."""

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del messages, tools, config
        return LLMResponse(content="ok", finish_reason="stop")

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        del messages, tools, config
        if False:
            yield LLMResponse(content=None, finish_reason="stop")


class _ScriptedCapableLLM(LLMProvider):
    """Scripted provider recording configs and returning predetermined responses."""

    def __init__(
        self,
        responses: list[LLMResponse],
        caps: ProviderCapabilities,
    ) -> None:
        self._responses = list(responses)
        self._caps = caps
        self.configs: list[LLMConfig] = []

    def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        del model
        return self._caps

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del messages, tools
        self.configs.append(replace(config) if config is not None else LLMConfig())
        return self._responses.pop(0)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        del messages, tools, config
        if False:
            yield LLMResponse(content=None, finish_reason="stop")


class _ConcurrencyProbeTool(Tool):
    """Tool measuring max concurrent in-flight executions."""

    def __init__(
        self,
        tool_name: str,
        *,
        is_side_effecting: bool,
        delay: float = 0.03,
        exit_code: int = 0,
    ) -> None:
        self._name = tool_name
        self._side_effecting = is_side_effecting
        self._delay = delay
        self._exit_code = exit_code
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Concurrency probe tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"item": {"type": "string"}}}

    @property
    def side_effecting(self) -> bool:
        return self._side_effecting

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        del sandbox
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
        finally:
            self.in_flight -= 1
        output = f"item={arguments.get('item', '')}"
        return ToolResult(
            output=output if self._exit_code == 0 else "",
            error=None if self._exit_code == 0 else "boom",
            exit_code=self._exit_code,
        )


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


# ---------------------------------------------------------------------------
# 1. Capability Model Tests
# ---------------------------------------------------------------------------


class TestCapabilityModel:
    def test_openai_responses_and_chat_completions_capabilities(self) -> None:
        default_openai = OpenAIProvider(api_key="k").capabilities("gpt-5.4")
        assert default_openai.api_protocol == "openai_responses"
        assert default_openai.stateful_response_ids is True
        assert default_openai.parallel_tool_calls is True
        assert default_openai.reasoning_continuity is True
        assert default_openai.prompt_caching is True
        assert default_openai.max_context_tokens == 200_000

        chat_openai = OpenAIProvider(api_key="k", api_mode="chat_completions").capabilities(
            "gpt-5.4"
        )
        assert chat_openai.api_protocol == "openai_chat_completions"
        assert chat_openai.stateful_response_ids is False
        assert chat_openai.reasoning_continuity is False

        custom_proxy = OpenAIProvider(
            api_key="k", base_url="https://api.together.xyz/v1"
        ).capabilities("llama")
        assert custom_proxy.api_protocol == "openai_chat_completions"
        assert custom_proxy.parallel_tool_calls is False
        assert custom_proxy.prompt_caching is False
        assert custom_proxy.stateful_response_ids is False

    def test_anthropic_gemini_and_default_capabilities(self) -> None:
        anthropic_caps = AnthropicProvider(api_key="k").capabilities("claude-sonnet-4-6")
        assert anthropic_caps.api_protocol == "anthropic_messages"
        assert anthropic_caps.context_editing is True
        assert anthropic_caps.stateful_response_ids is False
        assert anthropic_caps.max_context_tokens == 200_000

        claude5_caps = resolve_capabilities("anthropic", "claude-sonnet-5-5")
        assert claude5_caps.max_context_tokens == 1_000_000
        assert resolve_capabilities("anthropic", "claude-fable-5-2").max_context_tokens == 1_000_000

        gpt6_caps = resolve_capabilities("openai", "gpt-6.2-sol")
        assert gpt6_caps.max_context_tokens == 1_050_000
        assert gpt6_caps.stateful_response_ids is True

        gemini_caps = GeminiProvider(api_key="k").capabilities("gemini-4-hydrogen")
        assert gemini_caps.api_protocol == "gemini_generate_content"
        assert gemini_caps.max_context_tokens == 1_000_000
        assert gemini_caps.reasoning_continuity is True

        default_caps = query_provider_capabilities(_MinimalLLM())
        assert default_caps.parallel_tool_calls is False
        assert default_caps.stateful_response_ids is False

    def test_resolve_capabilities_overrides_and_ignores_unknown_keys(self) -> None:
        caps = resolve_capabilities(
            "openai",
            "gpt-5.4",
            overrides={
                "parallel_tool_calls": False,
                "max_context_tokens": 64_000,
                "nonexistent_flag": True,
            },
        )
        assert caps.parallel_tool_calls is False
        assert caps.max_context_tokens == 64_000
        assert caps.stateful_response_ids is True

    def test_query_capabilities_ignores_async_mock_magic_attrs(self) -> None:
        mock_llm = AsyncMock()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            caps = query_provider_capabilities(mock_llm, "gpt-5.4")
        assert caps == ProviderCapabilities()
        assert not mock_llm.method_calls
        assert not caught


# ---------------------------------------------------------------------------
# 2. Anthropic & Gemini v2 Adapters
# ---------------------------------------------------------------------------


class TestAnthropicAndGeminiV2:
    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_caching_parallel_tool_choice_and_thinking(self) -> None:
        route = respx.post("https://api.anthropic.com/v1/messages")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "model": "claude-sonnet-4-6",
                    "stop_reason": "end_turn",
                    "content": [
                        {"type": "thinking", "thinking": "step 1", "signature": "sig_1"},
                        {"type": "redacted_thinking", "data": "enc_1"},
                        {"type": "text", "text": "Answer"},
                    ],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 80,
                    },
                },
            ),
            httpx.Response(
                200,
                json={
                    "model": "claude-sonnet-4-6",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "Second turn"}],
                    "usage": {"input_tokens": 50, "output_tokens": 5},
                },
            ),
        ]

        provider = AnthropicProvider(api_key="ant-key")
        tools = [ToolDefinition(name="read_file", description="Read", parameters={})]
        messages = [
            Message(role=Role.SYSTEM, content="Be brief"),
            Message(role=Role.USER, content="Inspect foo.py"),
        ]
        cfg = LLMConfig(
            model="claude-sonnet-4-6",
            cache_key="run-1",
            parallel_tool_calls=False,
            thinking_budget_tokens=2048,
        )
        first = await provider.complete(messages, tools=tools, config=cfg)

        first_body = json.loads(respx.calls[0].request.content)
        assert first_body["system"] == [
            {"type": "text", "text": "Be brief", "cache_control": {"type": "ephemeral"}}
        ]
        assert first_body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert first_body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
        assert first_body["thinking"] == {"type": "enabled", "budget_tokens": 2048}

        assert first.usage.cached_prompt_tokens == 80
        assert len(first.reasoning_blocks) == 2
        assert first.content == "Answer"

        # Verify without cache_key system stays a plain string, and thinking blocks roundtrip
        messages.extend(
            [
                Message(
                    role=Role.ASSISTANT,
                    content="Answer",
                    reasoning_blocks=first.reasoning_blocks,
                ),
                Message(role=Role.USER, content="Follow up"),
            ]
        )
        await provider.complete(messages, tools=tools, config=LLMConfig(model="claude-sonnet-4-6"))
        second_body = json.loads(respx.calls[1].request.content)
        assert second_body["system"] == "Be brief"
        assistant_content = second_body["messages"][1]["content"]
        assert assistant_content[0]["type"] == "thinking"
        assert assistant_content[1]["type"] == "redacted_thinking"

    @respx.mock
    @pytest.mark.asyncio
    async def test_gemini_schema_thinking_and_cached_usage(self) -> None:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent"
        respx.post(url).respond(
            200,
            json={
                "modelVersion": "gemini-3.1-flash-lite-preview",
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "chain"},
                                {"text": '{"ok": true}'},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 300,
                    "candidatesTokenCount": 40,
                    "totalTokenCount": 340,
                    "cachedContentTokenCount": 200,
                    "thoughtsTokenCount": 25,
                },
            },
        )

        provider = GeminiProvider(api_key="gem-key")
        resp = await provider.complete(
            [Message(role=Role.USER, content="Return JSON")],
            config=LLMConfig(
                model="gemini-3.1-flash-lite-preview",
                response_schema={"type": "OBJECT"},
                thinking_budget_tokens=1024,
            ),
        )

        body = json.loads(respx.calls[0].request.content)
        gen_cfg = body["generationConfig"]
        assert gen_cfg["responseMimeType"] == "application/json"
        assert gen_cfg["responseSchema"] == {"type": "OBJECT"}
        assert gen_cfg["thinkingConfig"] == {"thinkingBudget": 1024}

        assert resp.content == '{"ok": true}'
        assert resp.reasoning_blocks == [{"thought": True, "text": "chain"}]
        assert resp.usage.cached_prompt_tokens == 200
        assert resp.usage.reasoning_tokens == 25


# ---------------------------------------------------------------------------
# 3. Cost Discount for Cached Prompt Tokens
# ---------------------------------------------------------------------------


def test_estimate_cost_discounts_cached_prompt_tokens() -> None:
    from agent_forge.observability.cost import COST_TABLE

    uncached = TokenUsage(prompt_tokens=1000, completion_tokens=100, total_tokens=1100)
    cached = TokenUsage(
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        cached_prompt_tokens=800,
    )
    uncached_usd = _estimate_cost(uncached, "gpt-5.4")
    cached_usd = _estimate_cost(cached, "gpt-5.4")
    assert uncached_usd > 0.0
    assert cached_usd < uncached_usd
    cached_rate = COST_TABLE["gpt-5.4"].get("cache_read_input_token_cost", 2.5e-6 * 0.5)
    assert cached_usd == pytest.approx(200 * 2.5e-6 + 800 * cached_rate + 100 * 1.5e-5)


# ---------------------------------------------------------------------------
# 4. ReAct Loop Runtime Adaptation (Parallel Read-Only & Stateful Response IDs)
# ---------------------------------------------------------------------------


class TestReactLoopCapabilityAdaptation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("parallel_cap", "second_side_effecting", "expected_max_in_flight"),
        [
            (True, False, 2),
            (False, False, 1),
            (True, True, 1),
        ],
    )
    async def test_parallel_read_only_tool_dispatch(
        self,
        tmp_path: Path,
        parallel_cap: bool,
        second_side_effecting: bool,
        expected_max_in_flight: int,
    ) -> None:
        read_probe = _ConcurrencyProbeTool("probe_read", is_side_effecting=False)
        second_name = "write_file" if second_side_effecting else "probe_read"
        registry = ToolRegistry()
        registry.register(read_probe)
        second_probe: _ConcurrencyProbeTool | None = None
        if second_side_effecting:
            second_probe = _ConcurrencyProbeTool("write_file", is_side_effecting=True)
            registry.register(second_probe)

        llm = _ScriptedCapableLLM(
            responses=[
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="c1", name="probe_read", arguments={"item": "1"}),
                        ToolCall(id="c2", name=second_name, arguments={"item": "2"}),
                    ],
                    finish_reason="tool_calls",
                ),
                LLMResponse(content="All done", finish_reason="stop"),
            ],
            caps=ProviderCapabilities(parallel_tool_calls=parallel_cap),
        )
        run = AgentRun(
            task="Inspect files",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=5, context_enabled=False),
        )
        journal = RunJournal(run.id, path=tmp_path / "journal.jsonl", fsync=False)
        await react_loop(run, llm, registry, _DummySandbox(), journal=journal)

        assert run.state == RunState.COMPLETED
        assert read_probe.max_in_flight == expected_max_in_flight
        if second_probe is not None:
            assert second_probe.max_in_flight == 1
        assert [inv.arguments["item"] for inv in run.tool_invocations] == ["1", "2"]

    @pytest.mark.asyncio
    async def test_previous_response_id_carried_until_compaction(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
        big_output = "line\n" * 600

        class _SelectiveReadTool(Tool):
            @property
            def name(self) -> str:
                return "read_file"

            @property
            def description(self) -> str:
                return "Read"

            @property
            def parameters(self) -> dict[str, Any]:
                return {"type": "object", "properties": {"path": {"type": "string"}}}

            @property
            def side_effecting(self) -> bool:
                return False

            async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
                del sandbox
                content = big_output if arguments.get("path") == "huge.py" else "x = 1"
                return ToolResult(output=content, exit_code=0)

        registry = ToolRegistry()
        registry.register(_SelectiveReadTool())
        llm = _ScriptedCapableLLM(
            responses=[
                LLMResponse(
                    content=None,
                    response_id="resp_1",
                    tool_calls=[
                        ToolCall(id="c1", name="read_file", arguments={"path": "small.py"})
                    ],
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content=None,
                    response_id="resp_2",
                    tool_calls=[ToolCall(id="c2", name="read_file", arguments={"path": "huge.py"})],
                    finish_reason="tool_calls",
                ),
                LLMResponse(content="Finished", response_id="resp_3", finish_reason="stop"),
            ],
            caps=ProviderCapabilities(stateful_response_ids=True, prompt_caching=True),
        )
        policy = ContextPolicy(
            context_window_tokens=1000,
            compaction_trigger_ratio=0.5,
            compaction_target_ratio=0.4,
            keep_recent_turns=1,
            discover_instructions=False,
            repo_map_enabled=False,
        )
        engine = ContextEngine(tmp_path, task="Inspect large files", policy=policy)
        run = AgentRun(
            task="Inspect large files",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=6),
        )
        journal = RunJournal(run.id, path=tmp_path / "journal.jsonl", fsync=False)
        await react_loop(
            run, llm, registry, _DummySandbox(), journal=journal, context_engine=engine
        )

        assert run.state == RunState.COMPLETED
        assert llm.configs[0].previous_response_id is None
        assert llm.configs[1].previous_response_id == "resp_1"
        # Compaction triggered by huge.py output resets previous_response_id on turn 3
        assert run.context_stats["compactions"] >= 1
        assert llm.configs[2].previous_response_id is None


# ---------------------------------------------------------------------------
# 5. Routing Policy, RoutingLLMProvider in react_loop, & Persistence
# ---------------------------------------------------------------------------


def _sample_candidates() -> tuple[ModelCandidate, ModelCandidate, ModelCandidate]:
    cheap = ModelCandidate(
        profile_name="cheap-fast",
        provider_name="gemini",
        model="gemini-3.1-flash-lite-preview",
        capabilities=ProviderCapabilities(
            reasoning_continuity=False,
            parallel_tool_calls=True,
            prompt_caching=True,
            structured_output=False,
            stateful_response_ids=False,
            native_tools=True,
            max_context_tokens=1_000_000,
            api_protocol="gemini_generate_content",
        ),
        input_cost_per_token=2.5e-7,
        output_cost_per_token=1.5e-6,
    )
    mid = ModelCandidate(
        profile_name="mid-openai",
        provider_name="openai",
        model="gpt-5.4",
        capabilities=resolve_capabilities("openai", "gpt-5.4"),
        input_cost_per_token=2.5e-6,
        output_cost_per_token=1.5e-5,
    )
    strong = ModelCandidate(
        profile_name="strong-anthropic",
        provider_name="anthropic",
        model="claude-sonnet-4-6",
        capabilities=resolve_capabilities("anthropic", "claude-sonnet-4-6"),
        input_cost_per_token=3.0e-6,
        output_cost_per_token=1.5e-5,
    )
    return cheap, mid, strong


class TestRouting:
    def test_select_route_policies(self) -> None:
        cheap, mid, strong = _sample_candidates()
        candidates = [mid, strong, cheap]

        # Cost-first picks cheapest
        cost_decision = select_route(
            candidates,
            TurnRoutingContext(task="hello", iteration=1),
            RoutingPolicy(enabled=True, strategy="cost_first"),
        )
        assert cost_decision.candidate.profile_name == "cheap-fast"

        # Budget downgrade overrides balanced/capability choice when >= threshold
        budget_decision = select_route(
            candidates,
            TurnRoutingContext(
                task="hard fix",
                iteration=3,
                consecutive_failures=3,
                spent_cost_usd=0.80,
                max_cost_usd=1.00,
            ),
            RoutingPolicy(enabled=True, strategy="balanced", downgrade_budget_fraction=0.75),
        )
        assert budget_decision.candidate.profile_name == "cheap-fast"
        assert budget_decision.reason == "budget_downgrade"

        # Failure escalation requires reasoning_continuity and picks strongest capable candidate
        esc_decision = select_route(
            candidates,
            TurnRoutingContext(task="fix", iteration=2, consecutive_failures=2),
            RoutingPolicy(enabled=True, strategy="cost_first", escalate_on_failures=2),
        )
        assert esc_decision.reason == "failure_escalation"
        assert esc_decision.candidate.profile_name == "mid-openai"

        # Structured output requirement filters out cheap candidate where structured_output=False
        struct_decision = select_route(
            candidates,
            TurnRoutingContext(task="schema", needs_structured_output=True),
            RoutingPolicy(enabled=True, strategy="cost_first"),
        )
        assert struct_decision.candidate.profile_name != "cheap-fast"

    @pytest.mark.asyncio
    async def test_routing_provider_in_react_loop_and_persistence_roundtrip(
        self,
        tmp_path: Path,
    ) -> None:
        cheap, mid, _ = _sample_candidates()
        cheap = replace(
            cheap,
            capabilities=replace(cheap.capabilities, stateful_response_ids=True),
        )
        cheap_llm = _ScriptedCapableLLM(
            responses=[
                LLMResponse(
                    content=None,
                    response_id="resp_cheap_1",
                    tool_calls=[ToolCall(id="c1", name="flaky_tool", arguments={"item": "fail"})],
                    finish_reason="tool_calls",
                )
            ],
            caps=cheap.capabilities,
        )
        mid_llm = _ScriptedCapableLLM(
            responses=[
                LLMResponse(
                    content=None,
                    response_id="resp_mid_1",
                    reasoning_blocks=[{"type": "reasoning", "id": "rs_1"}],
                    tool_calls=[
                        ToolCall(id="c2", name="flaky_tool", arguments={"item": "still_failing"})
                    ],
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content="Resolved",
                    response_id="resp_mid_2",
                    reasoning_blocks=[{"type": "reasoning", "id": "rs_2"}],
                    finish_reason="stop",
                ),
            ],
            caps=mid.capabilities,
        )
        router = RoutingLLMProvider(
            [cheap, mid],
            {"cheap-fast": cheap_llm, "mid-openai": mid_llm},
            policy=RoutingPolicy(enabled=True, strategy="cost_first", escalate_on_failures=1),
        )

        registry = ToolRegistry()
        registry.register(
            _ConcurrencyProbeTool("flaky_tool", is_side_effecting=False, delay=0.0, exit_code=1)
        )

        run = AgentRun(
            task="Fix flaky build",
            repo_path=str(tmp_path),
            config=AgentConfig(max_iterations=5, context_enabled=False),
        )
        journal_path = tmp_path / "journal.jsonl"
        journal = RunJournal(run.id, path=journal_path, fsync=False)
        await react_loop(run, router, registry, _DummySandbox(), journal=journal)

        assert run.state == RunState.COMPLETED
        assert [d.candidate.profile_name for d in router.decisions] == [
            "cheap-fast",
            "mid-openai",
            "mid-openai",
        ]
        # Switching from cheap-fast to mid-openai clears previous_response_id;
        # staying on mid-openai carries resp_mid_1 forward.
        assert mid_llm.configs[0].previous_response_id is None
        assert mid_llm.configs[1].previous_response_id == "resp_mid_1"

        events = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        route_events = [e for e in events if e["kind"] == "route_selected"]
        assert [e["data"]["profile"] for e in route_events] == [
            "cheap-fast",
            "mid-openai",
            "mid-openai",
        ]
        assert route_events[1]["data"]["reason"] == "failure_escalation"
        assert "budget_fraction_used" in route_events[0]["data"]

        # Persistence roundtrip preserves response_id and reasoning_blocks
        runs_dir = tmp_path / "runs"
        save_run(run, base_dir=runs_dir)
        loaded = load_run(run.id, base_dir=runs_dir)
        assert loaded.messages[-1].response_id == "resp_mid_2"
        assert loaded.messages[-1].reasoning_blocks == [{"type": "reasoning", "id": "rs_2"}]


# ---------------------------------------------------------------------------
# 6. Configuration Env Overrides & CLI `models capabilities`
# ---------------------------------------------------------------------------


class TestConfigAndCLI:
    def test_env_overrides_routing_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AGENT_FORGE_ROUTING_ENABLED", "true")
        monkeypatch.setenv("AGENT_FORGE_ROUTING_STRATEGY", "cost_first")
        monkeypatch.setenv("AGENT_FORGE_ROUTING_CANDIDATES", "gemini-flash-lite,openai-gpt54")
        monkeypatch.setenv("AGENT_FORGE_ROUTING_DOWNGRADE_BUDGET_FRACTION", "0.6")
        monkeypatch.setenv("AGENT_FORGE_ROUTING_ESCALATE_ON_FAILURES", "3")

        cfg = load_config()
        assert cfg.routing.enabled is True
        assert cfg.routing.strategy == "cost_first"
        assert cfg.routing.candidates == ["gemini-flash-lite", "openai-gpt54"]
        assert cfg.routing.downgrade_budget_fraction == pytest.approx(0.6)
        assert cfg.routing.escalate_on_failures == 3

    def test_cli_models_capabilities_json_text_and_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_toml = tmp_path / "agent-forge.toml"
        config_toml.write_text(
            '[model_profiles.openai-gpt54]\nprovider = "openai"\nmodel = "gpt-5.4"\n\n'
            '[model_profiles.meta-llama]\nprovider = "openai"\nmodel = "meta-llama/Llama-4"\n'
            'base_url = "https://api.together.xyz/v1"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        res_json = runner.invoke(main, ["models", "capabilities", "--output-format", "json"])
        assert res_json.exit_code == 0, res_json.output
        parsed = json.loads(res_json.stdout)
        assert parsed["openai-gpt54"]["api_protocol"] == "openai_responses"
        assert parsed["meta-llama"]["api_protocol"] == "openai_chat_completions"

        res_text = runner.invoke(main, ["models", "capabilities", "--output-format", "text"])
        assert res_text.exit_code == 0, res_text.output
        assert "openai-gpt54" in res_text.stdout

        res_bad = runner.invoke(main, ["models", "capabilities", "missing-profile"])
        assert res_bad.exit_code != 0

    def test_routing_provider_from_config_deduplicates_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
        cfg = ForgeConfig(
            model_profiles={
                "p1": ModelProfileSettings(
                    provider="gemini", model="gemini-3.1-flash-lite-preview"
                ),
                "p2": ModelProfileSettings(provider="gemini", model="gemini-2.5-flash"),
            }
        )
        created: list[str] = []

        def _fake_factory(provider: str, api_key: str, **kwargs: Any) -> LLMProvider:
            del api_key, kwargs
            created.append(provider)
            return _MinimalLLM()

        router = RoutingLLMProvider.from_config(cfg, provider_factory=_fake_factory)
        assert len(created) == 1
        assert len(router._candidates) == 2
