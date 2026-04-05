"""Unit tests for extension prompt injection into system prompts (#128)."""

from __future__ import annotations

from agent_forge.agent.prompts import (
    build_hosted_poa_system_prompt,
    build_system_prompt,
)

# ---------------------------------------------------------------------------
# build_system_prompt with extension_prompts
# ---------------------------------------------------------------------------


class TestBuildSystemPromptExtensions:
    """Test extension prompt injection in build_system_prompt."""

    def test_no_extension_prompts(self) -> None:
        """Without extension_prompts, no Extension Prompts section appears."""
        prompt = build_system_prompt("do a task", [])
        assert "## Extension Prompts" not in prompt

    def test_with_extension_prompts(self) -> None:
        """Extension prompts are appended in a dedicated section."""
        fragments = [
            "Always check for reentrancy.",
            "Report all access-control issues.",
        ]
        prompt = build_system_prompt("do a task", [], extension_prompts=fragments)
        assert "## Extension Prompts" in prompt
        assert "Always check for reentrancy." in prompt
        assert "Report all access-control issues." in prompt

    def test_with_scope_and_extensions(self) -> None:
        """Both prompt_scope and extension_prompts appear in order."""
        prompt = build_system_prompt(
            "do a task",
            [],
            prompt_scope="Focus on Solidity.",
            extension_prompts=["Check CEI pattern."],
        )
        scope_idx = prompt.index("## Profile Scope")
        ext_idx = prompt.index("## Extension Prompts")
        # Extension prompts come after profile scope
        assert ext_idx > scope_idx
        assert "Focus on Solidity." in prompt
        assert "Check CEI pattern." in prompt

    def test_empty_list_no_section(self) -> None:
        """An empty extension_prompts list does not add a section."""
        prompt = build_system_prompt("do a task", [], extension_prompts=[])
        assert "## Extension Prompts" not in prompt

    def test_none_no_section(self) -> None:
        """None extension_prompts does not add a section."""
        prompt = build_system_prompt("do a task", [], extension_prompts=None)
        assert "## Extension Prompts" not in prompt


# ---------------------------------------------------------------------------
# build_hosted_poa_system_prompt with extension_prompts
# ---------------------------------------------------------------------------


class TestBuildHostedPoaPromptExtensions:
    """Test extension prompt injection in build_hosted_poa_system_prompt."""

    def test_no_extension_prompts(self) -> None:
        """Without extension_prompts, no Extension Prompts section appears."""
        prompt = build_hosted_poa_system_prompt("audit task", [])
        assert "## Extension Prompts" not in prompt

    def test_with_extension_prompts(self) -> None:
        """Extension prompts are appended in a dedicated section."""
        prompt = build_hosted_poa_system_prompt(
            "audit task",
            [],
            extension_prompts=["Check for flash-loan attacks."],
        )
        assert "## Extension Prompts" in prompt
        assert "Check for flash-loan attacks." in prompt

    def test_with_scope_and_extensions(self) -> None:
        """Both prompt_scope and extension_prompts appear in the hosted prompt."""
        prompt = build_hosted_poa_system_prompt(
            "audit task",
            [],
            prompt_scope="Audit Solidity contracts.",
            extension_prompts=["Verify CEI pattern."],
        )
        assert "## Profile Scope" in prompt
        assert "## Extension Prompts" in prompt
        assert "Audit Solidity contracts." in prompt
        assert "Verify CEI pattern." in prompt
