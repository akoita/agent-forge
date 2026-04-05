"""Unit tests for the agent profile system (issue #117)."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from agent_forge.profiles.profile import (
    AgentProfile,
    _load_yaml_file,
    get_profile,
    load_profiles,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# AgentProfile model
# ---------------------------------------------------------------------------


class TestAgentProfileModel:
    """Test the Pydantic model itself."""

    def test_minimal_valid(self):
        profile = AgentProfile(id="test", name="Test Profile")
        assert profile.id == "test"
        assert profile.name == "Test Profile"
        assert profile.description == ""
        assert profile.prompt_scope == ""
        assert profile.llm_provider is None
        assert profile.llm_model is None
        assert profile.max_iterations is None

    def test_full_fields(self):
        profile = AgentProfile(
            id="deep",
            name="Deep",
            description="A deep analysis profile.",
            prompt_scope="Focus on everything.",
            llm_provider="gemini",
            llm_model="gemini-3-pro",
            max_iterations=50,
        )
        assert profile.llm_provider == "gemini"
        assert profile.llm_model == "gemini-3-pro"
        assert profile.max_iterations == 50
        assert profile.description == "A deep analysis profile."

    def test_max_iterations_must_be_positive(self):
        with pytest.raises(ValidationError, match="greater_than_equal"):
            AgentProfile(id="x", name="X", max_iterations=0)


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


class TestYamlLoading:
    """Test loading profiles from YAML files."""

    def test_load_valid_yaml(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            id: my-profile
            name: My Profile
            prompt_scope: "Focus on refactoring."
        """)
        profile_file = tmp_path / "my-profile.yaml"
        profile_file.write_text(yaml_content)

        profile = _load_yaml_file(profile_file)
        assert profile.id == "my-profile"
        assert profile.prompt_scope == "Focus on refactoring."

    def test_load_yaml_with_llm_overrides(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            id: llm-deep
            name: LLM Deep
            llm_provider: openai
            llm_model: gpt-4o
            max_iterations: 30
        """)
        profile_file = tmp_path / "llm-deep.yaml"
        profile_file.write_text(yaml_content)

        profile = _load_yaml_file(profile_file)
        assert profile.llm_provider == "openai"
        assert profile.llm_model == "gpt-4o"
        assert profile.max_iterations == 30

    def test_load_yaml_missing_required_field(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            description: "Missing id and name"
        """)
        profile_file = tmp_path / "broken.yaml"
        profile_file.write_text(yaml_content)

        with pytest.raises(ValidationError):
            _load_yaml_file(profile_file)

    def test_load_yaml_non_mapping(self, tmp_path: Path):
        profile_file = tmp_path / "bad.yaml"
        profile_file.write_text("- just\n- a\n- list\n")

        with pytest.raises(ValueError, match="YAML mapping"):
            _load_yaml_file(profile_file)


# ---------------------------------------------------------------------------
# Profile registry loading
# ---------------------------------------------------------------------------


class TestLoadProfiles:
    """Test the multi-directory profile loader."""

    def test_load_builtins(self):
        """Built-in profiles shipped with the package should load."""
        registry = load_profiles()
        assert "gemini" in registry
        assert "openai" in registry
        assert "thorough" in registry
        assert len(registry) >= 3

    def test_builtin_profiles_have_correct_shape(self):
        registry = load_profiles()
        for profile in registry.values():
            assert isinstance(profile, AgentProfile)
            assert profile.id
            assert profile.name

    def test_user_dir_overrides_builtin(self, tmp_path: Path):
        """User-provided profiles with the same ID should override builtins."""
        yaml_content = textwrap.dedent("""\
            id: thorough
            name: Custom Thorough
            prompt_scope: "Custom scope."
        """)
        (tmp_path / "thorough.yaml").write_text(yaml_content)

        registry = load_profiles([tmp_path])
        profile = registry["thorough"]
        assert profile.name == "Custom Thorough"
        assert profile.prompt_scope == "Custom scope."

    def test_extra_user_profiles(self, tmp_path: Path):
        """User-provided profiles should be added to the registry."""
        yaml_content = textwrap.dedent("""\
            id: custom-profile
            name: Custom Profile
            prompt_scope: "Only custom."
        """)
        (tmp_path / "custom.yaml").write_text(yaml_content)

        registry = load_profiles([tmp_path])
        assert "custom-profile" in registry
        # Builtins should still be present
        assert "gemini" in registry

    def test_skip_non_yaml_files(self, tmp_path: Path):
        (tmp_path / "readme.txt").write_text("not a profile")
        (tmp_path / "data.json").write_text("{}")
        registry = load_profiles([tmp_path], include_builtins=False)
        assert len(registry) == 0

    def test_nonexistent_dir_is_skipped(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist"
        registry = load_profiles([missing], include_builtins=False)
        assert len(registry) == 0

    def test_no_builtins_flag(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            id: solo
            name: Solo
        """)
        (tmp_path / "solo.yaml").write_text(yaml_content)

        registry = load_profiles([tmp_path], include_builtins=False)
        assert "solo" in registry
        assert "gemini" not in registry  # builtins excluded


# ---------------------------------------------------------------------------
# Profile lookup
# ---------------------------------------------------------------------------


class TestGetProfile:
    """Test profile ID lookup."""

    def test_found(self):
        registry = load_profiles()
        profile = get_profile("gemini", registry)
        assert profile.id == "gemini"

    def test_not_found(self):
        registry = load_profiles()
        with pytest.raises(KeyError, match="unknown profile"):
            get_profile("nonexistent-profile", registry)

    def test_error_lists_available(self):
        registry = load_profiles()
        with pytest.raises(KeyError) as exc_info:
            get_profile("nope", registry)
        error_msg = str(exc_info.value)
        assert "gemini" in error_msg
        assert "thorough" in error_msg


# ---------------------------------------------------------------------------
# Prompt scoping integration
# ---------------------------------------------------------------------------


class TestPromptScopeIntegration:
    """Test that profiles correctly scope system prompts."""

    def test_system_prompt_includes_scope(self):
        from agent_forge.agent.prompts import build_system_prompt

        prompt = build_system_prompt(
            "Review this code",
            [],
            prompt_scope="Focus exclusively on performance bottlenecks.",
        )
        assert "## Profile Scope" in prompt
        assert "Focus exclusively on performance bottlenecks." in prompt

    def test_system_prompt_no_scope_when_none(self):
        from agent_forge.agent.prompts import build_system_prompt

        prompt = build_system_prompt("Review this code", [])
        assert "## Profile Scope" not in prompt

    def test_hosted_prompt_includes_scope(self):
        from agent_forge.agent.prompts import build_hosted_poa_system_prompt

        prompt = build_hosted_poa_system_prompt(
            "Analyze this contract",
            [],
            prompt_scope="Perform a comprehensive analysis.",
        )
        assert "## Profile Scope" in prompt
        assert "Perform a comprehensive analysis." in prompt

    def test_hosted_prompt_no_scope_when_none(self):
        from agent_forge.agent.prompts import build_hosted_poa_system_prompt

        prompt = build_hosted_poa_system_prompt("Analyze this contract", [])
        assert "## Profile Scope" not in prompt


# ---------------------------------------------------------------------------
# CLI output payload integration
# ---------------------------------------------------------------------------


class TestCLIOutputPayload:
    """Test that profile metadata appears in the run output payload."""

    def test_payload_includes_profile_metadata(self):
        from unittest.mock import MagicMock

        from agent_forge.cli import _run_output_payload

        mock_run = MagicMock()
        mock_run.id = "run-123"
        mock_run.state.value = "completed"
        mock_run.task = "test task"
        mock_run.repo_path = "/tmp/repo"
        mock_run.iterations = 5
        mock_run.created_at.isoformat.return_value = "2026-01-01T00:00:00"
        mock_run.completed_at.isoformat.return_value = "2026-01-01T00:01:00"
        mock_run.completed_at.__bool__ = lambda _: True
        mock_run.completed_at.__sub__ = lambda _, _other: MagicMock(total_seconds=lambda: 60.0)
        mock_run.error = None
        mock_run.total_tokens.prompt_tokens = 100
        mock_run.total_tokens.completion_tokens = 50
        mock_run.total_tokens.total_tokens = 150

        profile = AgentProfile(
            id="thorough",
            name="Thorough Analysis",
            description="Extended iteration analysis.",
        )

        payload = _run_output_payload(mock_run, profile=profile)
        assert "profile" in payload
        assert payload["profile"]["id"] == "thorough"
        assert payload["profile"]["name"] == "Thorough Analysis"
        assert payload["profile"]["description"] == "Extended iteration analysis."

    def test_payload_without_profile(self):
        from unittest.mock import MagicMock

        from agent_forge.cli import _run_output_payload

        mock_run = MagicMock()
        mock_run.id = "run-456"
        mock_run.state.value = "completed"
        mock_run.task = "test task"
        mock_run.repo_path = "/tmp/repo"
        mock_run.iterations = 3
        mock_run.created_at.isoformat.return_value = "2026-01-01T00:00:00"
        mock_run.completed_at = None
        mock_run.error = None
        mock_run.total_tokens.prompt_tokens = 50
        mock_run.total_tokens.completion_tokens = 25
        mock_run.total_tokens.total_tokens = 75

        payload = _run_output_payload(mock_run)
        assert "profile" not in payload
