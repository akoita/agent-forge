"""Unit tests for the OpenRouter gateway provider, config, routing, and cost tracking."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from agent_forge.agent.persistence import deserialize_token_usage, serialize_token_usage
from agent_forge.agent.runner import _create_provider_from_config
from agent_forge.cli import _create_configured_provider
from agent_forge.config import ModelProfileSettings, load_config
from agent_forge.llm.base import LLMConfig, Message, Role, TokenUsage, ToolCall, ToolDefinition
from agent_forge.llm.errors import LLMAuthError, LLMRateLimitError, LLMTimeoutError
from agent_forge.llm.factory import create_provider, provider_names
from agent_forge.llm.openrouter import OpenRouterProvider
from agent_forge.llm.router import RoutingLLMProvider
from agent_forge.observability.cost import CostTracker, lookup_model_pricing

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_openrouter_complete_headers_metadata_and_reported_cost() -> None:
    provider = create_provider(
        "openrouter",
        "sk-or-test-key",
        http_referer="https://example.org/agent-forge",
        app_title="agent-forge-tests",
        fallback_models=["openai/gpt-4o", "anthropic/claude-sonnet-4-6"],
        allow_fallbacks=True,
        require_parameters=True,
        data_collection="deny",
        zdr=True,
        provider_order=["anthropic", "google-vertex"],
        only_providers=["anthropic"],
        ignore_providers=["azure"],
        provider_sort="latency",
    )
    assert isinstance(provider, OpenRouterProvider)
    assert "openrouter" in provider_names()

    captured_body: dict[str, Any] = {}
    captured_headers: dict[str, str] = {}

    with respx.mock(base_url="https://openrouter.ai/api/v1") as mock_router:

        def _handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update({k.lower(): v for k, v in request.headers.items()})
            captured_body.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "id": "gen-openrouter-1",
                    "model": "anthropic/claude-sonnet-4-6-20250929",
                    "provider": "Anthropic",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "native_finish_reason": "end_turn",
                            "message": {
                                "role": "assistant",
                                "content": "Done via OpenRouter.",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 30,
                        "total_tokens": 150,
                        "prompt_tokens_details": {"cached_tokens": 40},
                        "completion_tokens_details": {"reasoning_tokens": 10},
                        "cost": 0.0042,
                    },
                },
            )

        mock_router.post("/chat/completions").mock(side_effect=_handler)

        response = await provider.complete(
            [Message(role=Role.USER, content="Hello OpenRouter")],
            config=LLMConfig(model="anthropic/claude-sonnet-4-6"),
        )
    await provider.close()

    assert captured_headers["authorization"] == "Bearer sk-or-test-key"
    assert captured_headers["http-referer"] == "https://example.org/agent-forge"
    assert captured_headers["x-title"] == "agent-forge-tests"
    assert captured_body["usage"] == {"include": True}
    assert captured_body["models"] == ["anthropic/claude-sonnet-4-6", "openai/gpt-4o"]
    assert captured_body["provider"] == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "order": ["anthropic", "google-vertex"],
        "only": ["anthropic"],
        "ignore": ["azure"],
        "sort": "latency",
    }
    assert response.content == "Done via OpenRouter."
    assert response.model == "anthropic/claude-sonnet-4-6"
    assert response.reported_model == "anthropic/claude-sonnet-4-6-20250929"
    assert response.upstream_provider == "Anthropic"
    assert response.native_finish_reason == "end_turn"
    assert response.finish_reason == "stop"
    assert response.usage.cached_prompt_tokens == 40
    assert response.usage.reasoning_tokens == 10
    assert response.usage.reported_cost_usd == pytest.approx(0.0042)


@pytest.mark.asyncio
async def test_openrouter_tool_call_and_reasoning_details_continuity() -> None:
    provider = OpenRouterProvider(api_key="sk-or-test-key")
    turn_bodies: list[dict[str, Any]] = []
    tools = [
        ToolDefinition(
            name="read_file",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]
    reasoning_detail = {
        "type": "reasoning.encrypted",
        "id": "rs_1",
        "format": "anthropic-claude-v1",
        "data": "opaque-encrypted-payload",
    }

    with respx.mock(base_url="https://openrouter.ai/api/v1") as mock_router:

        def _handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            turn_bodies.append(body)
            if len(turn_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "id": "gen-or-t1",
                        "model": "anthropic/claude-sonnet-4-6",
                        "provider": "Google Vertex",
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "native_finish_reason": "tool_use",
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "reasoning_details": [reasoning_detail],
                                    "tool_calls": [
                                        {
                                            "id": "call_read_1",
                                            "type": "function",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": '{"path": "/workspace/app.py"}',
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "gen-or-t2",
                    "model": "anthropic/claude-sonnet-4-6",
                    "provider": "Google Vertex",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "native_finish_reason": "end_turn",
                            "message": {"role": "assistant", "content": "File verified."},
                        }
                    ],
                    "usage": {"prompt_tokens": 90, "completion_tokens": 12, "total_tokens": 102},
                },
            )

        mock_router.post("/chat/completions").mock(side_effect=_handler)

        messages = [Message(role=Role.USER, content="Inspect app.py")]
        r1 = await provider.complete(
            messages, tools=tools, config=LLMConfig(model="anthropic/claude-sonnet-4-6")
        )
        assert len(r1.tool_calls) == 1
        assert r1.tool_calls[0].name == "read_file"
        assert r1.reasoning_blocks == [reasoning_detail]
        assert r1.native_finish_reason == "tool_use"

        messages.append(
            Message(
                role=Role.ASSISTANT,
                content=r1.content or "",
                tool_calls=r1.tool_calls,
                reasoning_blocks=r1.reasoning_blocks,
                response_id=r1.response_id,
            )
        )
        messages.append(Message(role=Role.TOOL, content="x = 1", tool_call_id=r1.tool_calls[0].id))

        r2 = await provider.complete(
            messages, tools=tools, config=LLMConfig(model="anthropic/claude-sonnet-4-6")
        )
    await provider.close()

    assert r2.content == "File verified."
    sent_assistant = next(m for m in turn_bodies[1]["messages"] if m["role"] == "assistant")
    assert sent_assistant["reasoning_details"] == [reasoning_detail]


@pytest.mark.asyncio
async def test_openrouter_streaming_deltas_and_trailing_usage_chunk() -> None:
    provider = OpenRouterProvider(api_key="sk-or-test-key")
    captured_body: dict[str, Any] = {}
    sse_events = [
        {
            "id": "gen-s1",
            "model": "openai/gpt-4o",
            "provider": "OpenAI",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello "}}],
        },
        {
            "id": "gen-s1",
            "model": "openai/gpt-4o",
            "provider": "OpenAI",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                    "native_finish_reason": "tool_calls",
                }
            ],
        },
        {
            "id": "gen-s1",
            "model": "openai/gpt-4o",
            "provider": "OpenAI",
            "choices": [],
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 15,
                "total_tokens": 45,
                "cost": 0.0009,
            },
        },
    ]
    sse_payload = "".join(f"data: {json.dumps(ev)}\n\n" for ev in sse_events) + "data: [DONE]\n\n"

    with respx.mock(base_url="https://openrouter.ai/api/v1") as mock_router:

        def _stream_handler(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                content=sse_payload.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )

        mock_router.post("/chat/completions").mock(side_effect=_stream_handler)
        chunks = [
            chunk
            async for chunk in provider.stream(
                [Message(role=Role.USER, content="Hi")],
                config=LLMConfig(model="openai/gpt-4o"),
            )
        ]
    await provider.close()

    assert captured_body["stream"] is True
    assert captured_body["stream_options"] == {"include_usage": True}
    assert len(chunks) == 3
    assert chunks[0].content == "Hello "
    assert chunks[1].tool_calls == [
        ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})
    ]
    assert chunks[1].native_finish_reason == "tool_calls"
    assert chunks[2].reported_model == "openai/gpt-4o"
    assert chunks[2].upstream_provider == "OpenAI"
    assert chunks[2].usage.reported_cost_usd == pytest.approx(0.0009)


@pytest.mark.asyncio
async def test_openrouter_error_handling_auth_rate_limit_and_timeout() -> None:
    provider = OpenRouterProvider(api_key="bad-key")
    with respx.mock(base_url="https://openrouter.ai/api/v1") as mock_router:
        mock_router.post("/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": {"message": "Invalid credentials"}})
        )
        with pytest.raises(LLMAuthError):
            await provider.complete(
                [Message(role=Role.USER, content="hi")],
                config=LLMConfig(model="openai/gpt-4o", max_retries=0),
            )

        mock_router.post("/chat/completions").mock(
            return_value=httpx.Response(429, json={"error": {"message": "Rate limited"}})
        )
        with pytest.raises(LLMRateLimitError):
            await provider.complete(
                [Message(role=Role.USER, content="hi")],
                config=LLMConfig(model="openai/gpt-4o", max_retries=0),
            )

        mock_router.post("/chat/completions").mock(side_effect=httpx.ReadTimeout("timed out"))
        with pytest.raises(LLMTimeoutError):
            await provider.complete(
                [Message(role=Role.USER, content="hi")],
                config=LLMConfig(model="openai/gpt-4o", max_retries=0, timeout_seconds=1),
            )
    await provider.close()


def test_openrouter_cost_tracking_reported_and_prefixed_fallback() -> None:
    tracker = CostTracker(run_id="run-or-cost")
    tracker.record(
        TokenUsage(
            prompt_tokens=100, completion_tokens=50, total_tokens=150, reported_cost_usd=0.0123
        ),
        "openrouter/anthropic/claude-sonnet-4-6",
        reported_model="anthropic/claude-sonnet-4-6-20250929",
        upstream_provider="Anthropic",
    )
    assert tracker.entries[0].estimated_cost_usd == pytest.approx(0.0123)
    assert tracker.summary()["reported_models"] == ["anthropic/claude-sonnet-4-6-20250929"]
    assert tracker.summary()["upstream_providers"] == ["Anthropic"]

    # Fallback table must resolve OpenRouter-prefixed & tagged slugs without $0 silently
    for slug in ("anthropic/claude-sonnet-4-6", "openai/gpt-4o:free", "google/gemini-2.5-flash"):
        pricing = lookup_model_pricing(slug)
        assert pricing is not None, f"Expected pricing entry for {slug}"
        assert pricing["input_cost_per_token"] > 0

    tracker.record(
        TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500),
        "openai/gpt-4o:nitro",
    )
    assert tracker.entries[1].estimated_cost_usd > 0.0

    # TokenUsage sum and persistence preserve reported_cost_usd
    combined = TokenUsage(10, 5, 15, reported_cost_usd=0.001) + TokenUsage(
        20, 10, 30, reported_cost_usd=0.002
    )
    assert combined.reported_cost_usd == pytest.approx(0.003)
    restored = deserialize_token_usage(serialize_token_usage(combined))
    assert restored.reported_cost_usd == pytest.approx(0.003)


def test_openrouter_config_env_overrides_cli_runner_and_router_wiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FORGE_PROVIDERS_OPENROUTER_DATA_COLLECTION", "deny")
    monkeypatch.setenv("AGENT_FORGE_PROVIDERS_OPENROUTER_ZDR", "true")
    monkeypatch.setenv("AGENT_FORGE_PROVIDERS_OPENROUTER_REQUIRE_PARAMETERS", "true")
    monkeypatch.setenv("AGENT_FORGE_PROVIDERS_OPENROUTER_PROVIDER_ORDER", "anthropic,google-vertex")
    monkeypatch.setenv("AGENT_FORGE_PROVIDERS_OPENROUTER_FALLBACK_MODELS", "openai/gpt-4o")

    cfg = load_config()
    or_cfg = cfg.providers["openrouter"]
    assert or_cfg.api_key_env == "OPENROUTER_API_KEY"
    assert or_cfg.data_collection == "deny"
    assert or_cfg.zdr is True
    assert or_cfg.require_parameters is True
    assert or_cfg.provider_order == ["anthropic", "google-vertex"]
    assert or_cfg.fallback_models == ["openai/gpt-4o"]

    cli_provider = _create_configured_provider(cfg, "openrouter", "sk-or-cli")
    runner_provider = _create_provider_from_config(cfg, "openrouter", "sk-or-runner")
    assert isinstance(cli_provider, OpenRouterProvider)
    assert isinstance(runner_provider, OpenRouterProvider)
    assert cli_provider._provider_prefs["zdr"] is True
    assert cli_provider._provider_prefs["data_collection"] == "deny"
    assert cli_provider._fallback_models == ("openai/gpt-4o",)
    assert runner_provider._provider_prefs["order"] == ["anthropic", "google-vertex"]

    cfg.model_profiles["or-sonnet"] = ModelProfileSettings(
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
    )
    cfg.routing.enabled = True
    cfg.routing.candidates = ["or-sonnet"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-router")
    router = RoutingLLMProvider.from_config(cfg)
    routed_provider = router._providers["or-sonnet"]
    assert isinstance(routed_provider, OpenRouterProvider)
    assert routed_provider._provider_prefs["zdr"] is True
