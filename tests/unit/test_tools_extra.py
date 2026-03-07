"""Unit tests for run_shell, search_codebase, and edit_file tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from agent_forge.sandbox.base import ExecResult
from agent_forge.tools import (
    EditFileTool,
    RunShellTool,
    SearchCodebaseTool,
    create_default_registry,
)

# ---------------------------------------------------------------------------
# RunShellTool
# ---------------------------------------------------------------------------


class TestRunShell:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout="hello\n", stderr="")

        tool = RunShellTool()
        result = await tool.execute({"command": "echo hello"}, sandbox)

        assert result.exit_code == 0
        assert result.output == "hello\n"
        sandbox.exec.assert_called_once_with("echo hello", timeout_seconds=30)

    @pytest.mark.asyncio
    async def test_missing_command(self) -> None:
        sandbox = AsyncMock()
        tool = RunShellTool()
        result = await tool.execute({}, sandbox)
        assert result.exit_code == 1
        assert "Missing" in (result.error or "")

    @pytest.mark.asyncio
    async def test_blocked_rm_rf(self) -> None:
        sandbox = AsyncMock()
        tool = RunShellTool()
        result = await tool.execute({"command": "rm -rf /"}, sandbox)
        assert result.exit_code == 1
        assert "blocked" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_blocked_fork_bomb(self) -> None:
        sandbox = AsyncMock()
        tool = RunShellTool()
        result = await tool.execute({"command": ":(){ :|:& };:"}, sandbox)
        assert result.exit_code == 1
        assert "blocked" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_blocked_mkfs(self) -> None:
        sandbox = AsyncMock()
        tool = RunShellTool()
        result = await tool.execute({"command": "mkfs.ext4 /dev/sda"}, sandbox)
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_blocked_dd(self) -> None:
        sandbox = AsyncMock()
        tool = RunShellTool()
        result = await tool.execute({"command": "dd if=/dev/zero of=disk.img"}, sandbox)
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_blocked_shutdown(self) -> None:
        sandbox = AsyncMock()
        tool = RunShellTool()
        result = await tool.execute({"command": "shutdown now"}, sandbox)
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_timeout_capped(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout="ok", stderr="")

        tool = RunShellTool()
        await tool.execute({"command": "sleep 1", "timeout_seconds": 999}, sandbox)

        # Timeout should be capped at 120
        sandbox.exec.assert_called_once_with("sleep 1", timeout_seconds=120)

    @pytest.mark.asyncio
    async def test_output_truncation(self) -> None:
        sandbox = AsyncMock()
        large_output = "x" * (100 * 1024)  # 100KB
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout=large_output, stderr="")

        tool = RunShellTool()
        result = await tool.execute({"command": "cat bigfile"}, sandbox)

        assert result.exit_code == 0
        assert "truncated" in result.output.lower()
        assert len(result.output) < len(large_output)

    @pytest.mark.asyncio
    async def test_stderr_passed_through(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=1, stdout="", stderr="error msg")

        tool = RunShellTool()
        result = await tool.execute({"command": "bad_cmd"}, sandbox)

        assert result.exit_code == 1
        assert result.error == "error msg"


# ---------------------------------------------------------------------------
# SearchCodebaseTool
# ---------------------------------------------------------------------------

_RG_MATCH_LINE = json.dumps(
    {
        "type": "match",
        "data": {
            "path": {"text": "/workspace/src/main.py"},
            "line_number": 42,
            "lines": {"text": "def foo():\n"},
        },
    }
)

_RG_SUMMARY_LINE = json.dumps({"type": "summary", "data": {"stats": {}}})


class TestSearchCodebase:
    @pytest.mark.asyncio
    async def test_search_success(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(
            exit_code=0,
            stdout=f"{_RG_MATCH_LINE}\n{_RG_SUMMARY_LINE}\n",
            stderr="",
        )

        tool = SearchCodebaseTool()
        result = await tool.execute({"pattern": "def foo"}, sandbox)

        assert result.exit_code == 0
        matches = json.loads(result.output)
        assert len(matches) == 1
        assert matches[0]["file"] == "/workspace/src/main.py"
        assert matches[0]["line"] == 42
        assert "def foo" in matches[0]["content"]

    @pytest.mark.asyncio
    async def test_no_matches(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=1, stdout="", stderr="")

        tool = SearchCodebaseTool()
        result = await tool.execute({"pattern": "nonexistent"}, sandbox)

        assert result.exit_code == 0
        assert "No matches" in result.output

    @pytest.mark.asyncio
    async def test_missing_pattern(self) -> None:
        sandbox = AsyncMock()
        tool = SearchCodebaseTool()
        result = await tool.execute({}, sandbox)
        assert result.exit_code == 1
        assert "Missing" in (result.error or "")

    @pytest.mark.asyncio
    async def test_with_file_glob(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=1, stdout="", stderr="")

        tool = SearchCodebaseTool()
        await tool.execute({"pattern": "test", "file_glob": "*.py"}, sandbox)

        call_args = sandbox.exec.call_args[0][0]
        assert "--glob" in call_args

    @pytest.mark.asyncio
    async def test_rg_error(self) -> None:
        """Exit code 2 = regex/rg error (not 'no matches')."""
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=2, stdout="", stderr="regex parse error")

        tool = SearchCodebaseTool()
        result = await tool.execute({"pattern": "[invalid"}, sandbox)

        assert result.exit_code == 2
        assert "regex parse error" in (result.error or "")

    @pytest.mark.asyncio
    async def test_malformed_json_lines(self) -> None:
        """Malformed JSON lines in rg output should be skipped gracefully."""
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(
            exit_code=0,
            stdout=f"NOT_JSON\n{_RG_MATCH_LINE}\nALSO_NOT_JSON\n",
            stderr="",
        )

        tool = SearchCodebaseTool()
        result = await tool.execute({"pattern": "def foo"}, sandbox)

        assert result.exit_code == 0
        matches = json.loads(result.output)
        assert len(matches) == 1  # Only the valid match

    @pytest.mark.asyncio
    async def test_max_results_passed(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=1, stdout="", stderr="")

        tool = SearchCodebaseTool()
        await tool.execute({"pattern": "test", "max_results": 5}, sandbox)

        call_args = sandbox.exec.call_args[0][0]
        assert "--max-count 5" in call_args


# ---------------------------------------------------------------------------
# EditFileTool
# ---------------------------------------------------------------------------


class TestEditFile:
    @pytest.mark.asyncio
    async def test_edit_success(self) -> None:
        sandbox = AsyncMock()
        sandbox.read_file.return_value = "def hello():\n    pass\n"
        sandbox.write_file.return_value = None

        tool = EditFileTool()
        result = await tool.execute(
            {"path": "main.py", "old_text": "pass", "new_text": "print('hi')"},
            sandbox,
        )

        assert result.exit_code == 0
        assert "Successfully" in result.output
        sandbox.write_file.assert_called_once_with(
            "/workspace/main.py",
            "def hello():\n    print('hi')\n",
        )

    @pytest.mark.asyncio
    async def test_old_text_not_found(self) -> None:
        sandbox = AsyncMock()
        sandbox.read_file.return_value = "def hello():\n    pass\n"

        tool = EditFileTool()
        result = await tool.execute(
            {"path": "main.py", "old_text": "nonexistent", "new_text": "replacement"},
            sandbox,
        )

        assert result.exit_code == 1
        assert "not found" in (result.error or "").lower()
        assert "def hello" in (result.error or "")  # file preview

    @pytest.mark.asyncio
    async def test_file_not_found(self) -> None:
        sandbox = AsyncMock()
        sandbox.read_file.side_effect = FileNotFoundError("not found")

        tool = EditFileTool()
        result = await tool.execute(
            {"path": "missing.py", "old_text": "a", "new_text": "b"},
            sandbox,
        )

        assert result.exit_code == 1
        assert "not found" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_path_traversal(self) -> None:
        sandbox = AsyncMock()
        tool = EditFileTool()
        result = await tool.execute(
            {"path": "../etc/passwd", "old_text": "a", "new_text": "b"},
            sandbox,
        )
        assert result.exit_code == 1
        assert "traversal" in (result.error or "")

    @pytest.mark.asyncio
    async def test_missing_path(self) -> None:
        sandbox = AsyncMock()
        tool = EditFileTool()
        result = await tool.execute({"old_text": "a", "new_text": "b"}, sandbox)
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_missing_old_text(self) -> None:
        sandbox = AsyncMock()
        tool = EditFileTool()
        result = await tool.execute({"path": "main.py", "new_text": "b"}, sandbox)
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_replaces_only_first_occurrence(self) -> None:
        sandbox = AsyncMock()
        sandbox.read_file.return_value = "aaa"
        sandbox.write_file.return_value = None

        tool = EditFileTool()
        await tool.execute(
            {"path": "file.txt", "old_text": "a", "new_text": "b"},
            sandbox,
        )

        sandbox.write_file.assert_called_once_with("/workspace/file.txt", "baa")


# ---------------------------------------------------------------------------
# Default Registry
# ---------------------------------------------------------------------------


class TestDefaultRegistry:
    def test_all_tools_registered(self) -> None:
        registry = create_default_registry()
        names = {d.name for d in registry.list_definitions()}
        assert names == {
            "read_file",
            "write_file",
            "edit_file",
            "list_directory",
            "run_shell",
            "search_codebase",
        }
        assert len(registry) == 6
