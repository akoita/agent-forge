"""Unit tests for polyglot toolchains, port publishing, and greenfield verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.runner import build_sandbox_config
from agent_forge.agent.verification import VerificationWorkspace
from agent_forge.cli import main
from agent_forge.config import ForgeConfig, SandboxSettings, load_config
from agent_forge.llm.errors import SandboxStartupError
from agent_forge.sandbox import (
    TOOLCHAIN_IMAGES,
    PortMapping,
    SandboxConfig,
    detect_toolchain,
    parse_published_port,
    parse_published_ports,
    resolve_sandbox_image,
)
from agent_forge.sandbox.base import ExecResult
from agent_forge.sandbox.bwrap import BwrapSandbox
from agent_forge.sandbox.docker import DockerSandbox

_SANDBOX_DIR = Path(__file__).resolve().parents[2] / "agent_forge" / "sandbox"
_BUILD_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build-sandbox.sh"


class TestToolchainDetection:
    """Polyglot toolchain marker detection and image resolution precedence."""

    @pytest.mark.parametrize(
        ("marker_file", "expected"),
        [
            ("go.mod", "go"),
            ("Cargo.toml", "rust"),
            ("pom.xml", "jvm"),
            ("build.gradle.kts", "jvm"),
            ("package.json", "node"),
            ("pyproject.toml", "python"),
        ],
    )
    def test_detects_supported_ecosystems(
        self, tmp_path: Path, marker_file: str, expected: str
    ) -> None:
        (tmp_path / marker_file).write_text("", encoding="utf-8")
        assert detect_toolchain(tmp_path) == expected

    def test_detects_full_for_mixed_python_and_node_and_defaults_empty_to_python(
        self, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        assert detect_toolchain(empty_dir) == "python"
        assert detect_toolchain(tmp_path / "does-not-exist") == "python"

        mixed_dir = tmp_path / "mixed"
        mixed_dir.mkdir()
        (mixed_dir / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (mixed_dir / "package.json").write_text("{}\n", encoding="utf-8")
        assert detect_toolchain(mixed_dir) == "full"

    def test_resolve_sandbox_image_precedence(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\nname='app'\n", encoding="utf-8")

        assert resolve_sandbox_image(
            "agent-forge-sandbox:latest",
            toolchain="auto",
            repo_path=tmp_path,
        ) == ("agent-forge-sandbox:rust", "rust")

        assert resolve_sandbox_image(
            "agent-forge-sandbox:latest",
            toolchain="go",
            repo_path=tmp_path,
        ) == ("agent-forge-sandbox:go", "go")

        assert resolve_sandbox_image(
            "custom/my-image:1.0",
            toolchain="go",
            repo_path=tmp_path,
        ) == ("custom/my-image:1.0", "go")

        with pytest.raises(ValueError, match="Unknown sandbox toolchain"):
            resolve_sandbox_image("agent-forge-sandbox:latest", toolchain="cobol")


class TestPublishedPortsAndBackends:
    """Loopback port parsing and Docker/bubblewrap runtime enforcement."""

    def test_parse_published_port_and_bindings(self) -> None:
        assert parse_published_port("3000") == PortMapping("127.0.0.1", 3000, 3000, "tcp")
        assert parse_published_port("8080:3000") == PortMapping("127.0.0.1", 8080, 3000, "tcp")
        assert parse_published_port("127.0.0.1:8080:3000/tcp") == PortMapping(
            "127.0.0.1", 8080, 3000, "tcp"
        )
        assert parse_published_port("[::1]:9090:8080/UDP") == PortMapping("::1", 9090, 8080, "udp")

        assert parse_published_ports(["8080:3000", "9000"]) == {
            "3000/tcp": ("127.0.0.1", 8080),
            "9000/tcp": ("127.0.0.1", 9000),
        }

    @pytest.mark.parametrize(
        "bad_spec",
        ["", "0:80", "70000", "8080:3000/sctp", "not-an-ip:80:80", "[::1:80:80"],
    )
    def test_parse_published_port_rejects_invalid_specs(self, bad_spec: str) -> None:
        with pytest.raises(ValueError, match=r"."):
            parse_published_port(bad_spec)

    def test_parse_published_ports_rejects_duplicate_container_port(self) -> None:
        with pytest.raises(ValueError, match="Duplicate published container port"):
            parse_published_ports(["8080:3000", "8081:3000/tcp"])

    @pytest.mark.asyncio
    async def test_docker_sandbox_ports_and_polyglot_caches(self, tmp_path: Path) -> None:
        client = MagicMock()
        container = MagicMock()
        container.short_id = "poly123"
        client.containers.run.return_value = container

        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = client
            sandbox = DockerSandbox()
            with pytest.raises(SandboxStartupError, match="network_enabled=True"):
                await sandbox.start(
                    str(tmp_path),
                    SandboxConfig(network_enabled=False, published_ports=["8080:3000"]),
                )

            await sandbox.start(
                str(tmp_path),
                SandboxConfig(
                    image=TOOLCHAIN_IMAGES["go"],
                    toolchain="go",
                    network_enabled=True,
                    writable_cache_mounts=True,
                    published_ports=["8080:3000"],
                ),
            )
            kwargs = client.containers.run.call_args[1]
            assert kwargs["ports"] == {"3000/tcp": ("127.0.0.1", 8080)}
            assert kwargs["environment"]["GOCACHE"] == "/cache/go-build"
            assert kwargs["environment"]["GOMODCACHE"] == "/cache/go-mod"
            assert kwargs["environment"]["CARGO_HOME"] == "/cache/cargo"
            assert kwargs["environment"]["GRADLE_USER_HOME"] == "/cache/gradle"

    @pytest.mark.asyncio
    async def test_bwrap_sandbox_port_validation_and_polyglot_caches(self, tmp_path: Path) -> None:
        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            sandbox = BwrapSandbox()
            with pytest.raises(SandboxStartupError, match="network_enabled=True"):
                await sandbox.start(
                    str(tmp_path),
                    SandboxConfig(
                        backend="bwrap", network_enabled=False, published_ports=["8080:3000"]
                    ),
                )

            await sandbox.start(
                str(tmp_path),
                SandboxConfig(
                    backend="bwrap",
                    network_enabled=True,
                    writable_cache_mounts=True,
                    published_ports=["8080:3000"],
                ),
            )
            env_vars = sandbox._build_env_vars()
            assert env_vars["GOCACHE"] == "/cache/go-build"
            assert env_vars["CARGO_HOME"] == "/cache/cargo"
            assert env_vars["GRADLE_USER_HOME"] == "/cache/gradle"


class TestConfigCliAndPromptWiring:
    """Configuration, runner, prompt rendering, and CLI flags for polyglot & ports."""

    def test_env_validation_and_build_sandbox_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.23\n", encoding="utf-8")
        monkeypatch.setenv("AGENT_FORGE_SANDBOX_TOOLCHAIN", "auto")
        monkeypatch.setenv("AGENT_FORGE_SANDBOX_PUBLISHED_PORTS", "8080:8080,127.0.0.1:9000:9000")

        cfg = load_config(project_path=tmp_path / "no.toml", user_path=tmp_path / "no-u.toml")
        assert cfg.sandbox.toolchain == "auto"
        assert cfg.sandbox.published_ports == ["8080:8080", "127.0.0.1:9000:9000"]

        sandbox_cfg = build_sandbox_config(cfg, repo_path=tmp_path)
        assert sandbox_cfg.image == "agent-forge-sandbox:go"
        assert sandbox_cfg.toolchain == "go"
        assert sandbox_cfg.published_ports == ["8080:8080", "127.0.0.1:9000:9000"]

        prompt = build_system_prompt(
            "Build service",
            [],
            sandbox_image=sandbox_cfg.image,
            published_ports=sandbox_cfg.published_ports,
        )
        assert "Runtime image: agent-forge-sandbox:go" in prompt
        assert "Published ports: 8080:8080, 127.0.0.1:9000:9000" in prompt

        with pytest.raises(ValueError, match="Invalid port"):
            ForgeConfig(sandbox=SandboxSettings(published_ports=["bad-port"]))

    def test_cli_run_passes_toolchain_and_publish_port_overrides(self) -> None:
        fake_cfg = MagicMock()
        fake_cfg.agent.default_provider = "gemini"
        fake_cfg.providers = {"gemini": MagicMock(api_key_env="GEMINI_API_KEY")}

        with (
            patch("agent_forge.cli.load_config", return_value=fake_cfg) as mock_load_config,
            patch("agent_forge.cli._run_agent"),
            patch("agent_forge.cli._emit_run_output"),
        ):
            res = CliRunner().invoke(
                main,
                [
                    "run",
                    "--task",
                    "Build Go server",
                    "--repo",
                    "/tmp/fake-go-repo",
                    "--toolchain",
                    "go",
                    "--network",
                    "--publish-port",
                    "8080:8080",
                    "--publish-port",
                    "9090:9090/tcp",
                ],
                env={"GEMINI_API_KEY": "test-key"},
            )

        assert res.exit_code == 0, res.output
        mock_load_config.assert_called_once_with(
            cli_overrides={
                "sandbox.toolchain": "go",
                "sandbox.published_ports": ["8080:8080", "9090:9090/tcp"],
                "sandbox.network_enabled": True,
            }
        )


class TestGreenfieldVerificationWorkspace:
    """VerificationWorkspace bootstraps empty directories and uninitialized repos cleanly."""

    @pytest.mark.asyncio
    async def test_empty_dir_seed_files_and_empty_git_repo_verify(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty_greenfield"
        empty_dir.mkdir()

        seed_dir = tmp_path / "seeded_greenfield"
        seed_dir.mkdir()
        (seed_dir / "SPEC.md").write_text("# App Spec\n", encoding="utf-8")

        captured_configs: list[SandboxConfig] = []

        async def _verify_exec(*_args: Any, **_kwargs: Any) -> ExecResult:
            return ExecResult(0, "ok", "")

        verifier_sandbox = AsyncMock()
        verifier_sandbox.start.side_effect = lambda _path, cfg: captured_configs.append(cfg)
        verifier_sandbox.exec.side_effect = _verify_exec
        candidate_sandbox = AsyncMock()

        with VerificationWorkspace(str(empty_dir), "pytest -q") as ws1:
            rev1 = ws1.base_revision
            (ws1.workspace / "main.py").write_text("print('hi')\n", encoding="utf-8")
            res = await ws1.verify(
                SandboxConfig(network_enabled=True, published_ports=["8080:8080"]),
                candidate_sandbox,
                sandbox_factory=lambda _cfg: verifier_sandbox,
            )

        assert res.passed is True
        assert res.reason == "verified"
        assert "main.py" in res.patch
        assert list(empty_dir.iterdir()) == []
        assert captured_configs[0].network_enabled is False
        assert captured_configs[0].published_ports == []

        # Second enter produces the identical deterministic base revision for empty dirs
        with VerificationWorkspace(str(empty_dir), "pytest -q") as ws2:
            assert ws2.base_revision == rev1
            no_change = await ws2.verify(
                SandboxConfig(),
                candidate_sandbox,
                sandbox_factory=lambda _cfg: verifier_sandbox,
            )
            assert no_change.reason == "empty_patch"

        # Seed files are preserved as committed baseline inside the bootstrapped workspace
        with VerificationWorkspace(str(seed_dir), "cat SPEC.md") as ws_seed:
            assert (ws_seed.workspace / "SPEC.md").read_text(encoding="utf-8") == "# App Spec\n"
            (ws_seed.workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            patch_text = ws_seed.capture_patch()
            assert "app.py" in patch_text
            assert "SPEC.md" not in patch_text


class TestPolyglotDockerfiles:
    """Polyglot sandbox Dockerfiles and build script definitions stay consistent."""

    @pytest.mark.parametrize(
        ("filename", "variant"),
        [("Dockerfile.go", "go"), ("Dockerfile.rust", "rust"), ("Dockerfile.jvm", "jvm")],
    )
    def test_dockerfiles_and_build_script_present(self, filename: str, variant: str) -> None:
        dockerfile = _SANDBOX_DIR / filename
        assert dockerfile.is_file()
        content = dockerfile.read_text(encoding="utf-8")
        assert "WORKDIR /workspace" in content
        assert "USER agent" in content
        script_text = _BUILD_SCRIPT.read_text(encoding="utf-8")
        assert f"agent-forge-sandbox:{variant}" in script_text
