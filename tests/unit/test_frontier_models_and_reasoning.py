"""Unit tests for frontier model capabilities, per-profile pricing, and reasoning controls."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest
import respx
from click.testing import CliRunner

from agent_forge.agent.context import ContextEngine, ContextPolicy
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.runner import _resolve_context_engine_for_config
from agent_forge.cli import _apply_model_profile, _build_agent_config, main
from agent_forge.config import ContextSettings, ForgeConfig, ModelProfileSettings, load_config
from agent_forge.eval.benchmark import _estimate_attempt_cost
from agent_forge.llm.anthropic import AnthropicProvider
from agent_forge.llm.base import LLMConfig, LLMProvider, LLMResponse, Message, Role, TokenUsage
from agent_forge.llm.capabilities import resolve_capabilities
from agent_forge.llm.gemini import GeminiProvider
from agent_forge.llm.openai import OpenAIProvider
from agent_forge.llm.router import _build_candidate
from agent_forge.observability.cost import (
    CostTracker,
    _estimate_cost,
    build_pricing_overrides,
    pricing_override_from_per_million,
)
from agent_forge.tools import ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from agent_forge.sandbox.base import SandboxConfig


class _NoopSandbox:
    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del repo_path, config

    async def stop(self) -> None:
        pass


class TestPricingOverridesAndBudgets:
    """Per-profile pricing overrides drive CostTracker, routing, benchmark, and --max-cost."""

    def test_pricing_overrides_precedence_and_router_candidate(self) -> None:
        overrides = pricing_override_from_per_million(10.0, 40.0, cache_read_cost_per_million=2.0)
        assert overrides is not None
        assert overrides["input_cost_per_token"] == pytest.approx(1e-5)
        assert overrides["output_cost_per_token"] == pytest.approx(4e-5)
        assert overrides["cache_read_input_token_cost"] == pytest.approx(2e-6)

        usage = TokenUsage(
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            total_tokens=1_500_000,
            cached_prompt_tokens=500_000,
        )
        # 500k uncached * $10/1M ($5) + 500k cached * $2/1M ($1) + 500k output * $40/1M ($20) = $26
        model_id = "gpt-9.9-frontier-preview"
        tracker = CostTracker(run_id="r1", pricing_overrides={model_id: overrides})
        tracker.record(usage, model_id)
        assert tracker.total_cost() == pytest.approx(26.0)
        assert _estimate_cost(usage, model_id) == 0.0

        profile = ModelProfileSettings(
            provider="openai",
            model=model_id,
            input_cost_per_million=10.0,
            output_cost_per_million=40.0,
            cache_read_cost_per_million=2.0,
        )
        cfg = ForgeConfig(model_profiles={"frontier": profile})
        cand = _build_candidate(cfg, "frontier", profile)
        assert cand.input_cost_per_token == pytest.approx(1e-5)
        assert cand.output_cost_per_token == pytest.approx(4e-5)
        assert _estimate_attempt_cost(usage, model_id, None, profile=profile) == pytest.approx(26.0)

    @pytest.mark.asyncio
    async def test_unknown_frontier_model_enforces_max_cost_in_react_loop(
        self, tmp_path: Path
    ) -> None:
        class _ExpensiveMockLLM(LLMProvider):
            async def complete(
                self, messages: list[Message], tools: Any = None, config: Any = None
            ) -> LLMResponse:
                del messages, tools, config
                return LLMResponse(
                    content="Working...",
                    finish_reason="tool_calls",
                    usage=TokenUsage(prompt_tokens=2000, completion_tokens=500, total_tokens=2500),
                )

            async def stream(
                self, messages: list[Message], tools: Any = None, config: Any = None
            ) -> Any:
                del messages, tools, config
                yield await self.complete([])

        rates = pricing_override_from_per_million(20.0, 80.0)
        assert rates is not None
        cfg = AgentConfig(
            model="gpt-6.9-unlisted",
            provider="openai",
            max_iterations=5,
            max_cost_usd=0.05,
            budget_action="timeout",
            context_enabled=False,
            pricing_overrides={"gpt-6.9-unlisted": rates},
        )
        run = AgentRun(task="Test cost stop", repo_path=str(tmp_path), config=cfg)
        result = await react_loop(run, _ExpensiveMockLLM(), ToolRegistry(), _NoopSandbox())  # type: ignore[arg-type]
        assert result.state == RunState.TIMEOUT
        assert result.error is not None and "cost" in result.error.lower()


class TestFrontierCapabilitiesAndContextSizing:
    """Generalized long-context patterns and ContextEngine auto-sizing."""

    def test_frontier_long_context_heuristics_and_overrides(self) -> None:
        assert resolve_capabilities("openai", "gpt-5.4").max_context_tokens == 200_000
        assert resolve_capabilities("openai", "gpt-6.2-sol").max_context_tokens == 1_050_000
        assert (
            resolve_capabilities("openai", "openai/gpt-6.2-astra").max_context_tokens == 1_050_000
        )
        assert resolve_capabilities("openai", "o5-mini").max_context_tokens == 200_000
        assert resolve_capabilities("openai", "gpt-6.2-1m").max_context_tokens == 1_000_000

        assert (
            resolve_capabilities("anthropic", "claude-opus-5-5-1m").max_context_tokens == 1_000_000
        )
        assert resolve_capabilities("gemini", "gemini-4-neon").max_context_tokens == 1_000_000

        ant_caps = AnthropicProvider(
            api_key="test-key", capability_overrides={"max_context_tokens": 500_000}
        ).capabilities("claude-opus-5-5")
        assert ant_caps.max_context_tokens == 500_000

        gem_caps = GeminiProvider(
            api_key="test-key", capability_overrides={"max_context_tokens": 2_000_000}
        ).capabilities("gemini-4-carbon")
        assert gem_caps.max_context_tokens == 2_000_000

    def test_context_engine_auto_sizes_when_default_and_preserves_custom_window(
        self, tmp_path: Path
    ) -> None:
        default_cfg = ForgeConfig()
        default_cfg.agent.default_provider = "gemini"
        default_cfg.agent.default_model = "gemini-4-neon"
        engine = _resolve_context_engine_for_config(tmp_path, "Task", default_cfg)
        assert engine is not None
        assert engine.policy.context_window_tokens == 1_000_000

        pinned_cfg = ForgeConfig(context=ContextSettings(context_window_tokens=1_200))
        pinned_cfg.agent.default_provider = "gemini"
        pinned_cfg.agent.default_model = "gemini-4-neon"
        pinned_engine = _resolve_context_engine_for_config(tmp_path, "Task", pinned_cfg)
        assert pinned_engine is not None
        assert pinned_engine.policy.context_window_tokens == 1_200

        manual_engine = ContextEngine(tmp_path, policy=ContextPolicy(context_window_tokens=500))
        manual_engine.sync_context_window(1_000_000)
        assert manual_engine.policy.context_window_tokens == 500


class TestReasoningPayloadsAndCliProfileWiring:
    """Anthropic adaptive thinking, Gemini thinkingLevel, OpenAI effort, and CLI/profile flow."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_adaptive_thinking_and_output_config(self) -> None:
        url = "https://api.anthropic.com/v1/messages"
        respx.post(url).respond(
            200,
            json={
                "model": "claude-opus-5-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
        provider = AnthropicProvider(api_key="test-key")
        await provider.complete(
            [Message(role=Role.USER, content="Solve")],
            config=LLMConfig(
                model="claude-opus-5-5",
                thinking_mode="adaptive",
                reasoning_effort="high",
            ),
        )
        body = json.loads(respx.calls[0].request.content)
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"] == {"effort": "high"}

        await provider.complete(
            [Message(role=Role.USER, content="Fast")],
            config=LLMConfig(model="claude-haiku-5-5", thinking_mode="disabled"),
        )
        body2 = json.loads(respx.calls[1].request.content)
        assert body2["thinking"] == {"type": "disabled"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_gemini_thinking_level_and_openai_reasoning_effort(self) -> None:
        gem_url = (
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-neon:generateContent"
        )
        respx.post(gem_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 2,
                        "totalTokenCount": 7,
                    },
                },
            )
        )
        gemini = GeminiProvider(api_key="test-key")
        await gemini.complete(
            [Message(role=Role.USER, content="Hi")],
            config=LLMConfig(
                model="gemini-4-neon",
                thinking_level="high",
                thinking_budget_tokens=2048,
            ),
        )
        gem_body1 = json.loads(respx.calls[0].request.content)
        assert gem_body1["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "HIGH"}

        await gemini.complete(
            [Message(role=Role.USER, content="Off")],
            config=LLMConfig(model="gemini-4-neon", reasoning_effort="none"),
        )
        gem_body2 = json.loads(respx.calls[1].request.content)
        assert gem_body2["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}

        oai_url = "https://api.openai.com/v1/chat/completions"
        respx.post(oai_url).respond(
            200,
            json={
                "model": "gpt-6.2-sol",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )
        oai_chat = OpenAIProvider(api_key="test-key", api_mode="chat_completions")
        await oai_chat.complete(
            [Message(role=Role.USER, content="Hi")],
            config=LLMConfig(model="gpt-6.2-sol", reasoning_effort="medium"),
        )
        oai_body = json.loads(respx.calls[-1].request.content)
        assert oai_body["reasoning_effort"] == "medium"

    def test_profile_application_and_cli_reasoning_flags(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "agent-forge.toml"
        cfg_file.write_text(
            "[model_profiles.frontier]\n"
            'provider = "anthropic"\n'
            'model = "claude-opus-5-5-custom"\n'
            "input_cost_per_million = 15.0\n"
            "output_cost_per_million = 75.0\n"
            'thinking_mode = "adaptive"\n'
            'reasoning_effort = "high"\n',
            encoding="utf-8",
        )
        cfg = load_config(project_path=cfg_file, user_path=tmp_path / "none.toml")
        _apply_model_profile(cfg, "frontier")
        agent_cfg = _build_agent_config(cfg, cfg.agent.default_provider)
        assert agent_cfg.model == "claude-opus-5-5-custom"
        assert agent_cfg.thinking_mode == "adaptive"
        assert agent_cfg.reasoning_effort == "high"
        assert agent_cfg.pricing_overrides["claude-opus-5-5-custom"][
            "input_cost_per_token"
        ] == pytest.approx(15e-6)
        assert (
            build_pricing_overrides(cfg)["*"]
            == agent_cfg.pricing_overrides["claude-opus-5-5-custom"]
        )

        with (
            patch("agent_forge.cli.load_config", return_value=cfg) as mock_load,
            patch("agent_forge.cli._run_agent"),
            patch("agent_forge.cli._emit_run_output"),
        ):
            res = CliRunner().invoke(
                main,
                [
                    "run",
                    "--task",
                    "Reason carefully",
                    "--repo",
                    str(tmp_path),
                    "--provider",
                    "gemini",
                    "--thinking-level",
                    "high",
                    "--thinking-mode",
                    "adaptive",
                    "--reasoning-effort",
                    "medium",
                    "--thinking-budget",
                    "4096",
                ],
                env={"GEMINI_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"},
            )
        assert res.exit_code == 0, res.output
        overrides = mock_load.call_args.kwargs["cli_overrides"]
        assert overrides["agent.thinking_level"] == "high"
        assert overrides["agent.thinking_mode"] == "adaptive"
        assert overrides["agent.reasoning_effort"] == "medium"
        assert overrides["agent.thinking_budget_tokens"] == 4096
