"""Tests for multi-instance service mode (#119).

Validates persona binding, workspace isolation, and health metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agent_forge.profiles.profile import AgentProfile
from agent_forge.service.app import HostedRunService, create_app
from agent_forge.service.models import HealthResponse

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_service_root(tmp_path: Path) -> Path:
    """Create a temporary service root directory."""
    root = tmp_path / "service"
    root.mkdir()
    return root


def _minimal_config() -> dict:
    """Return kwargs for a HostedRunService with no external deps."""
    return {}


# ---------------------------------------------------------------------------
# HostedRunService — workspace isolation
# ---------------------------------------------------------------------------


class TestWorkspaceIsolation:
    """Verify instance_id creates isolated subdirectories."""

    def test_service_root_without_instance_id(self, base_service_root: Path) -> None:
        """Without instance_id, service_root is used directly."""
        service = HostedRunService(service_root=base_service_root)
        assert service._service_root == base_service_root

    def test_service_root_with_instance_id(self, base_service_root: Path) -> None:
        """With instance_id, service_root includes the instance subdirectory."""
        service = HostedRunService(service_root=base_service_root, instance_id="agent-01")
        assert service._service_root == base_service_root / "agent-01"

    def test_different_instances_get_different_roots(self, base_service_root: Path) -> None:
        """Two instances with different IDs get separate workspace roots."""
        svc_a = HostedRunService(service_root=base_service_root, instance_id="agent-01")
        svc_b = HostedRunService(service_root=base_service_root, instance_id="agent-02")
        assert svc_a._service_root != svc_b._service_root
        assert svc_a._service_root.name == "agent-01"
        assert svc_b._service_root.name == "agent-02"


# ---------------------------------------------------------------------------
# HostedRunService — persona resolution
# ---------------------------------------------------------------------------


class TestPersonaResolution:
    """Verify persona profile is resolved and validated at startup."""

    @pytest.mark.asyncio
    async def test_valid_persona_resolves(self, base_service_root: Path) -> None:
        """A valid persona profile is resolved and stored."""
        fake_profile = AgentProfile(
            id="reentrancy-only",
            name="Reentrancy Specialist",
            capabilities=["reentrancy"],
            llm_provider="gemini",
        )
        fake_registry = {"reentrancy-only": fake_profile, "gemini": fake_profile}

        service = HostedRunService(
            service_root=base_service_root,
            persona="reentrancy-only",
        )

        with (
            patch("agent_forge.service.app.load_profiles", return_value=fake_registry),
            patch("agent_forge.service.app.load_client_registry", return_value={}),
        ):
            await service.start()

        assert service._resolved_persona is not None
        assert service._resolved_persona.id == "reentrancy-only"
        assert service._resolved_persona.capabilities == ["reentrancy"]

        await service.stop()

    @pytest.mark.asyncio
    async def test_invalid_persona_raises(self, base_service_root: Path) -> None:
        """An unknown persona raises ValueError at startup."""
        service = HostedRunService(
            service_root=base_service_root,
            persona="nonexistent-profile",
        )

        with (
            patch(
                "agent_forge.service.app.load_profiles",
                return_value={"gemini": AgentProfile(id="gemini", name="Gemini")},
            ),
            patch("agent_forge.service.app.load_client_registry", return_value={}),
            pytest.raises(ValueError, match="unknown persona profile"),
        ):
            await service.start()

    @pytest.mark.asyncio
    async def test_no_persona_leaves_resolved_none(self, base_service_root: Path) -> None:
        """Without persona, _resolved_persona stays None."""
        service = HostedRunService(service_root=base_service_root)

        with (
            patch("agent_forge.service.app.load_profiles", return_value={}),
            patch("agent_forge.service.app.load_client_registry", return_value={}),
        ):
            await service.start()

        assert service._resolved_persona is None
        await service.stop()


# ---------------------------------------------------------------------------
# HealthResponse — persona metadata
# ---------------------------------------------------------------------------


class TestHealthResponseModel:
    """Verify HealthResponse model handles persona fields."""

    def test_health_with_persona_metadata(self) -> None:
        """HealthResponse includes persona fields when set."""
        resp = HealthResponse(
            status="ok",
            service_root="/tmp/service/agent-01",
            queue_backend="memory",
            sandbox_image="agent-forge-sandbox:latest",
            instance_id="agent-01",
            persona="reentrancy-only",
            capabilities=["reentrancy"],
            llm_provider="gemini",
        )
        assert resp.instance_id == "agent-01"
        assert resp.persona == "reentrancy-only"
        assert resp.capabilities == ["reentrancy"]
        assert resp.llm_provider == "gemini"

    def test_health_without_persona_metadata(self) -> None:
        """HealthResponse defaults to None when persona is not set."""
        resp = HealthResponse(
            status="ok",
            service_root="/tmp/service",
            queue_backend="memory",
            sandbox_image="agent-forge-sandbox:latest",
        )
        assert resp.instance_id is None
        assert resp.persona is None
        assert resp.capabilities == []
        assert resp.llm_provider is None

    def test_health_serialization_roundtrip(self) -> None:
        """HealthResponse can serialize and deserialize with persona fields."""
        resp = HealthResponse(
            status="ok",
            service_root="/tmp/service/agent-01",
            queue_backend="memory",
            sandbox_image="agent-forge-sandbox:latest",
            instance_id="agent-01",
            persona="full-spectrum",
            capabilities=["reentrancy", "access-control"],
            llm_provider="openai",
        )
        data = resp.model_dump()
        restored = HealthResponse.model_validate(data)
        assert restored == resp


# ---------------------------------------------------------------------------
# create_app — integration
# ---------------------------------------------------------------------------


class TestCreateApp:
    """Verify create_app wires instance_id and persona."""

    def test_create_app_passes_instance_id(self, base_service_root: Path) -> None:
        """create_app passes instance_id to the HostedRunService."""
        app = create_app(
            service_root=base_service_root,
            instance_id="agent-03",
        )
        service = app.state.service
        assert service._instance_id == "agent-03"
        assert service._service_root == base_service_root / "agent-03"

    def test_create_app_passes_persona(self, base_service_root: Path) -> None:
        """create_app passes persona to the HostedRunService."""
        app = create_app(
            service_root=base_service_root,
            persona="reentrancy-only",
        )
        service = app.state.service
        assert service._persona_id == "reentrancy-only"

    def test_create_app_without_multi_instance(self, base_service_root: Path) -> None:
        """create_app works without multi-instance params (backward compat)."""
        app = create_app(service_root=base_service_root)
        service = app.state.service
        assert service._instance_id is None
        assert service._persona_id is None
        assert service._service_root == base_service_root


# ---------------------------------------------------------------------------
# CLI — serve flags
# ---------------------------------------------------------------------------


class TestServeCLI:
    """Verify --persona and --instance-id flags are wired."""

    def test_serve_help_shows_persona_flag(self) -> None:
        """The serve command advertises --persona."""
        runner = CliRunner()
        from agent_forge.cli import main

        result = runner.invoke(main, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--persona" in result.output

    def test_serve_help_shows_instance_id_flag(self) -> None:
        """The serve command advertises --instance-id."""
        runner = CliRunner()
        from agent_forge.cli import main

        result = runner.invoke(main, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--instance-id" in result.output


# ---------------------------------------------------------------------------
# AgentProfile — capabilities field
# ---------------------------------------------------------------------------


class TestAgentProfileCapabilities:
    """Verify the capabilities field on AgentProfile."""

    def test_capabilities_defaults_to_empty(self) -> None:
        """Capabilities is an empty list by default."""
        profile = AgentProfile(id="test", name="Test")
        assert profile.capabilities == []

    def test_capabilities_from_yaml_data(self) -> None:
        """Capabilities can be set from YAML-like dict data."""
        profile = AgentProfile.model_validate(
            {
                "id": "reentrancy-only",
                "name": "Reentrancy Specialist",
                "capabilities": ["reentrancy"],
                "llm_provider": "gemini",
            }
        )
        assert profile.capabilities == ["reentrancy"]

    def test_multiple_capabilities(self) -> None:
        """Profile supports multiple capabilities."""
        profile = AgentProfile(
            id="full-spectrum",
            name="Full Spectrum",
            capabilities=["reentrancy", "access-control", "unchecked-calls"],
        )
        assert len(profile.capabilities) == 3
        assert "access-control" in profile.capabilities
