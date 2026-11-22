"""Integration tests for shared-use sandbox qualification (#157)."""

from __future__ import annotations

import platform
import shutil

import docker
import pytest
from docker.errors import DockerException

from agent_forge.sandbox.qualification import qualify_sandbox_backend

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_docker_sandbox_qualifies_for_shared_use() -> None:
    """Hardened DockerSandbox passes all six shared-use qualification checks."""
    try:
        client = docker.from_env()
        client.ping()
    except DockerException:
        pytest.skip("Docker daemon not available")

    try:
        client.images.get("agent-forge-sandbox:latest")
    except DockerException:
        pytest.skip("agent-forge-sandbox:latest image not built (run make build-sandbox)")

    report = await qualify_sandbox_backend("docker")
    failed = [check for check in report.checks if not check.passed]
    assert report.qualified_for_shared_use, f"Failed Docker checks: {failed}"
    assert [check.name for check in report.checks] == [
        "isolated_workspaces",
        "credential_handling",
        "egress_policy",
        "resource_limits",
        "cancellation",
        "cleanup",
    ]


@pytest.mark.asyncio
async def test_bwrap_sandbox_qualification_records_cgroup_gap() -> None:
    """BwrapSandbox isolates namespaces/FS/network/creds but fails shared cgroup limits."""
    if platform.system() != "Linux" or shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not available")

    report = await qualify_sandbox_backend("bwrap")
    by_name = {check.name: check for check in report.checks}

    assert by_name["isolated_workspaces"].passed
    assert by_name["credential_handling"].passed
    assert by_name["egress_policy"].passed
    assert by_name["cancellation"].passed
    assert by_name["cleanup"].passed
    assert not by_name["resource_limits"].passed
    assert not report.qualified_for_shared_use
