"""Unit tests for the tool system.

Tests Tool ABC, ToolResult, ToolRegistry, validate_path, and
the built-in read_file, write_file, list_directory tools using
AsyncMock for the Sandbox.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent_forge.sandbox.base import ExecResult
from agent_forge.tools import (
    CreatePRTool,
    ListDirectoryTool,
    ReadFileTool,
    ToolRegistry,
    ToolResult,
    WriteFileTool,
    create_default_registry,
    validate_path,
)

# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------


class TestToolResult:
    def test_defaults(self) -> None:
        r = ToolResult(output="hello")
        assert r.output == "hello"
        assert r.error is None
        assert r.exit_code == 0
        assert r.execution_time_ms == 0

    def test_error(self) -> None:
        r = ToolResult(output="", error="boom", exit_code=1)
        assert r.exit_code == 1
        assert r.error == "boom"


# ---------------------------------------------------------------------------
# validate_path
# ---------------------------------------------------------------------------


class TestValidatePath:
    def test_relative_path(self) -> None:
        assert validate_path("src/main.py") == "/workspace/src/main.py"

    def test_absolute_workspace_path(self) -> None:
        assert validate_path("/workspace/src/main.py") == "/workspace/src/main.py"

    def test_rejects_traversal(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            validate_path("../etc/passwd")

    def test_rejects_traversal_absolute(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            validate_path("/workspace/../etc/passwd")

    def test_rejects_outside_workspace(self) -> None:
        with pytest.raises(ValueError, match="must be within"):
            validate_path("/etc/passwd")

    def test_normalizes_path(self) -> None:
        assert validate_path("/workspace/./src//main.py") == "/workspace/src/main.py"


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        registry = ToolRegistry()
        tool = ReadFileTool()
        registry.register(tool)
        assert registry.get("read_file") is tool

    def test_register_duplicate(self) -> None:
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(ReadFileTool())

    def test_get_unknown(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(KeyError, match="Unknown tool"):
            registry.get("nonexistent")

    def test_list_definitions(self) -> None:
        registry = create_default_registry()
        defs = registry.list_definitions()
        names = {d.name for d in defs}
        assert names == {
            "create_pr",
            "read_file",
            "write_file",
            "edit_file",
            "list_directory",
            "run_shell",
            "search_codebase",
            "git_diff",
            "git_commit",
            "git_create_branch",
        }
        assert len(registry) == 10

    def test_to_definition(self) -> None:
        tool = ReadFileTool()
        defn = tool.to_definition()
        assert defn.name == "read_file"
        assert "path" in defn.parameters["properties"]

    def test_new_tool_to_definition(self) -> None:
        tool = CreatePRTool()
        defn = tool.to_definition()
        assert defn.name == "create_pr"
        assert "title" in defn.parameters["properties"]


# ---------------------------------------------------------------------------
# ReadFileTool
# ---------------------------------------------------------------------------


class TestReadFile:
    @pytest.mark.asyncio
    async def test_read_success(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout="hello world", stderr="")

        tool = ReadFileTool()
        result = await tool.execute({"path": "main.py"}, sandbox)

        assert result.exit_code == 0
        assert result.output == "hello world"
        sandbox.exec.assert_called_once_with("cat /workspace/main.py")

    @pytest.mark.asyncio
    async def test_read_missing_path(self) -> None:
        sandbox = AsyncMock()
        tool = ReadFileTool()
        result = await tool.execute({}, sandbox)
        assert result.exit_code == 1
        assert "Missing" in (result.error or "")

    @pytest.mark.asyncio
    async def test_read_path_traversal(self) -> None:
        sandbox = AsyncMock()
        tool = ReadFileTool()
        result = await tool.execute({"path": "../etc/passwd"}, sandbox)
        assert result.exit_code == 1
        assert "traversal" in (result.error or "")

    @pytest.mark.asyncio
    async def test_read_file_not_found(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=1, stdout="", stderr="cat: no such file")

        tool = ReadFileTool()
        result = await tool.execute({"path": "nonexistent.py"}, sandbox)
        assert result.exit_code == 1
        assert "no such file" in (result.error or "")

    @pytest.mark.asyncio
    async def test_read_large_file_truncated(self) -> None:
        sandbox = AsyncMock()
        # Create content larger than 100KB
        large_content = "x" * (200 * 1024)
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout=large_content, stderr="")

        tool = ReadFileTool()
        result = await tool.execute({"path": "big.bin"}, sandbox)

        assert result.exit_code == 0
        assert "truncated" in result.output.lower()
        assert len(result.output) < len(large_content)


# ---------------------------------------------------------------------------
# WriteFileTool
# ---------------------------------------------------------------------------


class TestWriteFile:
    @pytest.mark.asyncio
    async def test_write_success(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=0, stdout="", stderr="")
        sandbox.write_file.return_value = None

        tool = WriteFileTool()
        result = await tool.execute({"path": "src/new.py", "content": "print('hi')"}, sandbox)

        assert result.exit_code == 0
        assert "Successfully wrote" in result.output
        sandbox.exec.assert_called_once_with("mkdir -p /workspace/src")
        sandbox.write_file.assert_called_once_with("/workspace/src/new.py", "print('hi')")

    @pytest.mark.asyncio
    async def test_write_missing_path(self) -> None:
        sandbox = AsyncMock()
        tool = WriteFileTool()
        result = await tool.execute({"content": "hello"}, sandbox)
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_write_path_traversal(self) -> None:
        sandbox = AsyncMock()
        tool = WriteFileTool()
        result = await tool.execute({"path": "../evil.sh", "content": "rm -rf /"}, sandbox)
        assert result.exit_code == 1
        assert "traversal" in (result.error or "")

    @pytest.mark.asyncio
    async def test_write_outside_workspace(self) -> None:
        sandbox = AsyncMock()
        tool = WriteFileTool()
        result = await tool.execute({"path": "/etc/crontab", "content": "bad"}, sandbox)
        assert result.exit_code == 1
        assert "must be within" in (result.error or "")

    @pytest.mark.asyncio
    async def test_write_mkdir_failure(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(
            exit_code=1, stdout="", stderr="read-only file system"
        )

        tool = WriteFileTool()
        result = await tool.execute({"path": "deep/nested/file.py", "content": "code"}, sandbox)

        assert result.exit_code == 1
        assert "read-only" in (result.error or "")


# ---------------------------------------------------------------------------
# ListDirectoryTool
# ---------------------------------------------------------------------------


class TestListDirectory:
    @pytest.mark.asyncio
    async def test_list_default(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(
            exit_code=0, stdout="/workspace\n/workspace/main.py\n", stderr=""
        )

        tool = ListDirectoryTool()
        result = await tool.execute({}, sandbox)

        assert result.exit_code == 0
        assert "/workspace/main.py" in result.output
        sandbox.exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_recursive(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(
            exit_code=0, stdout="/workspace\n/workspace/src\n/workspace/src/a.py\n", stderr=""
        )

        tool = ListDirectoryTool()
        result = await tool.execute({"path": ".", "recursive": True, "max_depth": 2}, sandbox)

        assert result.exit_code == 0
        # Verify the find command uses maxdepth
        call_args = sandbox.exec.call_args[0][0]
        assert "-maxdepth 2" in call_args

    @pytest.mark.asyncio
    async def test_list_path_traversal(self) -> None:
        sandbox = AsyncMock()
        tool = ListDirectoryTool()
        result = await tool.execute({"path": "../"}, sandbox)
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_list_error(self) -> None:
        sandbox = AsyncMock()
        sandbox.exec.return_value = ExecResult(exit_code=1, stdout="", stderr="permission denied")

        tool = ListDirectoryTool()
        result = await tool.execute({"path": "/workspace/secret"}, sandbox)
        assert result.exit_code == 1
        assert "permission denied" in (result.error or "")
