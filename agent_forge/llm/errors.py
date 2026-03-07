"""LLM-specific exception hierarchy.

Follows the error taxonomy from spec.md § 7.1.
"""

from __future__ import annotations


class AgentForgeError(Exception):
    """Base exception for all Agent Forge errors."""


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
