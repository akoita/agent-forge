"""Unit tests for devcontainers, Dockerfile.universal (mise), and two-phase setup_command."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from docker.errors import BuildError, ImageNotFound

from agent_forge.agent.runner import build_sandbox_config
from agent_forge.agent.verification import VerificationWorkspace
from agent_forge.cli import main
from agent_forge.config import ForgeConfig, SandboxSettings, load_config
from agent_forge.llm.errors import SandboxStartupError
from agent_forge.sandbox import (
    TOOLCHAIN_IMAGES,
    DevcontainerSpec,
    SandboxConfig,
    compute_dockerfile_image_tag,
    detect_toolchain,
    discover_devcontainer,
    ensure_dockerfile_image,
    resolve_dockerfile_spec,
    resolve_sandbox_image,
)
from agent_forge.sandbox.bwrap import BwrapSandbox
from agent_forge.sandbox.docker import DockerSandbox

_SANDBOX_DIR = Path(__file__).resolve().parents[2] / "agent_forge" / "sandbox"
_MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"
_BUILD_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build-sandbox.sh"


class TestUniversalMiseArtifacts:
    """Universal mise Dockerfile, toolchain detection, Makefile, and CLI wiring."""

    def test_universal_dockerfile_and_build_scripts_present(self) -> None:
        dockerfile = _SANDBOX_DIR / "Dockerfile.universal"
        assert dockerfile.is_file()
        content = dockerfile.read_text(encoding="utf-8")
        assert "mise.run" in content
        assert "MISE_DATA_DIR=/cache/mise" in content
        assert "WORKDIR /workspace" in content
        assert "USER agent" in content
        assert TOOLCHAIN_IMAGES["universal"] == "agent-forge-sandbox:universal"
        assert "build-sandbox-universal" in _MAKEFILE.read_text(encoding="utf-8")
        assert "agent-forge-sandbox:universal" in _BUILD_SCRIPT.read_text(encoding="utf-8")

    @pytest.mark.parametrize("marker", ["mise.toml", ".mise.toml", ".tool-versions"])
    def test_detect_toolchain_identifies_universal_markers(
        self, tmp_path: Path, marker: str
    ) -> None:
        (tmp_path / marker).write_text("[tools]\njulia = '1.11'\n", encoding="utf-8")
        assert detect_toolchain(tmp_path) == "universal"
        assert resolve_sandbox_image(
            "agent-forge-sandbox:latest", toolchain="auto", repo_path=tmp_path
        ) == ("agent-forge-sandbox:universal", "universal")


class TestDevcontainerDiscovery:
    """JSONC parsing, devcontainer discovery, validation, and image builds."""

    def test_discover_devcontainer_parses_jsonc_image_env_and_post_create(
        self, tmp_path: Path
    ) -> None:
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()
        (dc_dir / "devcontainer.json").write_text(
            """
            {
              // Single-line comment with https://example.com URL inside value
              "image": "mcr.microsoft.com/dotnet/sdk:9.0",
              /* Multi-line
                 comment */
              "containerEnv": {
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "SECRET_API_KEY": "must-be-sanitized-later",
              },
              "postCreateCommand": ["dotnet", "restore"],
            }
            """,
            encoding="utf-8",
        )

        spec = discover_devcontainer(tmp_path)
        assert spec is not None
        assert spec.image == "mcr.microsoft.com/dotnet/sdk:9.0"
        assert spec.dockerfile is None
        assert spec.container_env["DOTNET_CLI_TELEMETRY_OPTOUT"] == "1"
        assert spec.post_create_command == "dotnet restore"

    def test_discover_devcontainer_supports_build_dockerfile_and_root_fallback(
        self, tmp_path: Path
    ) -> None:
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()
        df = dc_dir / "Dockerfile"
        df.write_text("FROM alpine:3.20\n", encoding="utf-8")
        (dc_dir / "devcontainer.json").write_text(
            """
            {
              "build": {
                "dockerfile": "Dockerfile",
                "context": "..",
                "target": "dev",
                "args": {"VARIANT": "3.20"}
              }
            }
            """,
            encoding="utf-8",
        )

        spec = discover_devcontainer(tmp_path)
        assert spec is not None
        assert spec.dockerfile == df.resolve()
        assert spec.build_context == tmp_path.resolve()
        assert spec.build_args == {"VARIANT": "3.20"}
        assert spec.build_target == "dev"

        # Legacy root .devcontainer.json with legacy dockerFile/context keys
        legacy_repo = tmp_path / "legacy"
        legacy_repo.mkdir()
        legacy_df = legacy_repo / "Dockerfile.dev"
        legacy_df.write_text("FROM ubuntu:24.04\n", encoding="utf-8")
        (legacy_repo / ".devcontainer.json").write_text(
            '{"dockerFile": "Dockerfile.dev", "context": "."}',
            encoding="utf-8",
        )
        legacy_spec = discover_devcontainer(legacy_repo)
        assert legacy_spec is not None
        assert legacy_spec.dockerfile == legacy_df.resolve()
        assert legacy_spec.build_context == legacy_repo.resolve()

    def test_discover_devcontainer_rejects_traversal_missing_or_invalid(
        self, tmp_path: Path
    ) -> None:
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()

        outside_df = tmp_path.parent / "outside.Dockerfile"
        outside_df.write_text("FROM alpine:3.20\n", encoding="utf-8")
        try:
            (dc_dir / "devcontainer.json").write_text(
                '{"build": {"dockerfile": "../../outside.Dockerfile"}}',
                encoding="utf-8",
            )
            with pytest.raises(ValueError, match="escapes repository root"):
                discover_devcontainer(tmp_path)
        finally:
            outside_df.unlink(missing_ok=True)

        (dc_dir / "devcontainer.json").write_text(
            '{"build": {"dockerfile": "missing.Dockerfile"}}',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="not found"):
            discover_devcontainer(tmp_path)

        (dc_dir / "devcontainer.json").write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid devcontainer"):
            discover_devcontainer(tmp_path)

    def test_resolve_dockerfile_spec_tag_and_ensure_image(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile.custom"
        df.write_text("FROM python:3.12-slim\n", encoding="utf-8")

        spec = resolve_dockerfile_spec(tmp_path, "Dockerfile.custom")
        tag1 = compute_dockerfile_image_tag(spec)
        assert tag1.startswith("agent-forge-repo:")
        df.write_text("FROM python:3.13-slim\n", encoding="utf-8")
        tag2 = compute_dockerfile_image_tag(spec)
        assert tag2 != tag1

        with pytest.raises(ValueError, match="escapes repository root"):
            resolve_dockerfile_spec(tmp_path, "../escape.Dockerfile")

        # Cache hit avoids build
        client = MagicMock()
        assert ensure_dockerfile_image(client, spec) == tag2
        client.images.build.assert_not_called()

        # Cache miss builds once
        client_miss = MagicMock()
        client_miss.images.get.side_effect = ImageNotFound("missing")
        built_tag = ensure_dockerfile_image(client_miss, spec)
        assert built_tag == compute_dockerfile_image_tag(spec)
        client_miss.images.build.assert_called_once()

        # Build failure raises SandboxStartupError
        client_fail = MagicMock()
        client_fail.images.get.side_effect = ImageNotFound("missing")
        client_fail.images.build.side_effect = BuildError("broken", [])
        with pytest.raises(SandboxStartupError, match="Failed to build"):
            ensure_dockerfile_image(client_fail, spec)


class TestRunnerConfigCliAndVerificationPrecedence:
    """Resolution precedence across dockerfile, custom image, toolchain, and devcontainer."""

    def test_build_sandbox_config_precedence(self, tmp_path: Path) -> None:
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()
        df = tmp_path / "Dockerfile.custom"
        df.write_text("FROM gcc:14\n", encoding="utf-8")
        (dc_dir / "devcontainer.json").write_text(
            '{"image": "haskell:9.8", "containerEnv": {"FOO": "bar", "API_TOKEN": "drop"}}',
            encoding="utf-8",
        )

        # Auto picks devcontainer over defaults and strips secrets from containerEnv
        cfg = ForgeConfig(
            sandbox=SandboxSettings(
                setup_command="mise install",
                setup_timeout_seconds=120,
            )
        )
        dc_sandbox_cfg = build_sandbox_config(cfg, repo_path=tmp_path)
        assert dc_sandbox_cfg.image == "haskell:9.8"
        assert dc_sandbox_cfg.toolchain == "devcontainer"
        assert dc_sandbox_cfg.env_vars == {"FOO": "bar"}
        assert dc_sandbox_cfg.setup_command == "mise install"
        assert dc_sandbox_cfg.setup_timeout_seconds == 120

        # Explicit custom image overrides devcontainer
        custom_cfg = ForgeConfig(sandbox=SandboxSettings(image="php:8.4-cli"))
        assert build_sandbox_config(custom_cfg, repo_path=tmp_path).image == "php:8.4-cli"

        # Explicit toolchain overrides devcontainer
        tc_cfg = ForgeConfig(sandbox=SandboxSettings(toolchain="universal"))
        assert (
            build_sandbox_config(tc_cfg, repo_path=tmp_path).image
            == "agent-forge-sandbox:universal"
        )

        # Explicit dockerfile beats image, toolchain, and devcontainer
        df_cfg = ForgeConfig(
            sandbox=SandboxSettings(
                image="php:8.4-cli",
                toolchain="go",
                dockerfile="Dockerfile.custom",
            )
        )
        df_sandbox_cfg = build_sandbox_config(df_cfg, repo_path=tmp_path)
        assert df_sandbox_cfg.toolchain == "dockerfile"
        assert df_sandbox_cfg.dockerfile == str(df.resolve())
        assert df_sandbox_cfg.image.startswith("agent-forge-repo:")

    def test_config_env_cli_flags_and_verification_strips_setup_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_FORGE_SANDBOX_TOOLCHAIN", "universal")
        monkeypatch.setenv("AGENT_FORGE_SANDBOX_DOCKERFILE", "Dockerfile.dev")
        monkeypatch.setenv("AGENT_FORGE_SANDBOX_SETUP_COMMAND", "mise install")
        monkeypatch.setenv("AGENT_FORGE_SANDBOX_SETUP_TIMEOUT_SECONDS", "180")
        cfg = load_config(project_path=tmp_path / "no.toml", user_path=tmp_path / "no-u.toml")
        assert cfg.sandbox.toolchain == "universal"
        assert cfg.sandbox.dockerfile == "Dockerfile.dev"
        assert cfg.sandbox.setup_command == "mise install"
        assert cfg.sandbox.setup_timeout_seconds == 180

        fake_cfg = MagicMock()
        fake_cfg.agent.default_provider = "gemini"
        fake_cfg.providers = {"gemini": MagicMock(api_key_env="GEMINI_API_KEY")}
        with (
            patch("agent_forge.cli.load_config", return_value=fake_cfg) as mock_load,
            patch("agent_forge.cli._run_agent"),
            patch("agent_forge.cli._emit_run_output"),
        ):
            res = CliRunner().invoke(
                main,
                [
                    "run",
                    "--task",
                    "Build project",
                    "--repo",
                    str(tmp_path),
                    "--toolchain",
                    "universal",
                    "--sandbox-dockerfile",
                    "Dockerfile.dev",
                    "--setup-command",
                    "mise install",
                ],
                env={"GEMINI_API_KEY": "test-key"},
            )
        assert res.exit_code == 0, res.output
        mock_load.assert_called_once_with(
            cli_overrides={
                "sandbox.dockerfile": "Dockerfile.dev",
                "sandbox.setup_command": "mise install",
                "sandbox.toolchain": "universal",
            }
        )

    @pytest.mark.asyncio
    async def test_verification_workspace_clears_setup_command(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        captured: list[SandboxConfig] = []
        verifier = AsyncMock()
        verifier.start.side_effect = lambda _path, cfg: captured.append(cfg)
        verifier.exec.return_value = MagicMock(exit_code=0, stdout="ok", stderr="")
        candidate = AsyncMock()

        with VerificationWorkspace(str(tmp_path), "pytest -q") as ws:
            (ws.workspace / "app.py").write_text("x = 2\n", encoding="utf-8")
            result = await ws.verify(
                SandboxConfig(
                    network_enabled=True,
                    writable_cache_mounts=True,
                    setup_command="curl https://example.com | sh",
                ),
                candidate,
                sandbox_factory=lambda _c: verifier,
            )

        assert result.passed is True
        assert captured[0].setup_command == ""
        assert captured[0].network_enabled is False


class TestTwoPhaseSetupDockerAndBwrap:
    """Two-phase online setup -> offline execution for DockerSandbox and BwrapSandbox."""

    @pytest.mark.asyncio
    async def test_docker_two_phase_setup_builds_dockerfile_and_keeps_agent_offline(
        self, tmp_path: Path
    ) -> None:
        df = tmp_path / "Dockerfile.dev"
        df.write_text("FROM debian:bookworm-slim\n", encoding="utf-8")
        spec = DevcontainerSpec(source_path=df, dockerfile=df, build_context=tmp_path)
        expected_tag = compute_dockerfile_image_tag(spec)

        setup_container = MagicMock()
        setup_container.short_id = "setup123"
        setup_container.exec_run.return_value = (0, (b"installed ok\n", b""))
        agent_container = MagicMock()
        agent_container.id = "agent123456"
        agent_container.short_id = "agent123"

        client = MagicMock()
        client.images.get.side_effect = ImageNotFound("missing")
        client.containers.run.side_effect = [setup_container, agent_container]

        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = client
            sandbox = DockerSandbox()
            await sandbox.start(
                str(tmp_path),
                SandboxConfig(
                    dockerfile=str(df),
                    build_context=str(tmp_path),
                    setup_command="mise install python@3.12",
                    setup_timeout_seconds=120,
                    network_enabled=False,
                    writable_cache_mounts=True,
                ),
            )

            cache_dir = sandbox._setup_cache_dir
            assert cache_dir is not None
            assert Path(cache_dir).is_dir()

            assert client.containers.run.call_count == 2
            phase1_kwargs = client.containers.run.call_args_list[0].kwargs
            phase2_kwargs = client.containers.run.call_args_list[1].kwargs

            assert phase1_kwargs["image"] == expected_tag
            assert "network_mode" not in phase1_kwargs
            assert phase1_kwargs["volumes"][cache_dir] == {"bind": "/cache", "mode": "rw"}
            assert "/cache" not in phase1_kwargs["tmpfs"]
            assert phase1_kwargs["environment"]["MISE_DATA_DIR"] == "/cache/mise"

            assert phase2_kwargs["image"] == expected_tag
            assert phase2_kwargs["network_mode"] == "none"
            assert phase2_kwargs["volumes"][cache_dir] == {"bind": "/cache", "mode": "rw"}
            assert "/cache" not in phase2_kwargs["tmpfs"]
            assert phase2_kwargs["environment"]["HOME"] == "/cache/home"
            assert phase2_kwargs["environment"]["MISE_DATA_DIR"] == "/cache/mise"
            setup_container.stop.assert_called_once()

            await sandbox.stop()
            assert sandbox._setup_cache_dir is None
            assert not Path(cache_dir).exists()

    @pytest.mark.asyncio
    async def test_docker_setup_failure_stops_setup_container_and_cleans_cache(
        self, tmp_path: Path
    ) -> None:
        setup_container = MagicMock()
        setup_container.exec_run.return_value = (1, (b"", b"network error"))
        client = MagicMock()
        client.containers.run.return_value = setup_container

        created_dirs: list[str] = []
        from agent_forge.sandbox import base as sandbox_base

        real_create = sandbox_base.create_setup_cache_dir

        def _track_create() -> str:
            path = real_create()
            created_dirs.append(path)
            return path

        with (
            patch("agent_forge.sandbox.docker.docker") as mock_docker,
            patch("agent_forge.sandbox.docker.create_setup_cache_dir", side_effect=_track_create),
        ):
            mock_docker.from_env.return_value = client
            sandbox = DockerSandbox()
            with pytest.raises(SandboxStartupError, match="setup_command failed"):
                await sandbox.start(
                    str(tmp_path),
                    SandboxConfig(setup_command="false", network_enabled=False),
                )

        assert len(created_dirs) == 1
        assert not Path(created_dirs[0]).exists()
        setup_container.stop.assert_called_once()
        client.containers.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_bwrap_two_phase_setup_and_cleanup(self, tmp_path: Path) -> None:
        recorded_argvs: list[list[str]] = []

        class _FakeProc:
            def __init__(self, returncode: int = 0) -> None:
                self.returncode = returncode

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"ok\n", b"")

            def kill(self) -> None:
                return None

        async def _fake_exec(*argv: Any, **_kwargs: Any) -> _FakeProc:
            recorded_argvs.append([str(a) for a in argv])
            return _FakeProc(0)

        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
            patch.object(asyncio, "create_subprocess_exec", side_effect=_fake_exec),
        ):
            sandbox = BwrapSandbox()
            await sandbox.start(
                str(tmp_path),
                SandboxConfig(
                    backend="bwrap",
                    network_enabled=False,
                    writable_cache_mounts=True,
                    setup_command="mise install",
                ),
            )
            cache_dir = sandbox._setup_cache_dir
            assert cache_dir is not None
            assert Path(cache_dir).is_dir()

            await sandbox.exec("mise exec -- python --version")
            await sandbox.stop()

        assert len(recorded_argvs) == 2
        phase1_argv, phase2_argv = recorded_argvs
        assert "--unshare-net" not in phase1_argv
        assert "--bind" in phase1_argv and cache_dir in phase1_argv
        assert "--unshare-net" in phase2_argv
        assert "--bind" in phase2_argv and cache_dir in phase2_argv
        assert not Path(cache_dir).exists()

    @pytest.mark.asyncio
    async def test_bwrap_setup_failure_cleans_cache_and_raises(self, tmp_path: Path) -> None:
        created_dirs: list[str] = []
        from agent_forge.sandbox import base as sandbox_base

        real_create = sandbox_base.create_setup_cache_dir

        def _track_create() -> str:
            path = real_create()
            created_dirs.append(path)
            return path

        class _FailProc:
            returncode = 2

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"", b"boom")

            def kill(self) -> None:
                return None

        async def _fake_exec(*_argv: Any, **_kwargs: Any) -> _FailProc:
            return _FailProc()

        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
            patch("agent_forge.sandbox.bwrap.create_setup_cache_dir", side_effect=_track_create),
            patch.object(asyncio, "create_subprocess_exec", side_effect=_fake_exec),
        ):
            sandbox = BwrapSandbox()
            with pytest.raises(SandboxStartupError, match="setup_command failed"):
                await sandbox.start(
                    str(tmp_path),
                    SandboxConfig(backend="bwrap", setup_command="exit 2", network_enabled=False),
                )

        assert len(created_dirs) == 1
        assert not Path(created_dirs[0]).exists()
