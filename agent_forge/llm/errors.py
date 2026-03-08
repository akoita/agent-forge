"""Agent Forge exception hierarchy.

Follows the error taxonomy from spec.md § 7.1.
All exceptions inherit from :class:`AgentForgeError`.
"""

from __future__ import annotations


class AgentForgeError(Exception):
    """Base exception for all Agent Forge errors."""


# ---------------------------------------------------------------------------
# LLM Errors
# ---------------------------------------------------------------------------


class LLMError(AgentForgeError):
    """Errors from LLM provider interactions."""


class LLMRateLimitError(LLMError):
    """Rate limit exceeded — should trigger retry with backoff."""


class LLMAuthError(LLMError):
    """Invalid API key or unauthorized — fail immediately."""


class LLMContextOverflowError(LLMError):
    """Prompt exceeds model context window."""


class LLMTimeoutError(LLMError):
    """LLM request timed out."""


class LLMResponseError(LLMError):
    """Malformed or unparseable response from the LLM."""


# ---------------------------------------------------------------------------
# Tool Errors
# ---------------------------------------------------------------------------


class ToolError(AgentForgeError):
    """Errors from tool execution."""


class ToolNotFoundError(ToolError):
    """LLM requested a tool that doesn't exist."""


class ToolTimeoutError(ToolError):
    """Tool execution exceeded timeout."""


class ToolExecutionError(ToolError):
    """Tool returned non-zero exit code or sandbox transient failure."""


# ---------------------------------------------------------------------------
# Sandbox Errors
# ---------------------------------------------------------------------------


class SandboxError(AgentForgeError):
    """Errors from sandbox operations."""


class SandboxStartupError(SandboxError):
    """Failed to create or start the sandbox container."""


class SandboxTimeoutError(SandboxError):
    """Sandbox container lifetime exceeded."""


# ---------------------------------------------------------------------------
# State Machine Errors
# ---------------------------------------------------------------------------


class InvalidStateTransitionError(AgentForgeError):
    """Invalid run state transition attempted."""
