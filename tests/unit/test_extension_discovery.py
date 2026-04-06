"""Unit tests for the extension discovery system (#120, #128)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from agent_forge.extensions.discovery import (
    EXTENSION_PLUGIN_GROUP,
    PROFILE_PLUGIN_GROUP,
    PROMPT_PLUGIN_GROUP,
    WORKFLOW_PLUGIN_GROUP,
    ExtensionInfo,
    discover_extension_profile_dirs,
    discover_extension_prompt_fragments,
    discover_extension_workflow_dirs,
    discover_extensions,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_ep(
    name: str,
    load_return: object | None = None,
    load_side_effect: Exception | None = None,
) -> MagicMock:
    """Create a mock EntryPoint with configurable load()."""
    ep = MagicMock()
    ep.name = name
    ep.value = f"{name}:obj"
    if load_side_effect is not None:
        ep.load.side_effect = load_side_effect
    elif load_return is not None:
        ep.load.return_value = load_return
    return ep


# ---------------------------------------------------------------------------
# discover_extensions
# ---------------------------------------------------------------------------


class TestDiscoverExtensions:
    """Test discovery of installed extensions."""

    def test_discover_empty(self) -> None:
        """No extensions installed → empty list."""

        def empty_factory(group: str) -> list[MagicMock]:
            return []

        result = discover_extensions(entry_points_factory=empty_factory)
        assert result == []

    def test_discover_from_entry_points(self) -> None:
        """Extensions are loaded from entry_points."""
        ext_info = ExtensionInfo(
            name="my-ext",
            version="1.0.0",
            description="Test extension",
            package="my-ext-pkg",
            profiles=["profile-a"],
            tools=["tool-a"],
        )

        ep = _mock_ep("my-ext", load_return=ext_info)

        def factory(group: str) -> list[MagicMock]:
            if group == EXTENSION_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extensions(entry_points_factory=factory)

        assert len(result) == 1
        assert result[0].name == "my-ext"
        assert result[0].version == "1.0.0"
        assert result[0].profiles == ["profile-a"]
        assert result[0].tools == ["tool-a"]

    def test_discover_from_callable_entry_point(self) -> None:
        """Entry point can resolve to a callable returning ExtensionInfo."""
        ext_info = ExtensionInfo(name="callable-ext", version="0.1.0")

        def ext_factory() -> ExtensionInfo:
            return ext_info

        ep = _mock_ep("callable-ext", load_return=ext_factory)

        def factory(group: str) -> list[MagicMock]:
            if group == EXTENSION_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extensions(entry_points_factory=factory)

        assert len(result) == 1
        assert result[0].name == "callable-ext"

    def test_discover_handles_load_error(self) -> None:
        """Broken entry points are skipped with a warning."""
        ep = _mock_ep("broken", load_side_effect=ImportError("no module"))

        def factory(group: str) -> list[MagicMock]:
            if group == EXTENSION_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extensions(entry_points_factory=factory)
        assert result == []

    def test_discover_handles_invalid_type(self) -> None:
        """Entry point resolving to wrong type is skipped."""
        ep = _mock_ep("wrong", load_return="not an ExtensionInfo")

        def factory(group: str) -> list[MagicMock]:
            if group == EXTENSION_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extensions(entry_points_factory=factory)
        assert result == []

    def test_discover_sorts_by_name(self) -> None:
        """Extensions are returned sorted by name."""
        ext_b = ExtensionInfo(name="b-ext")
        ext_a = ExtensionInfo(name="a-ext")

        ep_b = _mock_ep("b-ext", load_return=ext_b)
        ep_a = _mock_ep("a-ext", load_return=ext_a)

        def factory(group: str) -> list[MagicMock]:
            if group == EXTENSION_PLUGIN_GROUP:
                return [ep_b, ep_a]
            return []

        result = discover_extensions(entry_points_factory=factory)
        assert [r.name for r in result] == ["a-ext", "b-ext"]

    def test_discover_multiple_extensions(self) -> None:
        """Multiple extensions can be discovered."""
        ext1 = ExtensionInfo(name="ext-1", version="1.0")
        ext2 = ExtensionInfo(name="ext-2", version="2.0")

        ep1 = _mock_ep("ext-1", load_return=ext1)
        ep2 = _mock_ep("ext-2", load_return=ext2)

        def factory(group: str) -> list[MagicMock]:
            if group == EXTENSION_PLUGIN_GROUP:
                return [ep1, ep2]
            return []

        result = discover_extensions(entry_points_factory=factory)
        assert len(result) == 2

    def test_extension_info_has_prompts_and_workflows(self) -> None:
        """ExtensionInfo supports prompts and workflows fields."""
        info = ExtensionInfo(
            name="full-ext",
            prompts=["sys_prompt"],
            workflows=["audit-flow"],
        )
        assert info.prompts == ["sys_prompt"]
        assert info.workflows == ["audit-flow"]


# ---------------------------------------------------------------------------
# discover_extension_profile_dirs
# ---------------------------------------------------------------------------


class TestDiscoverExtensionProfileDirs:
    """Test discovery of profile directories from extensions."""

    def test_discover_empty(self) -> None:
        """No profile entry_points → empty list."""

        def factory(group: str) -> list[MagicMock]:
            return []

        result = discover_extension_profile_dirs(entry_points_factory=factory)
        assert result == []

    def test_discover_valid_path(self, tmp_path: Path) -> None:
        """A Path entry_point pointing to an existing directory is returned."""
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()

        ep = _mock_ep("my-ext", load_return=profiles_dir)

        def factory(group: str) -> list[MagicMock]:
            if group == PROFILE_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_profile_dirs(entry_points_factory=factory)
        assert result == [profiles_dir]

    def test_discover_skips_nonexistent_dir(self, tmp_path: Path) -> None:
        """A Path pointing to a non-directory is skipped."""
        missing = tmp_path / "missing"
        ep = _mock_ep("broken", load_return=missing)

        def factory(group: str) -> list[MagicMock]:
            if group == PROFILE_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_profile_dirs(entry_points_factory=factory)
        assert result == []

    def test_discover_skips_non_path(self) -> None:
        """An entry_point resolving to a non-Path is skipped."""
        ep = _mock_ep("bad", load_return="not/a/path")

        def factory(group: str) -> list[MagicMock]:
            if group == PROFILE_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_profile_dirs(entry_points_factory=factory)
        assert result == []

    def test_discover_handles_load_error(self) -> None:
        """Broken entry_points are skipped."""
        ep = _mock_ep("crash", load_side_effect=ImportError("fail"))

        def factory(group: str) -> list[MagicMock]:
            if group == PROFILE_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_profile_dirs(entry_points_factory=factory)
        assert result == []

    def test_discover_multiple_dirs(self, tmp_path: Path) -> None:
        """Multiple profile directories from different extensions."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        ep_a = _mock_ep("ext-a", load_return=dir_a)
        ep_b = _mock_ep("ext-b", load_return=dir_b)

        def factory(group: str) -> list[MagicMock]:
            if group == PROFILE_PLUGIN_GROUP:
                return [ep_a, ep_b]
            return []

        result = discover_extension_profile_dirs(entry_points_factory=factory)
        assert dir_a in result
        assert dir_b in result


# ---------------------------------------------------------------------------
# discover_extension_prompt_fragments
# ---------------------------------------------------------------------------


class TestDiscoverExtensionPromptFragments:
    """Test discovery of prompt fragments from extensions."""

    def test_discover_empty(self) -> None:
        """No prompt entry_points → empty list."""

        def factory(group: str) -> list[MagicMock]:
            return []

        result = discover_extension_prompt_fragments(entry_points_factory=factory)
        assert result == []

    def test_discover_from_string(self) -> None:
        """A string entry_point is returned directly."""
        fragment = "Always check for reentrancy vulnerabilities."
        ep = _mock_ep("my-ext", load_return=fragment)

        def factory(group: str) -> list[MagicMock]:
            if group == PROMPT_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_prompt_fragments(entry_points_factory=factory)
        assert result == [fragment]

    def test_discover_from_path(self, tmp_path: Path) -> None:
        """A Path entry_point reads the file content."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Check access controls.", encoding="utf-8")

        ep = _mock_ep("my-ext", load_return=prompt_file)

        def factory(group: str) -> list[MagicMock]:
            if group == PROMPT_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_prompt_fragments(entry_points_factory=factory)
        assert result == ["Check access controls."]

    def test_discover_from_prompt_directory(self, tmp_path: Path) -> None:
        """A Path entry_point can point to a directory of markdown fragments."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "01_scope.md").write_text("Check access controls.", encoding="utf-8")
        (prompts_dir / "02_style.md").write_text(
            "Prefer concrete remediation steps.",
            encoding="utf-8",
        )

        ep = _mock_ep("my-ext", load_return=prompts_dir)

        def factory(group: str) -> list[MagicMock]:
            if group == PROMPT_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_prompt_fragments(entry_points_factory=factory)
        assert result == ["Check access controls.\n\nPrefer concrete remediation steps."]

    def test_discover_from_callable(self) -> None:
        """A callable entry_point returning a string is resolved."""

        def prompt_factory() -> str:
            return "Dynamic prompt fragment"

        ep = _mock_ep("my-ext", load_return=prompt_factory)

        def factory(group: str) -> list[MagicMock]:
            if group == PROMPT_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_prompt_fragments(entry_points_factory=factory)
        assert result == ["Dynamic prompt fragment"]

    def test_discover_handles_load_error(self) -> None:
        """Broken entry_points are skipped."""
        ep = _mock_ep("crash", load_side_effect=ImportError("fail"))

        def factory(group: str) -> list[MagicMock]:
            if group == PROMPT_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_prompt_fragments(entry_points_factory=factory)
        assert result == []

    def test_discover_skips_missing_file(self, tmp_path: Path) -> None:
        """A Path to a non-existent file is skipped."""
        missing = tmp_path / "missing.md"
        ep = _mock_ep("bad", load_return=missing)

        def factory(group: str) -> list[MagicMock]:
            if group == PROMPT_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_prompt_fragments(entry_points_factory=factory)
        assert result == []

    def test_discover_skips_invalid_type(self) -> None:
        """An entry_point resolving to wrong type is skipped."""
        ep = _mock_ep("bad", load_return=42)

        def factory(group: str) -> list[MagicMock]:
            if group == PROMPT_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_prompt_fragments(entry_points_factory=factory)
        assert result == []

    def test_discover_multiple_fragments(self) -> None:
        """Multiple prompt fragments from different extensions."""
        ep_a = _mock_ep("ext-a", load_return="Fragment A")
        ep_b = _mock_ep("ext-b", load_return="Fragment B")

        def factory(group: str) -> list[MagicMock]:
            if group == PROMPT_PLUGIN_GROUP:
                return [ep_a, ep_b]
            return []

        result = discover_extension_prompt_fragments(entry_points_factory=factory)
        assert "Fragment A" in result
        assert "Fragment B" in result


# ---------------------------------------------------------------------------
# discover_extension_workflow_dirs
# ---------------------------------------------------------------------------


class TestDiscoverExtensionWorkflowDirs:
    """Test discovery of workflow directories from extensions."""

    def test_discover_empty(self) -> None:
        """No workflow entry_points → empty list."""

        def factory(group: str) -> list[MagicMock]:
            return []

        result = discover_extension_workflow_dirs(entry_points_factory=factory)
        assert result == []

    def test_discover_valid_path(self, tmp_path: Path) -> None:
        """A Path entry_point pointing to an existing directory is returned."""
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()

        ep = _mock_ep("my-ext", load_return=workflows_dir)

        def factory(group: str) -> list[MagicMock]:
            if group == WORKFLOW_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_workflow_dirs(entry_points_factory=factory)
        assert result == [workflows_dir]

    def test_discover_skips_nonexistent_dir(self, tmp_path: Path) -> None:
        """A Path pointing to a non-directory is skipped."""
        missing = tmp_path / "missing"
        ep = _mock_ep("broken", load_return=missing)

        def factory(group: str) -> list[MagicMock]:
            if group == WORKFLOW_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_workflow_dirs(entry_points_factory=factory)
        assert result == []

    def test_discover_skips_non_path(self) -> None:
        """An entry_point resolving to a non-Path is skipped."""
        ep = _mock_ep("bad", load_return="not/a/path")

        def factory(group: str) -> list[MagicMock]:
            if group == WORKFLOW_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_workflow_dirs(entry_points_factory=factory)
        assert result == []

    def test_discover_handles_load_error(self) -> None:
        """Broken entry_points are skipped."""
        ep = _mock_ep("crash", load_side_effect=ImportError("fail"))

        def factory(group: str) -> list[MagicMock]:
            if group == WORKFLOW_PLUGIN_GROUP:
                return [ep]
            return []

        result = discover_extension_workflow_dirs(entry_points_factory=factory)
        assert result == []

    def test_discover_multiple_dirs(self, tmp_path: Path) -> None:
        """Multiple workflow directories from different extensions."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        ep_a = _mock_ep("ext-a", load_return=dir_a)
        ep_b = _mock_ep("ext-b", load_return=dir_b)

        def factory(group: str) -> list[MagicMock]:
            if group == WORKFLOW_PLUGIN_GROUP:
                return [ep_a, ep_b]
            return []

        result = discover_extension_workflow_dirs(entry_points_factory=factory)
        assert dir_a in result
        assert dir_b in result
