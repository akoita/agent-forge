"""Unit tests for zero-wrapper stock Docker images, grep fallback, and bwrap toolchains."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from docker.errors import APIError

from agent_forge.agent.runner import build_sandbox_config
from agent_forge.config import ForgeConfig, SandboxSettings
from agent_forge.llm.errors import SandboxStartupError
from agent_forge.sandbox import SandboxConfig, validate_ro_bind_path
from agent_forge.sandbox.base import ExecResult
from agent_forge.sandbox.bwrap import BwrapSandbox
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.tools.search_codebase import SearchCodebaseTool

if TYPE_CHECKING:
    from pathlib import Path


class TestDockerStockImageCompatibility:
    """Upstream Docker images work without custom entrypoints, bash, or online /cache."""

    @pytest.mark.asyncio
    async def test_stock_image_entrypoint_keepalive_offline_home_and_sh_fallback(self) -> None:
        client = MagicMock()
        container = MagicMock()
        container.short_id = "stock1"
        client.containers.run.return_value = container

        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = client
            sandbox = DockerSandbox()
            await sandbox.start(
                "/tmp/repo",
                config=SandboxConfig(image="mcr.microsoft.com/dotnet/sdk:9.0"),
            )

            kwargs = client.containers.run.call_args[1]
            assert kwargs["entrypoint"] == ""
            assert kwargs["command"] == ["sh", "-c", "while :; do sleep 3600; done"]
            assert kwargs["environment"]["HOME"] == "/tmp/home"
            assert kwargs["environment"]["XDG_CACHE_HOME"] == "/tmp/xdg"

            # First exec fails due to missing bash -> automatically switches to sh
            container.exec_run.side_effect = [
                (127, (b"", b"OCI runtime exec failed: bash: executable file not found")),
                (0, (b"9.0.100\n", b"")),
            ]
            res1 = await sandbox.exec("dotnet --version")
            assert res1.exit_code == 0
            assert res1.stdout == "9.0.100\n"
            assert container.exec_run.call_args_list[0].args[0] == [
                "bash",
                "-c",
                "dotnet --version",
            ]
            assert container.exec_run.call_args_list[1].args[0] == ["sh", "-c", "dotnet --version"]

            # Sticky sh on subsequent commands
            container.exec_run.side_effect = [(0, (b"ok\n", b""))]
            res2 = await sandbox.exec("echo ok")
            assert res2.exit_code == 0
            assert container.exec_run.call_args_list[-1].args[0] == ["sh", "-c", "echo ok"]

    @pytest.mark.asyncio
    async def test_api_error_missing_bash_falls_back_to_sh(self) -> None:
        client = MagicMock()
        container = MagicMock()
        container.short_id = "alpine1"
        client.containers.run.return_value = container

        with patch("agent_forge.sandbox.docker.docker") as mock_docker:
            mock_docker.from_env.return_value = client
            sandbox = DockerSandbox()
            await sandbox.start("/tmp/repo", config=SandboxConfig(image="alpine:3.20"))
            container.exec_run.side_effect = [
                APIError('exec: "bash": executable file not found in $PATH'),
                (0, (b"sh-ok\n", b"")),
            ]
            res = await sandbox.exec("echo sh-ok")
            assert res.exit_code == 0
            assert res.stdout == "sh-ok\n"


class TestSearchCodebaseFallbacks:
    """SearchCodebaseTool falls back to host workspace search or in-container grep."""

    @pytest.mark.asyncio
    async def test_host_workspace_fallback_when_rg_missing(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.cs").write_text("public class App {}\n", encoding="utf-8")
        (tmp_path / "src" / "other.txt").write_text("public class Ignored {}\n", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("public class GitMeta {}\n", encoding="utf-8")
        (tmp_path / "bin.dat").write_bytes(b"\x00public class Binary")

        sandbox = AsyncMock()
        sandbox.host_workspace_path = str(tmp_path)
        sandbox.exec.return_value = ExecResult(exit_code=127, stdout="", stderr="sh: rg: not found")

        tool = SearchCodebaseTool()
        result = await tool.execute(
            {"pattern": r"class\s+App", "file_glob": "*.cs", "max_results": 10},
            sandbox,
        )
        assert result.exit_code == 0
        matches = json.loads(result.output)
        assert matches == [
            {"file": "/workspace/src/main.cs", "line": 1, "content": "public class App {}"}
        ]

        bad_regex = await tool.execute({"pattern": "[unclosed"}, sandbox)
        assert bad_regex.exit_code == 2
        assert "Invalid regex pattern" in (bad_regex.error or "")

    @pytest.mark.asyncio
    async def test_container_grep_fallback_when_no_host_path(self) -> None:
        sandbox = AsyncMock()
        sandbox.host_workspace_path = None
        sandbox.exec.side_effect = [
            ExecResult(exit_code=127, stdout="", stderr="rg: command not found"),
            ExecResult(
                exit_code=0,
                stdout='/workspace/lib/main.hs:3:main = putStrLn "hi"\n',
                stderr="",
            ),
        ]

        tool = SearchCodebaseTool()
        result = await tool.execute({"pattern": "putStrLn", "file_glob": "*.hs"}, sandbox)
        assert result.exit_code == 0
        matches = json.loads(result.output)
        assert matches == [
            {"file": "/workspace/lib/main.hs", "line": 3, "content": 'main = putStrLn "hi"'}
        ]
        grep_cmd = sandbox.exec.call_args_list[1].args[0]
        assert "grep -rnE" in grep_cmd
        assert "--include='*.hs'" in grep_cmd


class TestBwrapHostToolchainsAndSecurity:
    """BwrapSandbox mounts host user toolchains read-only and blocks credential paths."""

    @pytest.mark.asyncio
    async def test_bwrap_mounts_toolchains_ro_and_excludes_secrets(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        for d in (
            ".cargo",
            ".dotnet",
            ".foundry",
            ".ghcup",
            ".pyenv",
            ".local/share/mise",
            ".ssh",
            ".aws",
            ".agent-forge",
        ):
            (fake_home / d).mkdir(parents=True)
        extra_sdk = tmp_path / "custom-zig-sdk"
        extra_sdk.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()

        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda _cls: fake_home))

        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            sandbox = BwrapSandbox()
            await sandbox.start(
                str(repo),
                SandboxConfig(
                    backend="bwrap",
                    network_enabled=False,
                    mount_host_toolchains=True,
                    extra_ro_binds=[str(extra_sdk)],
                ),
            )
            argv = sandbox._build_bwrap_argv("cargo --version")
            env_vars = sandbox._build_env_vars()

        ro_binds = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--ro-bind"]
        for expected in (".cargo", ".dotnet", ".foundry", ".ghcup", ".pyenv", ".local/share/mise"):
            assert str((fake_home / expected).resolve()) in ro_binds
        assert str(extra_sdk.resolve()) in ro_binds
        assert str(fake_home.resolve()) not in ro_binds
        for secret in (".ssh", ".aws", ".agent-forge"):
            assert str((fake_home / secret).resolve()) not in ro_binds
        assert env_vars["HOME"] == "/tmp/home"

        # Disabling mount_host_toolchains skips user home toolchains
        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            minimal = BwrapSandbox()
            await minimal.start(
                str(repo),
                SandboxConfig(backend="bwrap", mount_host_toolchains=False),
            )
            minimal_argv = minimal._build_bwrap_argv("echo hi")
        minimal_ro = [
            minimal_argv[i + 1] for i, tok in enumerate(minimal_argv) if tok == "--ro-bind"
        ]
        assert str((fake_home / ".cargo").resolve()) not in minimal_ro

    def test_sensitive_extra_ro_binds_rejected_in_config_and_validator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda _cls: fake_home))

        for bad in (
            str(fake_home),
            str(fake_home / ".ssh"),
            str(fake_home / ".aws"),
            str(fake_home / ".agent-forge"),
            str(fake_home / ".config" / "gcloud"),
            "relative/path",
        ):
            with pytest.raises(ValueError, match=r"."):
                validate_ro_bind_path(bad)
            with pytest.raises(ValueError, match=r"."):
                ForgeConfig(sandbox=SandboxSettings(extra_ro_binds=[bad]))

        cfg = ForgeConfig(
            sandbox=SandboxSettings(mount_host_toolchains=False, extra_ro_binds=["/opt/custom-sdk"])
        )
        sb_cfg = build_sandbox_config(cfg)
        assert sb_cfg.mount_host_toolchains is False
        assert sb_cfg.extra_ro_binds == [str(validate_ro_bind_path("/opt/custom-sdk"))]

    @pytest.mark.asyncio
    async def test_bwrap_start_rejects_sensitive_extra_ro_bind(self, tmp_path: Path) -> None:
        with (
            patch("agent_forge.sandbox.bwrap.platform.system", return_value="Linux"),
            patch("agent_forge.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            sandbox = BwrapSandbox()
            with pytest.raises(SandboxStartupError, match="sensitive"):
                await sandbox.start(
                    str(tmp_path),
                    SandboxConfig(backend="bwrap", extra_ro_binds=["~/.ssh"]),
                )
