"""Unit tests for deployment artifacts introduced in issue #131."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_extension_dockerfile_supports_extension_build_arg() -> None:
    """Dockerfile.extension should install extension packages via build args."""
    dockerfile = (REPO_ROOT / "Dockerfile.extension").read_text(encoding="utf-8")

    assert 'ARG EXTENSIONS=""' in dockerfile
    assert "COPY examples ./examples" in dockerfile
    assert 'RUN pip install --no-cache-dir ".[redis]"' in dockerfile
    assert "pip install --no-cache-dir ${EXTENSIONS}" in dockerfile
    assert "RUN agent-forge extensions list" in dockerfile


def test_extensions_compose_override_defines_persona_services() -> None:
    """The compose override should wire persona-specific extension services."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.extensions.yml").read_text())
    services = compose["services"]

    assert "agent-reentrancy" in services
    assert "agent-access-control" in services
    assert "agent-full-spectrum" in services
    assert services["agent-forge-service"]["profiles"] == ["disabled"]

    build_args = services["agent-reentrancy"]["build"]["args"]
    assert build_args["EXTENSIONS"] == "${AGENT_FORGE_EXTENSIONS:-agent-forge-proof-of-audit}"

    environment = services["agent-reentrancy"]["environment"]
    assert environment["AGENT_FORGE_QUEUE_BACKEND"] == "redis"
    assert environment["AGENT_FORGE_QUEUE_REDIS_URL"] == "redis://redis:6379/0"
    assert environment["GEMINI_API_KEY"] == "${GEMINI_API_KEY:-}"
