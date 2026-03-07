"""Shared test fixtures for Agent Forge."""

import pytest


@pytest.fixture
def sample_repo_path() -> str:
    """Path to the sample repository used in E2E tests."""
    return "tests/fixtures/sample_repo"
