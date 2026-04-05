"""Unit tests for the extension CLI commands (#120)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from click.testing import CliRunner

from agent_forge.cli import main
from agent_forge.extensions.discovery import ExtensionInfo

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# agent-forge extensions list
# ---------------------------------------------------------------------------


class TestExtensionsList:
    """Test the ``agent-forge extensions list`` CLI command."""

    def test_no_extensions(self) -> None:
        """Empty extensions → informative message."""
        runner = CliRunner()

        with (
            patch(
                "agent_forge.extensions.discovery.discover_extensions",
                return_value=[],
            ),
            patch(
                "agent_forge.extensions.discovery.discover_extension_profile_dirs",
                return_value=[],
            ),
            patch(
                "agent_forge.tools.plugins.discover_tool_plugins",
                return_value=[],
            ),
        ):
            result = runner.invoke(main, ["extensions", "list"])

        assert result.exit_code == 0
        assert "No extensions installed" in result.output

    def test_with_extensions(self) -> None:
        """Installed extensions are rendered in a table."""
        runner = CliRunner()

        ext = ExtensionInfo(
            name="proof-of-audit",
            version="0.1.0",
            description="Smart contract audit tools",
            profiles=["full-spectrum", "reentrancy-only"],
            tools=["challenge_tool"],
        )

        with (
            patch(
                "agent_forge.extensions.discovery.discover_extensions",
                return_value=[ext],
            ),
            patch(
                "agent_forge.extensions.discovery.discover_extension_profile_dirs",
                return_value=[],
            ),
            patch(
                "agent_forge.tools.plugins.discover_tool_plugins",
                return_value=[],
            ),
        ):
            result = runner.invoke(main, ["extensions", "list"])

        assert result.exit_code == 0
        assert "proof-of-audit" in result.output
        assert "0.1.0" in result.output

    def test_help(self) -> None:
        """extensions list --help should work."""
        runner = CliRunner()
        result = runner.invoke(main, ["extensions", "list", "--help"])
        assert result.exit_code == 0
        assert "List installed" in result.output


# ---------------------------------------------------------------------------
# agent-forge init-extension
# ---------------------------------------------------------------------------


class TestInitExtension:
    """Test the ``agent-forge init-extension`` CLI command."""

    def test_scaffold_success(self, tmp_path: Path) -> None:
        """Creates extension project and shows next steps."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init-extension", "my-test-ext", "--target-dir", str(tmp_path)],
        )

        assert result.exit_code == 0
        assert "Extension project created" in result.output
        assert "my-test-ext" in result.output
        assert "Next steps" in result.output

        # Verify project was actually created
        assert (tmp_path / "my-test-ext").is_dir()
        assert (tmp_path / "my-test-ext" / "pyproject.toml").is_file()

    def test_scaffold_existing_dir_fails(self, tmp_path: Path) -> None:
        """Refuses to overwrite an existing directory."""
        (tmp_path / "existing-ext").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init-extension", "existing-ext", "--target-dir", str(tmp_path)],
        )

        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_scaffold_invalid_name_fails(self, tmp_path: Path) -> None:
        """Rejects invalid extension names."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init-extension", "123-bad", "--target-dir", str(tmp_path)],
        )

        assert result.exit_code != 0
        assert "Invalid extension name" in result.output

    def test_help(self) -> None:
        """init-extension --help should work."""
        runner = CliRunner()
        result = runner.invoke(main, ["init-extension", "--help"])
        assert result.exit_code == 0
        assert "Scaffold a new" in result.output
