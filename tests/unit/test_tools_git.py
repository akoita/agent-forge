"""Unit tests for git-aware tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from agent_forge.sandbox.base import ExecResult
from agent_forge.tools import CreatePRTool, GitCommitTool, GitCreateBranchTool, GitDiffTool

_PULLS_URL = "https://api.github.com/repos/akoita/agent-forge/pulls"


class TestGitDiffTool:
    @pytest.mark.asyncio
    async def test_unstaged_diff(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout="diff --git\n", stderr="")

        result = await GitDiffTool().execute({}, sandbox)

        assert result.exit_code == 0
        assert result.output == "diff --git\n"
        sandbox.exec.assert_called_once_with(
            "git -C /workspace diff --no-ext-diff --",
            timeout_seconds=30,
        )

    @pytest.mark.asyncio
    async def test_staged_diff_with_path(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")

        result = await GitDiffTool().execute(
            {"staged": True, "path": "src/main.py"},
            sandbox,
        )

        assert result.exit_code == 0
        sandbox.exec.assert_called_once_with(
            "git -C /workspace diff --cached --no-ext-diff -- src/main.py",
            timeout_seconds=30,
        )

    @pytest.mark.asyncio
    async def test_base_ref_accepts_revision_syntax(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")

        result = await GitDiffTool().execute({"base_ref": "HEAD~1"}, sandbox)

        assert result.exit_code == 0
        sandbox.exec.assert_called_once_with(
            "git -C /workspace diff --no-ext-diff 'HEAD~1' --",
            timeout_seconds=30,
        )

    @pytest.mark.asyncio
    async def test_rejects_conflicting_modes(self) -> None:
        sandbox = AsyncMock()

        result = await GitDiffTool().execute(
            {"staged": True, "base_ref": "main"},
            sandbox,
        )

        assert result.exit_code == 1
        assert "mutually exclusive" in (result.error or "")

    @pytest.mark.asyncio
    async def test_rejects_invalid_path(self) -> None:
        sandbox = AsyncMock()

        result = await GitDiffTool().execute({"path": "../etc/passwd"}, sandbox)

        assert result.exit_code == 1
        assert "traversal" in (result.error or "")


class TestGitCommitTool:
    @pytest.mark.asyncio
    async def test_requires_message(self) -> None:
        sandbox = AsyncMock()

        result = await GitCommitTool().execute({}, sandbox)

        assert result.exit_code == 1
        assert "message" in (result.error or "")

    @pytest.mark.asyncio
    async def test_rejects_when_no_staged_changes(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")

        result = await GitCommitTool().execute({"message": "feat: test"}, sandbox)

        assert result.exit_code == 1
        assert "No staged changes" in (result.error or "")
        sandbox.exec.assert_called_once_with(
            "git -C /workspace diff --cached --quiet",
            timeout_seconds=30,
        )

    @pytest.mark.asyncio
    async def test_commit_success(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.side_effect = [
            ExecResult(exit_code=1, stdout="", stderr=""),
            ExecResult(exit_code=0, stdout="[main 1234567] feat: test\n", stderr=""),
        ]

        result = await GitCommitTool().execute({"message": "feat: test"}, sandbox)

        assert result.exit_code == 0
        assert "[main 1234567]" in result.output
        assert sandbox.exec.call_count == 2
        assert sandbox.exec.call_args_list[1].args[0] == "git -C /workspace commit -m 'feat: test'"


class TestGitCreateBranchTool:
    @pytest.mark.asyncio
    async def test_create_branch_success(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(
            exit_code=0,
            stdout="Switched to a new branch 'feature/test'\n",
            stderr="",
        )

        result = await GitCreateBranchTool().execute(
            {"branch_name": "feature/test", "base_ref": "main"},
            sandbox,
        )

        assert result.exit_code == 0
        sandbox.exec.assert_called_once_with(
            "git -C /workspace checkout -b feature/test main",
            timeout_seconds=30,
        )

    @pytest.mark.asyncio
    async def test_rejects_invalid_branch_name(self) -> None:
        sandbox = AsyncMock()

        result = await GitCreateBranchTool().execute({"branch_name": "bad branch"}, sandbox)

        assert result.exit_code == 1
        assert "Invalid branch_name" in (result.error or "")


class TestCreatePRTool:
    @pytest.mark.asyncio
    async def test_requires_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sandbox = AsyncMock()
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        result = await CreatePRTool().execute({"title": "Test PR"}, sandbox)

        assert result.exit_code == 1
        assert "GitHub token" in (result.error or "")

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_pr_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sandbox = AsyncMock()
        sandbox.exec.side_effect = [
            ExecResult(
                exit_code=0,
                stdout="git@github.com:akoita/agent-forge.git\n",
                stderr="",
            ),
            ExecResult(exit_code=0, stdout="feature/test\n", stderr=""),
            ExecResult(exit_code=0, stdout="origin/main\n", stderr=""),
        ]
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        route = respx.post(_PULLS_URL).respond(
            201,
            json={"number": 111, "html_url": "https://github.com/akoita/agent-forge/pull/111"},
        )

        result = await CreatePRTool().execute({"title": "Test PR", "body": "Hello"}, sandbox)

        assert result.exit_code == 0
        assert "#111" in result.output
        assert route.called
        request = route.calls[0].request
        assert request.headers["Authorization"] == "Bearer test-token"
        assert json.loads(request.content) == {
            "title": "Test PR",
            "head": "feature/test",
            "base": "main",
            "body": "Hello",
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_surfaces_github_api_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sandbox = AsyncMock()
        sandbox.exec.side_effect = [
            ExecResult(exit_code=0, stdout="git@github.com:akoita/agent-forge.git\n", stderr=""),
            ExecResult(exit_code=0, stdout="feature/test\n", stderr=""),
            ExecResult(exit_code=0, stdout="origin/main\n", stderr=""),
        ]
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        respx.post(_PULLS_URL).respond(422, json={"message": "Validation Failed"})

        result = await CreatePRTool().execute({"title": "Test PR"}, sandbox)

        assert result.exit_code == 1
        assert "Validation Failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_rejects_invalid_repo_argument(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sandbox = AsyncMock()
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")

        result = await CreatePRTool().execute(
            {"title": "Test PR", "repo": "akoita"},
            sandbox,
        )

        assert result.exit_code == 1
        assert "owner/repo" in (result.error or "")

    @pytest.mark.asyncio
    @respx.mock
    async def test_surfaces_network_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sandbox = AsyncMock()
        sandbox.exec.side_effect = [
            ExecResult(exit_code=0, stdout="git@github.com:akoita/agent-forge.git\n", stderr=""),
            ExecResult(exit_code=0, stdout="feature/test\n", stderr=""),
            ExecResult(exit_code=0, stdout="origin/main\n", stderr=""),
        ]
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        respx.post(_PULLS_URL).mock(side_effect=httpx.ConnectError("boom"))

        result = await CreatePRTool().execute({"title": "Test PR"}, sandbox)

        assert result.exit_code == 1
        assert "request failed" in (result.error or "").lower()
