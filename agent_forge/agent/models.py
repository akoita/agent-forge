"""Agent data models — AgentRun, RunState, ToolInvocation, and related classes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_forge.llm.base import Message, TokenUsage
    from agent_forge.tools.base import ToolResult


class RunState(Enum):
    """Lifecycle state of an agent run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class AgentConfig:
    """Configuration for a single agent run."""

    max_iterations: int = 25
    max_tokens_per_run: int = 200_000
    model: str = "gemini-2.0-flash"
    provider: str = "gemini"  # "gemini" | "openai" | "anthropic"
    temperature: float = 0.0
    system_prompt: str | None = None  # Override default system prompt


@dataclass
class ToolInvocation:
    """Record of a single tool execution during an agent run."""

    tool_name: str
    arguments: dict[str, object]
    result: ToolResult
    iteration: int
    timestamp: datetime
    duration_ms: int


@dataclass
class AgentRun:
    """Full state of an agent run, including conversation history and metrics."""

    task: str
    repo_path: str
    config: AgentConfig
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: RunState = RunState.PENDING
    messages: list[Message] = field(default_factory=list)
    iterations: int = 0
    total_tokens: TokenUsage = field(
        default_factory=lambda: _zero_usage(),
    )
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(UTC),
    )
    completed_at: datetime | None = None
    error: str | None = None


def _zero_usage() -> TokenUsage:
    """Create a zero TokenUsage without a top-level import."""
    from agent_forge.llm.base import TokenUsage

    return TokenUsage(0, 0, 0)
