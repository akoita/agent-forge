"""Unit tests for the OpenAI LLM provider adapter.

Uses respx to mock httpx requests to the OpenAI Chat Completions API.
"""

from __future__ import annotations

import json

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
_COMPLETIONS_URL = f"{_OPENAI_URL}/chat/completions"


def _text_response(text: str) -> dict[str, object]:
    """Build a minimal OpenAI text completion response."""
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


def _tool_call_response(name: str, args: dict[str, object]) -> dict[str, object]:
    """Build an OpenAI tool call response."""
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
    """Tests for OpenAIProvider construction and lifecycle."""

    def test_create_provider(self) -> None:
        provider = OpenAIProvider(api_key="test-key")
        assert isinstance(provider, OpenAIProvider)

    def test_factory(self) -> None:
        provider = create_provider("openai", api_key="test-key")
        assert isinstance(provider, OpenAIProvider)

    def test_factory_unknown(self) -> None:
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_provider("unknown", api_key="k")


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------


class TestOpenAIComplete:
    """Tests for OpenAIProvider.complete()."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_text_completion(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(200, json=_text_response("Hello world!"))

        provider = OpenAIProvider(api_key="test-key")
        config = LLMConfig(model=_MODEL)
        messages = [Message(role=Role.USER, content="Say hello")]

        resp = await provider.complete(messages, config=config)

        assert resp.content == "Hello world!"
        assert resp.finish_reason == "stop"
        assert resp.usage.total_tokens == 15
        assert resp.model == _MODEL
        assert resp.tool_calls == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_call_completion(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(
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

    @respx.mock
    @pytest.mark.asyncio
    async def test_system_message_handled(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(200, json=_text_response("I understand."))

        provider = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role=Role.SYSTEM, content="You are a coding assistant."),
            Message(role=Role.USER, content="Hello"),
        ]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content == "I understand."

        # Verify system message in request
        request = respx.calls[0].request
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"] == "You are a coding assistant."

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_result_message(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(
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

        # Verify tool message in request body
        request = respx.calls[0].request
        body = json.loads(request.content)
        tool_msg = body["messages"][1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_abc"

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_choice_auto_when_tools_provided(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(200, json=_text_response("ok"))

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

        request = respx.calls[0].request
        body = json.loads(request.content)
        assert body["tool_choice"] == "auto"
        assert len(body["tools"]) == 1
        assert body["tools"][0]["type"] == "function"

    @respx.mock
    @pytest.mark.asyncio
    async def test_assistant_tool_call_serialization(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(200, json=_text_response("Done"))

        provider = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role=Role.USER, content="Read file"),
            Message(
                role=Role.ASSISTANT,
                content=None,
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

        request = respx.calls[0].request
        body = json.loads(request.content)
        assistant_msg = body["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert len(assistant_msg["tool_calls"]) == 1
        assert assistant_msg["tool_calls"][0]["id"] == "call_1"


# ---------------------------------------------------------------------------
# Error Handling
# ---------------------------------------------------------------------------


class TestOpenAIErrors:
    """Tests for error handling and retry logic."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_auth_error(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(401, json={"error": {"message": "unauthorized"}})

        provider = OpenAIProvider(api_key="bad-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMAuthError, match="authentication failed"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_exhausts_retries(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(429, json={"error": {"message": "rate limited"}})

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMRateLimitError, match="rate limit"):
            await provider.complete(messages, config=config)

        assert len(respx.calls) == 6

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_retries_then_succeeds(self) -> None:
        route = respx.post(_COMPLETIONS_URL)
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
    async def test_timeout_error(self) -> None:
        respx.post(_COMPLETIONS_URL).mock(side_effect=httpx.ReadTimeout("timeout"))

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL, timeout_seconds=1)

        with pytest.raises(LLMTimeoutError, match="timed out"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_server_error_retries(self) -> None:
        route = respx.post(_COMPLETIONS_URL)
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
    async def test_malformed_json_retries(self) -> None:
        route = respx.post(_COMPLETIONS_URL)
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
    async def test_malformed_json_exhausts_retries(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(200, text="not json at all")

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        with pytest.raises(LLMResponseError, match="Malformed JSON"):
            await provider.complete(messages, config=config)

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_choices(self) -> None:
        respx.post(_COMPLETIONS_URL).respond(200, json={"choices": []})

        provider = OpenAIProvider(api_key="test-key")
        messages = [Message(role=Role.USER, content="Hello")]
        config = LLMConfig(model=_MODEL)

        resp = await provider.complete(messages, config=config)
        assert resp.content is None
        assert resp.finish_reason == "error"
