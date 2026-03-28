"""Unit tests for sandbox backend selection."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_forge.llm.errors import SandboxStartupError
from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.bwrap import BwrapSandbox
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.sandbox.factory import create_sandbox


class TestCreateSandbox:
    def test_docker_backend(self) -> None:
        sandbox = create_sandbox(SandboxConfig(backend="docker"))
        assert isinstance(sandbox, DockerSandbox)

    def test_bwrap_backend(self) -> None:
        sandbox = create_sandbox(SandboxConfig(backend="bwrap"))
        assert isinstance(sandbox, BwrapSandbox)

    def test_auto_prefers_docker(self) -> None:
        with (
            patch("agent_forge.sandbox.factory._docker_available", return_value=True),
            patch("agent_forge.sandbox.factory._bwrap_available", return_value=True),
        ):
            sandbox = create_sandbox(SandboxConfig(backend="auto"))
        assert isinstance(sandbox, DockerSandbox)

    def test_auto_falls_back_to_bwrap(self) -> None:
        with (
            patch("agent_forge.sandbox.factory._docker_available", return_value=False),
            patch("agent_forge.sandbox.factory._bwrap_available", return_value=True),
        ):
            sandbox = create_sandbox(SandboxConfig(backend="auto"))
        assert isinstance(sandbox, BwrapSandbox)

    def test_auto_raises_when_no_backend_available(self) -> None:
        with (
            patch("agent_forge.sandbox.factory._docker_available", return_value=False),
            patch("agent_forge.sandbox.factory._bwrap_available", return_value=False),
            pytest.raises(SandboxStartupError, match="No supported sandbox backend"),
        ):
            create_sandbox(SandboxConfig(backend="auto"))
