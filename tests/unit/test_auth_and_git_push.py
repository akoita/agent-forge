"""Unit tests for CredentialStore, agent-forge auth CLI, and host GitPushTool."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from click.testing import CliRunner

from agent_forge.agent.approvals import ApprovalGate, ApprovalRequest
from agent_forge.auth import (
    CredentialStore,
    get_service_status,
    mask_token,
    resolve_auth_env_fallback,
    resolve_github_token,
)
from agent_forge.cli import main
from agent_forge.config import MCPServerSettings, MCPSettings
from agent_forge.eval.deterministic import SubprocessEvalSandbox
from agent_forge.sandbox.base import ExecResult, SandboxConfig, sanitize_sandbox_env
from agent_forge.tools import CreatePRTool, GitPushTool, MCPClientManager

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    cmd = ["git", "-c", f"core.hooksPath={os.devnull}", *args]
    proc = subprocess.run(  # noqa: S603
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return proc.stdout.strip()


def _init_repo_with_bare_remote(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")

    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test Author")
    _git(repo, "config", "user.email", "author@example.com")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "main")
    return repo, bare


class TestCredentialStore:
    """Secure host-side credential persistence and precedence rules."""

    def test_store_permissions_masking_and_resolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        assert mask_token("") == ""
        assert mask_token("short") == "***"
        assert mask_token("ghp_1234567890abcd") == "ghp_...abcd"

        store = CredentialStore()
        sample_val = "".join(["ghp_secret_", "9876"])
        rec = store.set("github", sample_val, username="akoita", host="github.com")
        assert rec.service == "github"
        mode = stat.S_IMODE(store.path.stat().st_mode)
        assert mode == 0o600

        resolved, source = resolve_github_token()
        assert resolved == sample_val
        assert source == "store"
        assert resolve_auth_env_fallback("GITHUB_TOKEN") == sample_val

        status = get_service_status("github")
        assert status.configured is True
        assert status.token_preview == mask_token(sample_val)

        env_val = "".join(["env_override_", "1111"])
        monkeypatch.setenv("GITHUB_TOKEN", env_val)
        resolved_env, src_env = resolve_github_token()
        assert resolved_env == env_val
        assert src_env == "env:GITHUB_TOKEN"

        assert store.delete("github") is True
        assert store.delete("github") is False

    def test_cli_auth_login_status_and_logout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        runner = CliRunner()
        res_login = runner.invoke(
            main,
            ["auth", "login", "github", "--token-stdin", "--host", "github.com"],
            input="ghp_clitoken_1234\n",
        )
        assert res_login.exit_code == 0, res_login.output
        assert "ghp_...1234" in res_login.output
        assert "ghp_clitoken_1234" not in res_login.output

        res_status = runner.invoke(main, ["auth", "status", "--output-format", "json"])
        assert res_status.exit_code == 0, res_status.output
        payload = json.loads(res_status.output)
        gh_row = next(row for row in payload["services"] if row["service"] == "github")
        assert gh_row["configured"] is True
        assert gh_row["source"] == "store"
        assert gh_row["token_preview"] == mask_token("".join(["ghp_cli", "token_1234"]))

        res_logout = runner.invoke(main, ["auth", "logout", "github"])
        assert res_logout.exit_code == 0
        assert CredentialStore().get("github") is None


class TestCredentialStoreIntegration:
    """CreatePRTool and HttpMCPTransport consume CredentialStore without env leakage."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_pr_and_mcp_http_use_stored_credentials(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        CredentialStore().set("github", "ghp_stored_for_pr_and_mcp", host="github.com")

        sandbox = AsyncMock()
        sandbox.exec.side_effect = [
            ExecResult(
                exit_code=0, stdout="https://github.com/akoita/agent-forge.git\n", stderr=""
            ),
            ExecResult(exit_code=0, stdout="feat/test\n", stderr=""),
            ExecResult(exit_code=0, stdout="origin/main\n", stderr=""),
        ]
        route = respx.post("https://api.github.com/repos/akoita/agent-forge/pulls").mock(
            return_value=httpx.Response(
                201,
                json={"number": 42, "html_url": "https://github.com/akoita/agent-forge/pull/42"},
            )
        )
        res = await CreatePRTool().execute({"title": "Stored Auth PR"}, sandbox)
        assert res.exit_code == 0
        assert route.calls[0].request.headers["authorization"] == "Bearer ghp_stored_for_pr_and_mcp"

        seen_mcp_headers: list[str] = []

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            seen_mcp_headers.append(request.headers.get("authorization", ""))
            body = json.loads(request.content)
            rid = body.get("id")
            if body.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"tools": []}})

        respx.post("https://mcp.example.com/rpc").mock(side_effect=_mcp_handler)
        mcp_cfg = MCPSettings(
            servers={
                "gh-mcp": MCPServerSettings(
                    transport="http",
                    url="https://mcp.example.com/rpc",
                    auth_env="GITHUB_TOKEN",
                )
            }
        )
        mgr = MCPClientManager.from_settings(mcp_cfg)
        async with mgr:
            pass
        assert seen_mcp_headers[0] == "Bearer ghp_stored_for_pr_and_mcp"


class TestGitPushToolAndApprovals:
    """Host-brokered git_push from offline sandboxes with ApprovalGate gating."""

    @pytest.mark.asyncio
    async def test_git_push_blocks_protected_branch_and_pushes_feature_branch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path / "home")
        repo, bare = _init_repo_with_bare_remote(tmp_path)
        sandbox = SubprocessEvalSandbox()
        await sandbox.start(
            str(repo),
            SandboxConfig(backend="subprocess-eval", network_enabled=False),
        )
        try:
            tool = GitPushTool()
            refused = await tool.execute({}, sandbox)
            assert refused.exit_code == 1
            assert "protected branch 'main'" in (refused.error or "")

            _git(repo, "checkout", "-b", "feat/196-demo")
            (repo / "feature.txt").write_text("added\n", encoding="utf-8")
            _git(repo, "add", "feature.txt")
            _git(repo, "commit", "-m", "feat: add feature")

            pushed = await tool.execute({}, sandbox)
            assert pushed.exit_code == 0, pushed.error
            remote_head = _git(bare, "rev-parse", "refs/heads/feat/196-demo")
            local_head = _git(repo, "rev-parse", "HEAD")
            assert remote_head == local_head
        finally:
            await sandbox.stop()

    @pytest.mark.asyncio
    async def test_git_push_https_uses_ephemeral_askpass_and_scrubs_secrets(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path / "home")
        CredentialStore().set("github", "ghp_super_secret_9999", host="github.com")
        repo, _bare = _init_repo_with_bare_remote(tmp_path)
        _git(repo, "checkout", "-b", "feat/https-test")
        _git(repo, "remote", "set-url", "origin", "https://github.com/akoita/example.git")

        captured_env: list[dict[str, str] | None] = []

        async def _fake_exec(
            repo_root: Path,
            push_args: list[str],
            token: str | None,
        ) -> tuple[int, str, str]:
            del repo_root, push_args
            captured_env.append({"token": token or ""})
            return 1, "", "fatal: authentication failed for token ghp_super_secret_9999"

        sandbox = SubprocessEvalSandbox()
        await sandbox.start(str(repo), SandboxConfig(backend="subprocess-eval"))
        try:
            tool = GitPushTool()
            monkeypatch.setattr(tool, "_execute_push", _fake_exec)
            res = await tool.execute({}, sandbox)
        finally:
            await sandbox.stop()

        assert res.exit_code == 1
        assert captured_env == [{"token": "ghp_super_secret_9999"}]
        assert "ghp_super_secret_9999" not in (res.error or "")
        assert "[REDACTED]" in (res.error or "")
        # Sandbox env stripping still blocks token env vars from containers
        sanitized = sanitize_sandbox_env(
            {"PATH": "/usr/bin", "GITHUB_TOKEN": "secret", "AGENT_FORGE_TEST": "1"}
        )
        assert "GITHUB_TOKEN" not in sanitized
        assert sanitized["AGENT_FORGE_TEST"] == "1"

    @pytest.mark.asyncio
    async def test_approval_gate_classifies_git_push_and_force_with_lease(self) -> None:
        requests: list[ApprovalRequest] = []

        async def _cb(req: ApprovalRequest) -> bool:
            requests.append(req)
            return req.category != "git_force_push"

        gate = ApprovalGate(mode="prompt", callback=_cb)
        req1, dec1 = await gate.evaluate("git_push", {"branch": "feat/ok"})
        assert req1 is not None and req1.category == "git_push"
        assert dec1 is not None and dec1.approved is True

        req2, dec2 = await gate.evaluate(
            "git_push",
            {"branch": "feat/ok", "force_with_lease": True},
        )
        assert req2 is not None and req2.category == "git_force_push"
        assert dec2 is not None and dec2.approved is False
