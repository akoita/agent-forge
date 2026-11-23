"""Unit tests for the OpenAI LLM provider adapter.

Uses respx to mock httpx requests to the OpenAI Responses API (default) and
Chat Completions API (fallback / custom base_url / explicit api_mode).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from agent_forge.llm.base import (
    LLMConfig,
    Message,
    Role,
    ToolCall,
    ToolDefinition,
)
from agent_forge.llm.errors import (
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from agent_forge.llm.factory import create_provider
from agent_forge.llm.openai import OpenAIProvider

_OPENAI_URL = "https://api.openai.com/v1"
_MODEL = "gpt-5.4"
_RESPONSES_URL = f"{_OPENAI_URL}/responses"
_COMPLETIONS_URL = f"{_OPENAI_URL}/chat/completions"


def _text_response(
    text: str,
    *,
    response_id: str = "resp_1",
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> dict[str, object]:
    """Build a minimal OpenAI Responses API text completion response."""
    return {
        "id": response_id,
        "model": _MODEL,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


def _tool_call_response(
    name: str,
    args: dict[str, object],
    *,
    call_id: str = "call_abc123",
    response_id: str = "resp_tool_1",
) -> dict[str, object]:
    """Build an OpenAI Responses API tool call response."""
    return {
        "id": response_id,
        "model": _MODEL,
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(args),
            }
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 10,
            "total_tokens": 30,
        },
    }


def _chat_text_response(text: str) -> dict[str, object]:
    """Build a minimal Chat Completions API text completion response."""
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _chat_tool_call_response(name: str, args: dict[str, object]) -> dict[str, object]:
    """Build a Chat Completions API tool call response."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
        },
    }


# ---------------------------------------------------------------------------
# Provider Construction
# ---------------------------------------------------------------------------


class TestOpenAIProvider:
    """Tests for OpenAIProvider construction, api_mode, and lifecycle."""

    def test_create_provider_defaults_to_responses(self) -> None:
        provider = OpenAIProvider(api_key="test-key")
        assert isinstance(provider, OpenAIProvider)
        assert provider.api_mode == "responses"

    def test_custom_base_url_auto_uses_chat_completions(self) -> None:
        provider = OpenAIProvider(api_key="test-key", base_url="https://api.together.xyz/v1")
        assert provider.api_mode == "chat_completions"
        assert provider.capabilities(_MODEL).stateful_response_ids is False

    def test_explicit_api_mode_chat_completions(self) -> None:
        provider = OpenAIProvider(api_key="test-key", api_mode="chat_completions")
        assert provider.api_mode == "chat_completions"
        assert provider.capabilities(_MODEL).api_protocol == "openai_chat_completions"

    def test_factory(self) -> None:
        provider = create_provider("openai", api_key="test-key")
        assert isinstance(provider, OpenAIProvider)

    def test_factory_unknown(self) -> None:
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_provider("unknown", api_key="k")


# ---------------------------------------------------------------------------
# Responses API Complete & Stream
# ---------------------------------------------------------------------------


class TestOpenAIComplete:
    """Tests for OpenAIProvider.complete() on the default Responses API."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_text_completion(self) -> None:
        respx.post(_RESPONSES_URL).respond(200, json=_text_response("Hello world!"))

        provider = OpenAIProvider(api_key="test-key")
        config = LLMConfig(model=_MODEL)
        messages = [Message(role=Role.USER, content="Say hello")]

        resp = await provider.complete(messages, config=config)

        assert resp.content == "Hello world!"
        assert resp.finish_reason == "stop"
        assert resp.response_id == "resp_1"
        assert resp.usage.total_tokens == 15
        assert resp.model == _MODEL
        assert resp.tool_calls == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_call_completion(self) -> None:
        respx.post(_RESPONSES_URL).respond(
            200,
            json=_tool_call_response("read_file", {"path": "main.py"}),
        )

        provider = OpenAIProvider(api_key="test-key")
        tools = [
            ToolDefinition(
                name="read_file",
                description="Read a file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            )
        ]
        messages = [Message(role=Role.USER, content="Read main.py")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, tools=tools, config=config)

        assert resp.content is None
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"
        assert resp.tool_calls[0].arguments == {"path": "main.py"}
        assert resp.finish_reason == "tool_calls"
        assert resp.response_id == "resp_tool_1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_system_message_mapped_to_instructions(self) -> None:
        respx.post(_RESPONSES_URL).respond(200, json=_text_response("I understand."))

        provider = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role=Role.SYSTEM, content="You are a coding assistant."),
            Message(role=Role.USER, content="Hello"),
        ]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "I understand."

        body = json.loads(respx.calls[0].request.content)
        assert body["instructions"] == "You are a coding assistant."
        assert body["input"] == [{"type": "message", "role": "user", "content": "Hello"}]

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_result_message(self) -> None:
        respx.post(_RESPONSES_URL).respond(
            200, json=_text_response("The file contains a function.")
        )

        provider = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role=Role.USER, content="Read main.py"),
            Message(
                role=Role.TOOL,
                content="def main(): pass",
                tool_call_id="call_abc",
            ),
        ]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "The file contains a function."

        body = json.loads(respx.calls[0].request.content)
        tool_item = body["input"][1]
        assert tool_item == {
            "type": "function_call_output",
            "call_id": "call_abc",
            "output": "def main(): pass",
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_choice_auto_and_flattened_tool_definitions(self) -> None:
        respx.post(_RESPONSES_URL).respond(200, json=_text_response("ok"))

        provider = OpenAIProvider(api_key="test-key")
        tools = [
            ToolDefinition(
                name="read_file",
                description="Read a file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            )
        ]
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        await provider.complete(messages, tools=tools, config=config)

        body = json.loads(respx.calls[0].request.content)
        assert body["tool_choice"] == "auto"
        assert len(body["tools"]) == 1
        assert body["tools"][0]["type"] == "function"
        assert body["tools"][0]["name"] == "read_file"

    @respx.mock
    @pytest.mark.asyncio
    async def test_assistant_tool_call_serialization(self) -> None:
        respx.post(_RESPONSES_URL).respond(200, json=_text_response("Done"))

        provider = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role=Role.USER, content="Read file"),
            Message(
                role=Role.ASSISTANT,
                content=None,
                reasoning_blocks=[{"type": "reasoning", "id": "rs_1", "summary": []}],
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "foo.py"},
                    )
                ],
            ),
            Message(
                role=Role.TOOL,
                content="file contents",
                tool_call_id="call_1",
            ),
        ]
        config = LLMConfig(model=_MODEL)

        await provider.complete(messages, config=config)

        body = json.loads(respx.calls[0].request.content)
        types = [item["type"] for item in body["input"]]
        assert types == ["message", "reasoning", "function_call", "function_call_output"]
        assert body["input"][2]["call_id"] == "call_1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_previous_response_id_delta_slicing_and_cache_and_usage(self) -> None:
        respx.post(_RESPONSES_URL).respond(
            200,
            json=_text_response(
                "Patched.", response_id="resp_2", cached_tokens=7, reasoning_tokens=3
            ),
        )

        provider = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role=Role.SYSTEM, content="System prompt"),
            Message(role=Role.USER, content="Initial ask"),
            Message(
                role=Role.ASSISTANT,
                content="",
                response_id="resp_1",
                tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})],
            ),
            Message(role=Role.TOOL, content="contents of a.py", tool_call_id="call_1"),
        ]
        config = LLMConfig(
            model=_MODEL,
            previous_response_id="resp_1",
            cache_key="run-cache-1",
            reasoning_effort="high",
            response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
            parallel_tool_calls=True,
        )

        tools = [ToolDefinition(name="read_file", description="Read", parameters={})]
        resp = await provider.complete(messages, tools=tools, config=config)

        assert resp.usage.cached_prompt_tokens == 7
        assert resp.usage.reasoning_tokens == 3
        body = json.loads(respx.calls[0].request.content)
        assert body["previous_response_id"] == "resp_1"
        assert body["prompt_cache_key"] == "run-cache-1"
        assert body["reasoning"] == {"effort": "high"}
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["parallel_tool_calls"] is True
        # Only delta items after resp_1 are transmitted
        assert body["input"] == [
            {"type": "function_call_output", "call_id": "call_1", "output": "contents of a.py"}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_responses_sse_streaming(self) -> None:
        sse_body = (
            'data: {"type":"response.output_text.delta","delta":"Hel"}\n\n'
            'data: {"type":"response.output_text.delta","delta":"lo!"}\n\n'
            'data: {"type":"response.completed","response":{"id":"resp_stream",'
            '"status":"completed","output":[{"type":"message","role":"assistant",'
            '"content":[{"type":"output_text","text":"Hello!"}]}],'
            '"usage":{"input_tokens":4,"output_tokens":2,'
            '"total_tokens":6,"input_tokens_details":{"cached_tokens":2}}}}\n\n'
            "data: [DONE]\n\n"
        )
        respx.post(_RESPONSES_URL).respond(200, text=sse_body)

        provider = OpenAIProvider(api_key="test-key")
        chunks = [
            chunk
            async for chunk in provider.stream(
                [Message(role=Role.USER, content="Hi")],
                config=LLMConfig(model=_MODEL),
            )
        ]

        assert [c.content for c in chunks[:-1]] == ["Hel", "lo!"]
        assert chunks[-1].finish_reason == "stop"
        assert chunks[-1].response_id == "resp_stream"
        assert chunks[-1].usage.cached_prompt_tokens == 2


# ---------------------------------------------------------------------------
# Chat Completions Fallback Mode
# ---------------------------------------------------------------------------


class TestOpenAIChatCompletionsMode:
    """Tests for OpenAIProvider when routed through /chat/completions."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_chat_completions_text_and_tools(self) -> None:
        route = respx.post(_COMPLETIONS_URL).respond(
            200, json=_chat_tool_call_response("read_file", {"path": "x.py"})
        )

        provider = OpenAIProvider(api_key="test-key", api_mode="chat_completions")
        tools = [ToolDefinition(name="read_file", description="Read", parameters={})]
        resp = await provider.complete(
            [
                Message(role=Role.SYSTEM, content="Sys"),
                Message(role=Role.USER, content="Read x.py"),
            ],
            tools=tools,
            config=LLMConfig(model=_MODEL),
        )

        assert route.call_count == 1
        assert resp.tool_calls[0].name == "read_file"
        body = json.loads(respx.calls[0].request.content)
        assert body["messages"][0] == {"role": "system", "content": "Sys"}
        assert body["tools"][0]["type"] == "function"

    @respx.mock
    @pytest.mark.asyncio
    async def test_custom_base_url_uses_chat_completions(self) -> None:
        custom_url = "https://openrouter.ai/api/v1/chat/completions"
        route = respx.post(custom_url).respond(200, json=_chat_text_response("From proxy"))

        provider = OpenAIProvider(api_key="test-key", base_url="https://openrouter.ai/api/v1")
        resp = await provider.complete(
            [Message(role=Role.USER, content="Hi")],
            config=LLMConfig(model="custom/model"),
        )

        assert route.call_count == 1
        assert resp.content == "From proxy"


# ---------------------------------------------------------------------------
# Error Handling
# ---------------------------------------------------------------------------


class TestOpenAIErrors:
    """Tests for error handling and retry logic."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_auth_error(self) -> None:
        respx.post(_RESPONSES_URL).respond(401, json={"error": {"message": "unauthorized"}})

        provider = OpenAIProvider(api_key="bad-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMAuthError, match="authentication failed"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_exhausts_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        respx.post(_RESPONSES_URL).respond(429, json={"error": {"message": "rate limited"}})

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMRateLimitError, match="rate limit"):
            await provider.complete(messages, config=config)

        assert len(respx.calls) == 6

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        route = respx.post(_RESPONSES_URL)
        route.side_effect = [
            httpx.Response(429, json={"error": {"message": "rate limited"}}),
            httpx.Response(429, json={"error": {"message": "rate limited"}}),
            httpx.Response(200, json=_text_response("Success!")),
        ]

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Success!"
        assert len(respx.calls) == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_timeout_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        respx.post(_RESPONSES_URL).mock(side_effect=httpx.ReadTimeout("timeout"))

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL, timeout_seconds=1)

        with pytest.raises(LLMTimeoutError, match="timed out"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_server_error_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        route = respx.post(_RESPONSES_URL)
        route.side_effect = [
            httpx.Response(500, text="Internal Server Error"),
            httpx.Response(200, json=_text_response("Recovered")),
        ]

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Recovered"

    @respx.mock
    @pytest.mark.asyncio
    async def test_malformed_json_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        route = respx.post(_RESPONSES_URL)
        route.side_effect = [
            httpx.Response(200, text="not json"),
            httpx.Response(200, json=_text_response("Fixed")),
        ]

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "Fixed"

    @respx.mock
    @pytest.mark.asyncio
    async def test_malformed_json_exhausts_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        respx.post(_RESPONSES_URL).respond(200, text="not json at all")

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMResponseError, match="Malformed JSON"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_output(self) -> None:
        respx.post(_RESPONSES_URL).respond(
            200,
            json={"id": "resp_empty", "status": "completed", "output": []},
        )

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content is None
        assert resp.finish_reason == "error"
