"""OpenAI LLM provider adapter.

Uses the OpenAI Responses API (default for OpenAI endpoints) and supports the
Chat Completions API for custom OpenAI-compatible gateways via ``api_mode``.
Handles tool mapping, stateful continuation, streaming, and retry logic per
spec § 4.1 and § 7.2.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_forge.llm.base import ToolDefinition
    from agent_forge.llm.capabilities import ProviderCapabilities

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
from agent_forge.llm.capabilities import resolve_capabilities
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


def _resolve_api_mode(
    api_mode: Literal["auto", "responses", "chat_completions"],
    base_url: str | None,
) -> Literal["responses", "chat_completions"]:
    if api_mode == "responses":
        return "responses"
    if api_mode == "chat_completions":
        return "chat_completions"
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized or normalized.startswith("https://api.openai.com"):
        return "responses"
    return "chat_completions"


class OpenAIProvider(LLMProvider):
    """OpenAI adapter supporting Responses API (default) and Chat Completions."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        api_mode: Literal["auto", "responses", "chat_completions"] = "auto",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url or _OPENAI_BASE_URL
        self._api_mode: Literal["responses", "chat_completions"] = _resolve_api_mode(
            api_mode, base_url
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(120.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    @property
    def api_mode(self) -> Literal["responses", "chat_completions"]:
        """Return the active wire protocol (``responses`` or ``chat_completions``)."""
        return self._api_mode

    def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        """Return declared capabilities for this OpenAI provider and *model*."""
        return resolve_capabilities(
            "openai",
            model or _DEFAULT_MODEL,
            api_mode=self._api_mode,
            base_url=self._base_url,
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
        if self._api_mode == "responses":
            body = self._build_responses_request_body(messages, tools, cfg)
            data = await self._post_with_retry("/responses", body, cfg)
            return self._parse_responses_response(data, cfg.model)

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
        endpoint = "/responses" if self._api_mode == "responses" else "/chat/completions"
        body = (
            self._build_responses_request_body(messages, tools, cfg)
            if self._api_mode == "responses"
            else self._build_request_body(messages, tools, cfg)
        )
        body["stream"] = True

        try:
            async with self._client.stream(
                "POST",
                endpoint,
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
                    parsed = (
                        self._parse_responses_stream_event(chunk, cfg.model)
                        if self._api_mode == "responses"
                        else self._parse_stream_chunk(chunk, cfg.model)
                    )
                    if parsed is not None:
                        yield parsed
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"OpenAI streaming request timed out after {cfg.timeout_seconds}s"
            ) from exc

    # ------------------------------------------------------------------
    # Responses API Request Building & Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _slice_after_previous_response(
        messages: list[Message],
        previous_response_id: str | None,
    ) -> list[Message]:
        if not previous_response_id:
            return messages
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if msg.role == Role.ASSISTANT and msg.response_id == previous_response_id:
                return messages[idx + 1 :]
        return messages

    @staticmethod
    def _assistant_to_responses_items(msg: Message) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [
            dict(block) for block in (msg.reasoning_blocks or ()) if isinstance(block, dict)
        ]
        if msg.content:
            items.append({"type": "message", "role": "assistant", "content": msg.content})
        for tc in msg.tool_calls or ():
            items.append(
                {
                    "type": "function_call",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                }
            )
        return items

    @classmethod
    def _messages_to_responses_input(
        cls,
        messages: list[Message],
        *,
        previous_response_id: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts = [m.content for m in messages if m.role == Role.SYSTEM and m.content]
        instructions = "\n\n".join(system_parts) if system_parts else None
        tail_messages = cls._slice_after_previous_response(messages, previous_response_id)

        items: list[dict[str, Any]] = []
        for msg in tail_messages:
            if msg.role == Role.SYSTEM:
                continue
            if msg.role == Role.USER:
                items.append({"type": "message", "role": "user", "content": msg.content})
            elif msg.role == Role.ASSISTANT:
                items.extend(cls._assistant_to_responses_items(msg))
            elif msg.role == Role.TOOL:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.tool_call_id or "",
                        "output": msg.content,
                    }
                )
        return instructions, items

    def _build_responses_request_body(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: LLMConfig,
    ) -> dict[str, Any]:
        """Build an OpenAI Responses API request payload."""
        instructions, items = self._messages_to_responses_input(
            messages,
            previous_response_id=config.previous_response_id,
        )
        body: dict[str, Any] = {
            "model": config.model,
            "input": items,
            "max_output_tokens": config.max_tokens,
        }
        if instructions:
            body["instructions"] = instructions
        if config.previous_response_id:
            body["previous_response_id"] = config.previous_response_id
        if config.reasoning_effort:
            body["reasoning"] = {"effort": config.reasoning_effort}
        else:
            body["temperature"] = config.temperature
            body["top_p"] = config.top_p
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"
            if config.parallel_tool_calls is not None:
                body["parallel_tool_calls"] = config.parallel_tool_calls
        if config.cache_key:
            body["prompt_cache_key"] = config.cache_key
        if config.response_schema:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "structured_output",
                    "schema": config.response_schema,
                    "strict": True,
                }
            }
        return body

    @staticmethod
    def _extract_message_item_text(item: dict[str, Any]) -> list[str]:
        raw_content = item.get("content", [])
        if isinstance(raw_content, str):
            return [raw_content] if raw_content else []
        texts: list[str] = []
        for part in raw_content:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return texts

    @staticmethod
    def _parse_function_call_item(item: dict[str, Any]) -> ToolCall:
        args_raw = item.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except json.JSONDecodeError:
            args = {}
        return ToolCall(
            id=str(item.get("call_id") or item.get("id", "")),
            name=str(item.get("name", "")),
            arguments=args,
        )

    @staticmethod
    def _map_responses_finish_reason(
        data: dict[str, Any],
        output: list[Any],
        tool_calls: list[ToolCall],
    ) -> str:
        if tool_calls:
            return "tool_calls"
        if not output:
            return "error"
        status = str(data.get("status", "completed"))
        if status == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason")
            return "length" if reason == "max_output_tokens" else "error"
        if status in ("failed", "cancelled"):
            return "error"
        return "stop"

    def _parse_responses_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        """Parse an OpenAI Responses API JSON payload."""
        output = data.get("output", [])
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        reasoning_blocks: list[dict[str, Any]] = []

        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message":
                text_parts.extend(self._extract_message_item_text(item))
            elif item_type == "function_call":
                tool_calls.append(self._parse_function_call_item(item))
            elif item_type == "reasoning":
                reasoning_blocks.append(dict(item))

        usage_data = data.get("usage", {})
        input_tokens = int(usage_data.get("input_tokens", 0))
        output_tokens = int(usage_data.get("output_tokens", 0))
        total_tokens = int(usage_data.get("total_tokens", input_tokens + output_tokens))
        cached = int((usage_data.get("input_tokens_details") or {}).get("cached_tokens", 0))
        reasoning_toks = int(
            (usage_data.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
        )

        usage = TokenUsage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_prompt_tokens=cached,
            reasoning_tokens=reasoning_toks,
        )
        finish_reason = self._map_responses_finish_reason(data, output, tool_calls)
        return LLMResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=finish_reason,
            reported_model=data.get("model"),
            response_id=data.get("id"),
            reasoning_blocks=reasoning_blocks,
        )

    def _parse_responses_stream_event(
        self,
        event: dict[str, Any],
        model: str,
    ) -> LLMResponse | None:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            return LLMResponse(
                content=event.get("delta"),
                model=model,
                finish_reason="",
            )
        if event_type in ("response.completed", "response.incomplete", "response.failed"):
            resp_obj = event.get("response")
            if isinstance(resp_obj, dict):
                return self._parse_responses_response(resp_obj, model)
            return LLMResponse(content=None, model=model, finish_reason="stop")
        return None

    # ------------------------------------------------------------------
    # Chat Completions Request Building & Parsing (OpenAI-compatible mode)
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
            if config.parallel_tool_calls is not None:
                body["parallel_tool_calls"] = config.parallel_tool_calls

        return body

    @staticmethod
    def _messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal messages to OpenAI chat format."""
        oai_messages: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                oai_messages.append({"role": "system", "content": msg.content})
            elif msg.role == Role.USER:
                oai_messages.append({"role": "user", "content": msg.content})
            elif msg.role == Role.ASSISTANT:
                m: dict[str, Any] = {"role": "assistant", "content": msg.content}
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

    def _parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        """Parse an OpenAI Chat Completions response."""
        choices = data.get("choices", [])
        if not choices:
            return LLMResponse(content=None, model=model, finish_reason="error")

        choice = choices[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")

        content = message.get("content")
        tool_calls = self._parse_tool_calls(message.get("tool_calls"))

        usage_data = data.get("usage", {})
        cached = int((usage_data.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
        reasoning_toks = int(
            (usage_data.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        )
        usage = TokenUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
            cached_prompt_tokens=cached,
            reasoning_tokens=reasoning_toks,
        )

        mapped_reason = self._map_finish_reason(finish_reason, tool_calls)
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=mapped_reason,
            reported_model=data.get("model"),
            response_id=data.get("id"),
        )

    def _parse_stream_chunk(self, data: dict[str, Any], model: str) -> LLMResponse:
        """Parse a single SSE chunk from a Chat Completions stream."""
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

        max_retries = _MAX_RETRIES if config.max_retries is None else config.max_retries
        for attempt in range(max_retries + 1):
            try:
                resp = await self._client.post(
                    url,
                    json=body,
                    timeout=httpx.Timeout(config.timeout_seconds),
                )
                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt < max_retries:
                        delay = self._compute_delay(attempt, resp)
                        logger.warning(
                            "openai_retryable_error",
                            status_code=resp.status_code,
                            delay=round(delay, 1),
                            attempt=attempt + 1,
                            max_retries=max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code == 429:
                        raise LLMRateLimitError(
                            f"OpenAI rate limit exceeded after {max_retries} retries"
                        )
                    raise LLMResponseError(
                        f"OpenAI API returned {resp.status_code} after {max_retries} retries"
                    )

                self._check_status(resp)

                try:
                    return resp.json()  # type: ignore[no-any-return]
                except (json.JSONDecodeError, ValueError) as exc:
                    if attempt < max_retries:
                        logger.warning(
                            "openai_malformed_response",
                            attempt=attempt + 1,
                            max_retries=max_retries,
                        )
                        await asyncio.sleep(_BACKOFF_BASE)
                        continue
                    raise LLMResponseError("Malformed JSON response from OpenAI API") from exc

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < max_retries:
                    logger.warning(
                        "openai_timeout",
                        attempt=attempt + 1,
                        max_retries=max_retries,
                    )
                    await asyncio.sleep(_BACKOFF_BASE)
                    continue
                raise LLMTimeoutError(
                    f"OpenAI request timed out after {config.timeout_seconds}s "
                    f"({max_retries} retries exhausted)"
                ) from last_exc

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
