"""Integration checks for deployment artifacts introduced in issue #131."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from agent_forge.extensions.scaffolding import scaffold_extension

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _docker_cli_path() -> str | None:
    """Return the absolute Docker CLI path when available."""
    return shutil.which("docker")


def _docker_cli_available() -> bool:
    """Return whether the Docker CLI is available for validation commands."""
    return _docker_cli_path() is not None


def _docker_buildx_available() -> bool:
    """Return whether ``docker buildx`` is available for syntax checks."""
    if not _docker_cli_available():
        return False

    result = subprocess.run(  # noqa: S603
        [_docker_cli_path() or "docker", "buildx", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _copy_file(src: Path, dest: Path) -> None:
    """Copy a single file into a synthetic Docker build context."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _copy_tree(src: Path, dest: Path) -> None:
    """Copy a directory tree into a synthetic Docker build context."""
    shutil.copytree(src, dest, dirs_exist_ok=True)


@pytest.mark.skipif(not _docker_cli_available(), reason="docker CLI is not available")
def test_extensions_compose_override_resolves_with_docker_compose() -> None:
    """The compose override should pass ``docker compose config`` validation."""
    result = subprocess.run(  # noqa: S603
        [
            _docker_cli_path() or "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.extensions.yml",
            "config",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert "agent-reentrancy:" in result.stdout
    assert "agent-access-control:" in result.stdout
    assert "agent-full-spectrum:" in result.stdout


@pytest.mark.skipif(not _docker_buildx_available(), reason="docker buildx is not available")
def test_extension_dockerfile_passes_buildx_syntax_check() -> None:
    """Dockerfile.extension should pass ``docker buildx build --check``."""
    subprocess.run(  # noqa: S603
        [
            _docker_cli_path() or "docker",
            "buildx",
            "build",
            "--check",
            "-f",
            "Dockerfile.extension",
            ".",
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(not _docker_cli_available(), reason="docker CLI is not available")
def test_extension_image_discovers_installed_extension_inside_container(tmp_path: Path) -> None:
    """A built extension image should list an installed extension at runtime."""
    extension_project = scaffold_extension("smoke-ext", target_dir=tmp_path)
    build_context = tmp_path / "docker-context"
    examples_dir = build_context / "examples"
    docker_image = f"agent-forge-extension-smoke:{uuid.uuid4().hex[:12]}"

    for filename in (
        "Dockerfile.extension",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "agent-forge.toml",
    ):
        _copy_file(REPO_ROOT / filename, build_context / filename)

    _copy_tree(REPO_ROOT / "agent_forge", build_context / "agent_forge")
    _copy_tree(REPO_ROOT / "examples", build_context / "examples")
    _copy_tree(extension_project, examples_dir / "smoke-ext")

    try:
        subprocess.run(  # noqa: S603
            [
                _docker_cli_path() or "docker",
                "build",
                "-f",
                "Dockerfile.extension",
                "--build-arg",
                "EXTENSIONS=./examples/smoke-ext",
                "-t",
                docker_image,
                ".",
            ],
            check=True,
            cwd=build_context,
            capture_output=True,
            text=True,
        )

        result = subprocess.run(  # noqa: S603
            [
                _docker_cli_path() or "docker",
                "run",
                "--rm",
                docker_image,
                "agent-forge",
                "extensions",
                "list",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        discovery_check = subprocess.run(  # noqa: S603
            [
                _docker_cli_path() or "docker",
                "run",
                "--rm",
                docker_image,
                "python",
                "-c",
                (
                    "from agent_forge.extensions.discovery import "
                    "discover_extension_prompt_fragments, discover_extension_workflow_dirs, "
                    "discover_extensions; "
                    "from agent_forge.profiles.profile import load_profiles; "
                    "ext = next(ext for ext in discover_extensions() if ext.name == 'smoke-ext'); "
                    "assert 'smoke_ext-default' in ext.profiles; "
                    "assert 'system_prompt' in ext.prompts; "
                    "assert 'sample-workflow' in ext.workflows; "
                    "assert 'smoke_ext-default' in load_profiles(); "
                    "prompts = discover_extension_prompt_fragments(); "
                    "assert any('smoke-ext' in fragment for fragment in prompts); "
                    "workflows = discover_extension_workflow_dirs(); "
                    "assert any((path / 'sample-workflow.md').is_file() for path in workflows)"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        subprocess.run(  # noqa: S603
            [_docker_cli_path() or "docker", "image", "rm", "-f", docker_image],
            check=False,
            capture_output=True,
            text=True,
        )

    assert "smoke-ext" in result.stdout
    assert discovery_check.returncode == 0


@pytest.mark.skipif(not _docker_buildx_available(), reason="docker buildx is not available")
def test_scaffolded_extension_dockerfile_passes_buildx_syntax_check(tmp_path: Path) -> None:
    """Scaffolded extension Dockerfiles should be syntactically valid."""
    project_root = scaffold_extension("build-check-ext", target_dir=tmp_path)

    subprocess.run(  # noqa: S603
        [
            _docker_cli_path() or "docker",
            "buildx",
            "build",
            "--check",
            "--build-arg",
            "AGENT_FORGE_IMAGE=python:3.12-slim",
            "-f",
            "Dockerfile",
            ".",
        ],
        check=True,
        cwd=project_root,
        capture_output=True,
        text=True,
    )
