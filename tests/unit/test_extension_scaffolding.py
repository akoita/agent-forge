"""Unit tests for the extension scaffolding system (#120)."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

import pytest
import yaml

from agent_forge.extensions.scaffolding import (
    ScaffoldError,
    _to_class_prefix,
    _to_package_name,
    scaffold_extension,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------


class TestNameNormalization:
    """Test extension name → package/class conversions."""

    def test_to_package_name_basic(self) -> None:
        assert _to_package_name("my-extension") == "my_extension"

    def test_to_package_name_with_multiple_separators(self) -> None:
        assert _to_package_name("my-cool--extension") == "my_cool_extension"

    def test_to_package_name_uppercase(self) -> None:
        assert _to_package_name("MyExtension") == "myextension"

    def test_to_package_name_mixed(self) -> None:
        assert _to_package_name("My-Cool_Extension") == "my_cool_extension"

    def test_to_class_prefix_basic(self) -> None:
        assert _to_class_prefix("my-extension") == "MyExtension"

    def test_to_class_prefix_single_word(self) -> None:
        assert _to_class_prefix("scanner") == "Scanner"

    def test_to_class_prefix_underscores(self) -> None:
        assert _to_class_prefix("web_security_scanner") == "WebSecurityScanner"


# ---------------------------------------------------------------------------
# scaffold_extension
# ---------------------------------------------------------------------------


class TestScaffoldExtension:
    """Test the scaffold_extension function."""

    def test_creates_directory_structure(self, tmp_path: Path) -> None:
        """Check that all expected files and directories are created."""
        result = scaffold_extension("my-scanner", target_dir=tmp_path)

        assert result == tmp_path / "my-scanner"
        assert result.is_dir()

        pkg = result / "my_scanner"
        assert pkg.is_dir()
        assert (pkg / "__init__.py").is_file()
        assert (pkg / "profiles").is_dir()
        assert (pkg / "profiles" / "default.yaml").is_file()
        assert (pkg / "tools").is_dir()
        assert (pkg / "tools" / "__init__.py").is_file()
        assert (pkg / "tools" / "sample_tool.py").is_file()
        assert (result / "pyproject.toml").is_file()
        assert (result / "Dockerfile").is_file()
        assert (result / "README.md").is_file()
        assert (result / "tests").is_dir()
        assert (result / "tests" / "test_sample_tool.py").is_file()

    def test_pyproject_has_entry_points(self, tmp_path: Path) -> None:
        """Generated pyproject.toml should have the correct entry_points."""
        scaffold_extension("test-ext", target_dir=tmp_path)

        pyproject_path = tmp_path / "test-ext" / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        eps = data["project"]["entry-points"]
        assert "agent_forge.extensions" in eps
        assert "test-ext" in eps["agent_forge.extensions"]
        assert "agent_forge.profiles" in eps
        assert "test-ext" in eps["agent_forge.profiles"]

    def test_sample_profile_is_valid_yaml(self, tmp_path: Path) -> None:
        """The generated profile YAML should be parseable."""
        scaffold_extension("yaml-test", target_dir=tmp_path)

        profile_path = tmp_path / "yaml-test" / "yaml_test" / "profiles" / "default.yaml"
        with open(profile_path) as f:
            data = yaml.safe_load(f)

        assert isinstance(data, dict)
        assert "id" in data
        assert "name" in data
        assert "prompt_scope" in data

    def test_sample_profile_loads_as_agent_profile(self, tmp_path: Path) -> None:
        """The generated profile should validate as an AgentProfile."""
        from agent_forge.profiles.profile import AgentProfile

        scaffold_extension("profile-test", target_dir=tmp_path)

        profile_path = tmp_path / "profile-test" / "profile_test" / "profiles" / "default.yaml"
        with open(profile_path) as f:
            data = yaml.safe_load(f)

        profile = AgentProfile.model_validate(data)
        assert profile.id == "profile_test-default"

    def test_refuses_existing_directory(self, tmp_path: Path) -> None:
        """Should raise if the target directory already exists."""
        (tmp_path / "existing").mkdir()

        with pytest.raises(ScaffoldError, match="already exists"):
            scaffold_extension("existing", target_dir=tmp_path)

    def test_refuses_invalid_name(self, tmp_path: Path) -> None:
        """Should raise on invalid extension names."""
        with pytest.raises(ScaffoldError, match="Invalid extension name"):
            scaffold_extension("123-bad", target_dir=tmp_path)

        with pytest.raises(ScaffoldError, match="Invalid extension name"):
            scaffold_extension("", target_dir=tmp_path)

    def test_readme_contains_extension_name(self, tmp_path: Path) -> None:
        """README should reference the extension name."""
        scaffold_extension("cool-ext", target_dir=tmp_path)

        readme = (tmp_path / "cool-ext" / "README.md").read_text()
        assert "cool-ext" in readme

    def test_init_contains_extension_info(self, tmp_path: Path) -> None:
        """The __init__.py should contain ExtensionInfo with the correct name."""
        scaffold_extension("info-ext", target_dir=tmp_path)

        init = (tmp_path / "info-ext" / "info_ext" / "__init__.py").read_text()
        assert 'name="info-ext"' in init
        assert "PROFILES_DIR" in init

    def test_tool_template_has_correct_class_name(self, tmp_path: Path) -> None:
        """The sample tool should have a CamelCase class name."""
        scaffold_extension("my-tool-ext", target_dir=tmp_path)

        tool = (tmp_path / "my-tool-ext" / "my_tool_ext" / "tools" / "sample_tool.py").read_text()
        assert "class MyToolExtSampleTool(Tool):" in tool
        assert 'return "my_tool_ext_sample"' in tool

    def test_default_target_dir_is_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without target_dir, scaffolds in cwd."""
        monkeypatch.chdir(tmp_path)
        result = scaffold_extension("cwd-test")
        assert result == tmp_path / "cwd-test"
        assert result.is_dir()

    def test_creates_prompts_directory(self, tmp_path: Path) -> None:
        """Scaffolded extension should have a prompts/ directory with a sample."""
        scaffold_extension("prompt-ext", target_dir=tmp_path)
        pkg = tmp_path / "prompt-ext" / "prompt_ext"
        assert (pkg / "prompts").is_dir()
        assert (pkg / "prompts" / "system_prompt.md").is_file()
        content = (pkg / "prompts" / "system_prompt.md").read_text()
        assert "prompt-ext" in content

    def test_creates_workflows_directory(self, tmp_path: Path) -> None:
        """Scaffolded extension should have a workflows/ directory with a sample."""
        scaffold_extension("wf-ext", target_dir=tmp_path)
        pkg = tmp_path / "wf-ext" / "wf_ext"
        assert (pkg / "workflows").is_dir()
        assert (pkg / "workflows" / "sample-workflow.md").is_file()
        content = (pkg / "workflows" / "sample-workflow.md").read_text()
        assert "wf-ext" in content

    def test_pyproject_has_prompts_entry_point(self, tmp_path: Path) -> None:
        """Generated pyproject.toml should include agent_forge.prompts."""
        scaffold_extension("pe-test", target_dir=tmp_path)
        pyproject_path = tmp_path / "pe-test" / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        eps = data["project"]["entry-points"]
        assert "agent_forge.prompts" in eps
        assert "pe-test" in eps["agent_forge.prompts"]

    def test_pyproject_has_workflows_entry_point(self, tmp_path: Path) -> None:
        """Generated pyproject.toml should include agent_forge.workflows."""
        scaffold_extension("we-test", target_dir=tmp_path)
        pyproject_path = tmp_path / "we-test" / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        eps = data["project"]["entry-points"]
        assert "agent_forge.workflows" in eps
        assert "we-test" in eps["agent_forge.workflows"]

    def test_init_contains_prompts_and_workflows(self, tmp_path: Path) -> None:
        """The __init__.py should export PROMPTS_DIR and WORKFLOWS_DIR."""
        scaffold_extension("init-check", target_dir=tmp_path)
        init = (tmp_path / "init-check" / "init_check" / "__init__.py").read_text()
        assert "PROMPTS_DIR" in init
        assert "WORKFLOWS_DIR" in init

    def test_creates_extension_dockerfile_template(self, tmp_path: Path) -> None:
        """Scaffolded projects should include a hosted-service Dockerfile."""
        scaffold_extension("deploy-ext", target_dir=tmp_path)

        dockerfile = (tmp_path / "deploy-ext" / "Dockerfile").read_text()
        assert "ARG AGENT_FORGE_IMAGE=agent-forge-service:latest" in dockerfile
        assert "FROM ${AGENT_FORGE_IMAGE}" in dockerfile
        assert "RUN pip install --no-cache-dir ." in dockerfile
        assert "RUN agent-forge extensions list" in dockerfile
