"""LLM provider base classes and data models.

Defines the unified interface for interacting with multiple LLM providers
(Gemini, OpenAI, Anthropic) via a common adapter pattern.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class Role(Enum):
    """Message role in a conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """A tool invocation requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, object]


@dataclass
class ToolDefinition:
    """Schema describing a tool available to the LLM."""

    name: str
    description: str
    parameters: dict[str, object]  # JSON Schema object


@dataclass
class TokenUsage:
    """Token consumption for a single LLM request."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class Message:
    """A single message in the conversation history."""

    role: Role
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


@dataclass
class LLMResponse:
    """Response from an LLM completion or streaming chunk."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0, 0))
    model: str = ""
    finish_reason: str = ""  # "stop", "tool_calls", "length", "error"


@dataclass
class LLMConfig:
    """Configuration for a single LLM request."""

    model: str = "gemini-2.0-flash"
    temperature: float = 0.0
    max_tokens: int = 4096
    top_p: float = 1.0
    timeout_seconds: int = 120


class LLMProvider(ABC):
    """Abstract base class for all LLM provider adapters."""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        """Send a completion request and return the full response."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        """Stream partial responses as they arrive."""
        ...
        # Make this an async generator for type checkers
        yield  # type: ignore[misc]  # pragma: no cover
