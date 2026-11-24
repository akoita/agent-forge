"""Agent data models — AgentRun, RunState, ToolInvocation, and related classes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_forge.agent.contracts import ContractEvaluation
    from agent_forge.agent.verification import VerificationResult
    from agent_forge.llm.base import Message, TokenUsage
    from agent_forge.tools.base import ToolResult


class RunState(Enum):
    """Lifecycle state of an agent run."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    VERIFICATION_FAILED = "verification_failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class AgentConfig:
    """Configuration for a single agent run."""

    max_iterations: int = 25
    max_tokens_per_run: int = 200_000
    model: str = "gemini-3.1-flash-lite-preview"
    provider: str = "gemini"  # "gemini" | "openai" | "anthropic"
    temperature: float = 0.0
    system_prompt: str | None = None  # Override default system prompt
    profile_id: str | None = None  # Active agent profile for this run
    approval_mode: str = "auto"  # "auto" | "prompt" | "deny"
    execution_mode: str = "tool_calling"  # "tool_calling" | "code_mode"
    max_cost_usd: float | None = None
    max_duration_seconds: float | None = None
    budget_action: str = "timeout"  # "timeout" | "pause"
    context_enabled: bool = True
    pricing_overrides: dict[str, dict[str, float]] = field(default_factory=dict)
    reasoning_effort: str | None = None
    thinking_mode: str | None = None
    thinking_budget_tokens: int | None = None
    thinking_level: str | None = None


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
    verification: VerificationResult | None = None
    contract_evaluation: ContractEvaluation | None = None
    context_stats: dict[str, Any] = field(default_factory=dict)


def _zero_usage() -> TokenUsage:
    """Create a zero TokenUsage without a top-level import."""
    from agent_forge.llm.base import TokenUsage

    return TokenUsage(0, 0, 0)
