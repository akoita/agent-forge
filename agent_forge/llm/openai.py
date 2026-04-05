"""OpenAI LLM provider adapter.

Uses the OpenAI Chat Completions REST API via httpx. Handles tool
mapping, streaming, and retry logic per spec § 4.1 and § 7.2.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_forge.llm.base import ToolDefinition

import httpx

from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    Role,
    TokenUsage,
    ToolCall,
)
from agent_forge.llm.errors import (
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from agent_forge.observability import get_logger

logger = get_logger("openai")

_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-5.4"

# Retry config per spec § 7.2
_MAX_RETRIES = 5
_BACKOFF_BASE = 2.0  # seconds
_BACKOFF_MAX = 30.0  # cap delay at 30s
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


class OpenAIProvider(LLMProvider):
    """OpenAI adapter using the Chat Completions REST API with httpx."""

    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url or _OPENAI_BASE_URL
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(120.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        """Send a completion request and return the full response."""
        cfg = config or LLMConfig(model=_DEFAULT_MODEL)
        body = self._build_request_body(messages, tools, cfg)

        data = await self._post_with_retry("/chat/completions", body, cfg)
        return self._parse_response(data, cfg.model)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        """Stream partial responses as they arrive via SSE."""
        cfg = config or LLMConfig(model=_DEFAULT_MODEL)
        body = self._build_request_body(messages, tools, cfg)
        body["stream"] = True

        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
                json=body,
                timeout=httpx.Timeout(cfg.timeout_seconds),
            ) as response:
                self._check_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    yield self._parse_stream_chunk(chunk, cfg.model)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"OpenAI streaming request timed out after {cfg.timeout_seconds}s"
            ) from exc

    # ------------------------------------------------------------------
    # Request Building
    # ------------------------------------------------------------------

    def _build_request_body(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: LLMConfig,
    ) -> dict[str, Any]:
        """Build the OpenAI Chat Completions request body."""
        oai_messages = self._messages_to_openai(messages)
        body: dict[str, Any] = {
            "model": config.model,
            "messages": oai_messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "top_p": config.top_p,
        }

        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"

        return body

    @staticmethod
    def _messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal messages to OpenAI chat format."""
        oai_messages: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                oai_messages.append(
                    {
                        "role": "system",
                        "content": msg.content,
                    }
                )
            elif msg.role == Role.USER:
                oai_messages.append(
                    {
                        "role": "user",
                        "content": msg.content,
                    }
                )
            elif msg.role == Role.ASSISTANT:
                m: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content,
                }
                if msg.tool_calls:
                    m["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                oai_messages.append(m)
            elif msg.role == Role.TOOL:
                oai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id or "",
                        "content": msg.content,
                    }
                )

        return oai_messages

    # ------------------------------------------------------------------
    # Response Parsing
    # ------------------------------------------------------------------

    def _parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        """Parse an OpenAI Chat Completions response."""
        choices = data.get("choices", [])
        if not choices:
            return LLMResponse(
                content=None,
                model=model,
                finish_reason="error",
            )

        choice = choices[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")

        content = message.get("content")
        tool_calls = self._parse_tool_calls(message.get("tool_calls"))

        usage_data = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        mapped_reason = self._map_finish_reason(finish_reason, tool_calls)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=mapped_reason,
        )

    def _parse_stream_chunk(self, data: dict[str, Any], model: str) -> LLMResponse:
        """Parse a single SSE chunk from a streaming response."""
        choices = data.get("choices", [])
        if not choices:
            return LLMResponse(content=None, model=model, finish_reason="")

        delta = choices[0].get("delta", {})
        content = delta.get("content")
        tool_calls = self._parse_tool_calls(delta.get("tool_calls"))
        finish_reason = choices[0].get("finish_reason") or ""

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            model=model,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _parse_tool_calls(
        raw_calls: list[dict[str, Any]] | None,
    ) -> list[ToolCall]:
        """Parse OpenAI tool_calls into our ToolCall objects."""
        if not raw_calls:
            return []
        result: list[ToolCall] = []
        for tc in raw_calls:
            fn = tc.get("function", {})
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}
            result.append(
                ToolCall(
                    id=tc.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=args,
                )
            )
        return result

    @staticmethod
    def _map_finish_reason(oai_reason: str, tool_calls: list[ToolCall]) -> str:
        """Map OpenAI finish reason to our standard reasons."""
        if tool_calls:
            return "tool_calls"
        mapping = {
            "stop": "stop",
            "length": "length",
            "content_filter": "error",
        }
        return mapping.get(oai_reason, "stop")

    # ------------------------------------------------------------------
    # HTTP + Retry
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_delay(attempt: int, resp: httpx.Response | None = None) -> float:
        """Compute the retry delay, respecting Retry-After if present."""
        if resp is not None:
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            if retry_after:
                try:
                    parsed: float = float(retry_after)
                    return min(parsed, _BACKOFF_MAX)
                except ValueError:
                    pass
        # Exponential backoff with jitter
        delay: float = _BACKOFF_BASE * (2**attempt)
        jitter = random.uniform(0, delay * 0.25)  # noqa: S311
        capped: float = min(delay + jitter, _BACKOFF_MAX)
        return capped

    async def _post_with_retry(
        self,
        url: str,
        body: dict[str, Any],
        config: LLMConfig,
    ) -> dict[str, Any]:
        """POST with exponential backoff retry on retryable errors."""
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._client.post(
                    url,
                    json=body,
                    timeout=httpx.Timeout(config.timeout_seconds),
                )
                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt < _MAX_RETRIES:
                        delay = self._compute_delay(attempt, resp)
                        logger.warning(
                            "openai_retryable_error",
                            status_code=resp.status_code,
                            delay=round(delay, 1),
                            attempt=attempt + 1,
                            max_retries=_MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code == 429:
                        raise LLMRateLimitError(
                            f"OpenAI rate limit exceeded after {_MAX_RETRIES} retries"
                        )
                    raise LLMResponseError(
                        f"OpenAI API returned {resp.status_code} after {_MAX_RETRIES} retries"
                    )

                self._check_status(resp)

                try:
                    return resp.json()  # type: ignore[no-any-return]
                except (json.JSONDecodeError, ValueError) as exc:
                    if attempt < _MAX_RETRIES:
                        logger.warning(
                            "openai_malformed_response",
                            attempt=attempt + 1,
                            max_retries=_MAX_RETRIES,
                        )
                        await asyncio.sleep(_BACKOFF_BASE)
                        continue
                    raise LLMResponseError("Malformed JSON response from OpenAI API") from exc

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "openai_timeout",
                        attempt=attempt + 1,
                        max_retries=_MAX_RETRIES,
                    )
                    await asyncio.sleep(_BACKOFF_BASE)
                    continue
                raise LLMTimeoutError(
                    f"OpenAI request timed out after {config.timeout_seconds}s "
                    f"({_MAX_RETRIES} retries exhausted)"
                ) from last_exc

        # Should not reach here, but satisfy type checker
        msg = "Unexpected retry loop exit"
        raise LLMResponseError(msg)  # pragma: no cover

    @staticmethod
    def _check_status(resp: httpx.Response) -> None:
        """Raise specific errors for non-retryable status codes."""
        status_code = resp.status_code
        if status_code == 401 or status_code == 403:
            raise LLMAuthError(
                f"OpenAI authentication failed (HTTP {status_code}). Check your OPENAI_API_KEY."
            )
        if status_code >= 400:
            try:
                body = resp.json()
                detail = body.get("error", {}).get("message", resp.text[:500])
            except Exception:  # noqa: BLE001
                detail = resp.text[:500]
            logger.error("openai_api_error", status_code=status_code, detail=detail)
            raise LLMResponseError(f"OpenAI API error (HTTP {status_code}): {detail}")
