"""Gemini LLM provider adapter.

Uses the Gemini REST API via httpx. Handles tool mapping, streaming,
and retry logic per spec § 4.1 and § 7.2.
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from typing import TYPE_CHECKING, Any

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

logger = get_logger("gemini")

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
_DEFAULT_MODEL = "gemini-4-hydrogen"

# Retry config per spec § 7.2
_MAX_RETRIES = 5
_BACKOFF_BASE = 2.0  # seconds
_BACKOFF_MAX = 30.0  # cap delay at 30s
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


class GeminiProvider(LLMProvider):
    """Gemini adapter using the REST API with httpx."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        capability_overrides: dict[str, Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url or _GEMINI_BASE_URL
        self._capability_overrides = dict(capability_overrides or {})
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(120.0),
        )

    def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        """Return declared capabilities for Gemini generateContent API."""
        return resolve_capabilities(
            "gemini",
            model or _DEFAULT_MODEL,
            overrides=self._capability_overrides or None,
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
        url = f"/v1beta/models/{cfg.model}:generateContent"

        data = await self._post_with_retry(url, body, cfg)
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
        url = f"/v1beta/models/{cfg.model}:streamGenerateContent"

        params = {"alt": "sse", "key": self._api_key}
        try:
            async with self._client.stream(
                "POST",
                url,
                json=body,
                params=params,
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
                    yield self._parse_response(chunk, cfg.model)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"Gemini streaming request timed out after {cfg.timeout_seconds}s"
            ) from exc

    # ------------------------------------------------------------------
    # Request Building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_thinking_config(config: LLMConfig) -> dict[str, Any] | None:
        if config.thinking_level:
            return {"thinkingLevel": config.thinking_level.strip().upper()}
        if config.thinking_budget_tokens is not None:
            return {"thinkingBudget": config.thinking_budget_tokens}
        if config.reasoning_effort:
            normalized = config.reasoning_effort.strip().lower()
            if normalized == "none":
                return {"thinkingBudget": 0}
            if normalized in {"minimal", "low", "medium", "high"}:
                return {"thinkingLevel": normalized.upper()}
            if normalized in {"xhigh", "max"}:
                return {"thinkingLevel": "HIGH"}
        return None

    def _build_request_body(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: LLMConfig,
    ) -> dict[str, Any]:
        """Build the Gemini API request body from messages and tools."""
        contents = self._messages_to_contents(messages)
        body: dict[str, Any] = {"contents": contents}

        # Generation config
        gen_cfg: dict[str, Any] = {
            "temperature": config.temperature,
            "maxOutputTokens": config.max_tokens,
            "topP": config.top_p,
        }
        if config.response_schema:
            gen_cfg["responseMimeType"] = "application/json"
            gen_cfg["responseSchema"] = config.response_schema
        thinking_cfg = self._build_thinking_config(config)
        if thinking_cfg is not None:
            gen_cfg["thinkingConfig"] = thinking_cfg
        body["generationConfig"] = gen_cfg

        # System instruction (extract from messages)
        system_parts = [{"text": m.content} for m in messages if m.role == Role.SYSTEM]
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}

        # Tool declarations
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        }
                        for t in tools
                    ]
                }
            ]

        return body

    def _messages_to_contents(self, messages: list[Message]) -> list[dict[str, object]]:
        """Convert messages to Gemini 'contents' format.

        System messages are handled separately via systemInstruction.
        """
        contents: list[dict[str, Any]] = []
        tool_names: dict[str, str] = {}

        for msg in messages:
            if msg.role == Role.SYSTEM:
                continue  # handled in systemInstruction

            role = "user" if msg.role in (Role.USER, Role.TOOL) else "model"
            parts: list[dict[str, Any]] = []

            if msg.role == Role.TOOL and msg.tool_call_id:
                # Tool result → functionResponse
                parts.append(
                    {
                        "functionResponse": {
                            "name": tool_names.get(msg.tool_call_id, msg.tool_call_id),
                            "response": {"result": msg.content},
                        }
                    }
                )
            elif msg.tool_calls:
                # Assistant with tool calls → functionCall parts
                for tc in msg.tool_calls:
                    tool_names[tc.id] = tc.name
                    part: dict[str, Any] = {
                        "functionCall": {
                            "name": tc.name,
                            "args": tc.arguments,
                        }
                    }
                    # Gemini 3.x thought signatures — must be preserved
                    if tc.thought_signature:
                        part["thoughtSignature"] = tc.thought_signature
                    parts.append(part)
                if msg.content:
                    parts.append({"text": msg.content})
            elif msg.content:
                parts.append({"text": msg.content})

            if parts:
                contents.append({"role": role, "parts": parts})

        return contents

    # ------------------------------------------------------------------
    # Response Parsing
    # ------------------------------------------------------------------

    def _parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        """Parse a Gemini API response into an LLMResponse."""
        candidates = data.get("candidates", [])
        if not candidates:
            return LLMResponse(
                content=None,
                model=model,
                finish_reason="error",
            )

        candidate = candidates[0]
        content_obj = candidate.get("content", {})
        parts = content_obj.get("parts", [])
        finish_reason = candidate.get("finishReason", "STOP")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        reasoning_blocks: list[dict[str, Any]] = []

        for part in parts:
            if part.get("thought") is True:
                reasoning_blocks.append(dict(part))
            elif "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=str(uuid.uuid4()),
                        name=fc.get("name", ""),
                        arguments=fc.get("args", {}),
                        thought_signature=part.get("thoughtSignature"),
                    )
                )

        # Parse token usage
        usage_data = data.get("usageMetadata", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("promptTokenCount", 0),
            completion_tokens=usage_data.get("candidatesTokenCount", 0),
            total_tokens=usage_data.get("totalTokenCount", 0),
            cached_prompt_tokens=int(usage_data.get("cachedContentTokenCount", 0)),
            reasoning_tokens=int(usage_data.get("thoughtsTokenCount", 0)),
        )

        # Map Gemini finish reasons to our standard
        mapped_reason = self._map_finish_reason(str(finish_reason), tool_calls)

        return LLMResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason=mapped_reason,
            reported_model=data.get("modelVersion"),
            reasoning_blocks=reasoning_blocks,
        )

    @staticmethod
    def _map_finish_reason(gemini_reason: str, tool_calls: list[ToolCall]) -> str:
        """Map Gemini finish reason to our standard reasons."""
        if tool_calls:
            return "tool_calls"
        mapping = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "error",
            "RECITATION": "error",
            "OTHER": "error",
        }
        return mapping.get(gemini_reason, "stop")

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
        params = {"key": self._api_key}
        last_exc: Exception | None = None

        max_retries = _MAX_RETRIES if config.max_retries is None else config.max_retries
        for attempt in range(max_retries + 1):
            try:
                resp = await self._client.post(
                    url,
                    json=body,
                    params=params,
                    timeout=httpx.Timeout(config.timeout_seconds),
                )
                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt < max_retries:
                        delay = self._compute_delay(attempt, resp)
                        logger.warning(
                            "gemini_retryable_error",
                            status_code=resp.status_code,
                            delay=round(delay, 1),
                            attempt=attempt + 1,
                            max_retries=max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # Out of retries — raise appropriate error
                    if resp.status_code == 429:
                        raise LLMRateLimitError(
                            f"Gemini rate limit exceeded after {max_retries} retries"
                        )
                    raise LLMResponseError(
                        f"Gemini API returned {resp.status_code} after {max_retries} retries"
                    )

                self._check_status(resp)

                try:
                    return resp.json()  # type: ignore[no-any-return]
                except (json.JSONDecodeError, ValueError) as exc:
                    if attempt < max_retries:
                        logger.warning(
                            "gemini_malformed_response",
                            attempt=attempt + 1,
                            max_retries=max_retries,
                        )
                        await asyncio.sleep(_BACKOFF_BASE)
                        continue
                    raise LLMResponseError("Malformed JSON response from Gemini API") from exc

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < max_retries:
                    logger.warning(
                        "gemini_timeout",
                        attempt=attempt + 1,
                        max_retries=max_retries,
                    )
                    await asyncio.sleep(_BACKOFF_BASE)
                    continue
                raise LLMTimeoutError(
                    f"Gemini request timed out after {config.timeout_seconds}s "
                    f"({max_retries} retries exhausted)"
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
                f"Gemini authentication failed (HTTP {status_code}). Check your GEMINI_API_KEY."
            )
        if status_code >= 400:
            # Include response body for debugging
            try:
                body = resp.json()
                detail = body.get("error", {}).get("message", resp.text[:500])
            except Exception:  # noqa: BLE001
                detail = resp.text[:500]
            logger.error("gemini_api_error", status_code=status_code, detail=detail)
            raise LLMResponseError(f"Gemini API error (HTTP {status_code}): {detail}")
