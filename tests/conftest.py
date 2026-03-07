"""Shared test fixtures for Agent Forge."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from agent_forge.agent.models import AgentConfig, AgentRun
from agent_forge.llm.base import LLMResponse, TokenUsage
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from agent_forge.llm.base import LLMProvider
    from agent_forge.tools.base import ToolRegistry


# ---------------------------------------------------------------------------
# Custom Markers
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers to suppress warnings."""
    config.addinivalue_line("markers", "e2e: end-to-end tests (require GEMINI_API_KEY + Docker)")
    config.addinivalue_line("markers", "integration: integration tests (require Docker)")


# ---------------------------------------------------------------------------
# Shared Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_repo_path() -> str:
    """Path to the sample repository used in E2E tests."""
    return "tests/fixtures/sample_repo"


@pytest.fixture
def agent_config() -> AgentConfig:
    """Default agent configuration for tests."""
    return AgentConfig(
        max_iterations=5,
        max_tokens_per_run=50_000,
        model="gemini-3.1-flash-lite-preview",
        provider="gemini",
        temperature=0.0,
    )


@pytest.fixture
def agent_run(agent_config: AgentConfig, tmp_path: object) -> AgentRun:
    """A fresh AgentRun instance for testing."""
    return AgentRun(
        task="Test task",
        repo_path=str(tmp_path),
        config=agent_config,
    )


@pytest.fixture
def mock_llm() -> LLMProvider:
    """A mock LLM provider that returns a stop response by default."""
    llm = AsyncMock(spec=["complete", "stream", "close"])
    llm.complete.return_value = LLMResponse(
        content="Task completed successfully.",
        model="test-model",
        finish_reason="stop",
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    return llm


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """A fully populated tool registry with all built-in tools."""
    return create_default_registry()
