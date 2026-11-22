"""Unit tests for release version parity and ``scripts/check_release.py``."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from agent_forge import __version__
from agent_forge.config import ForgeConfig
from agent_forge.service.app import create_app

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_release.py"


def _load_module() -> ModuleType:
    """Import ``check_release.py`` as a module from its file path."""
    spec = importlib.util.spec_from_file_location("check_release", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr: ModuleType = _load_module()


def test_package_version_matches_across_repo(tmp_path: Path) -> None:
    pyproject_data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_version = pyproject_data["project"]["version"]
    app = create_app(service_root=tmp_path, config=ForgeConfig())

    assert pyproject_version == __version__
    assert app.version == __version__


def test_check_release_succeeds_for_repo_and_matching_tag() -> None:
    assert cr.main([]) == 0
    assert cr.main(["--tag", f"v{__version__}"]) == 0


def test_check_release_rejects_mismatched_and_unprefixed_tags() -> None:
    assert cr.main(["--tag", "v0.0.0"]) == 1
    assert cr.main(["--tag", __version__]) == 1


def test_check_release_rejects_version_drift_between_files(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    init_file = tmp_path / "__init__.py"
    pyproject.write_text('[project]\nname = "agent-forge"\nversion = "0.3.0"\n', encoding="utf-8")
    init_file.write_text('__version__ = "0.2.0"\n', encoding="utf-8")

    assert cr.main(["--pyproject", str(pyproject), "--init", str(init_file)]) == 1
