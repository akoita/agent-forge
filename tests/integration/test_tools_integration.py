"""Integration tests for tools executing in a real Docker sandbox.

Requires Docker to be running.
Mark all tests with @pytest.mark.integration so they can be
deselected with ``-m 'not integration'``.
"""

from __future__ import annotations

import pytest

from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.docker import DockerSandbox
from agent_forge.tools import (
    EditFileTool,
    ListDirectoryTool,
    ReadFileTool,
    RunShellTool,
    SearchCodebaseTool,
    WriteFileTool,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def sandbox(tmp_path: object) -> DockerSandbox:  # type: ignore[override]
    """Create a real Docker sandbox backed by a temp directory."""
    import tempfile
    from pathlib import Path

    repo = Path(tempfile.mkdtemp())
    # Seed a test file
    (repo / "hello.py").write_text("print('hello world')\n")
    (repo / "src").mkdir()
    (repo / "src" / "utils.py").write_text("def add(a, b):\n    return a + b\n")

    sb = DockerSandbox()
    await sb.start(str(repo), SandboxConfig(timeout_seconds=60))
    yield sb  # type: ignore[misc]
    await sb.stop()


class TestReadFileIntegration:
    @pytest.mark.asyncio
    async def test_read_existing(self, sandbox: DockerSandbox) -> None:
        tool = ReadFileTool()
        result = await tool.execute({"path": "hello.py"}, sandbox)
        assert result.exit_code == 0
        assert "hello world" in result.output


class TestWriteFileIntegration:
    @pytest.mark.asyncio
    async def test_write_and_read_back(self, sandbox: DockerSandbox) -> None:
        write_tool = WriteFileTool()
        result = await write_tool.execute({"path": "new_file.py", "content": "x = 42\n"}, sandbox)
        assert result.exit_code == 0

        read_tool = ReadFileTool()
        result = await read_tool.execute({"path": "new_file.py"}, sandbox)
        assert result.exit_code == 0
        assert "x = 42" in result.output


class TestEditFileIntegration:
    @pytest.mark.asyncio
    async def test_edit_existing(self, sandbox: DockerSandbox) -> None:
        tool = EditFileTool()
        result = await tool.execute(
            {"path": "hello.py", "old_text": "hello world", "new_text": "goodbye world"},
            sandbox,
        )
        assert result.exit_code == 0

        read_tool = ReadFileTool()
        result = await read_tool.execute({"path": "hello.py"}, sandbox)
        assert "goodbye world" in result.output


class TestRunShellIntegration:
    @pytest.mark.asyncio
    async def test_echo(self, sandbox: DockerSandbox) -> None:
        tool = RunShellTool()
        result = await tool.execute({"command": "echo 'integration test'"}, sandbox)
        assert result.exit_code == 0
        assert "integration test" in result.output

    @pytest.mark.asyncio
    async def test_python_version(self, sandbox: DockerSandbox) -> None:
        tool = RunShellTool()
        result = await tool.execute({"command": "python3 --version"}, sandbox)
        assert result.exit_code == 0
        assert "Python 3" in result.output


class TestSearchCodebaseIntegration:
    @pytest.mark.asyncio
    async def test_search_pattern(self, sandbox: DockerSandbox) -> None:
        tool = SearchCodebaseTool()
        result = await tool.execute({"pattern": "def add"}, sandbox)
        assert result.exit_code == 0
        assert "utils.py" in result.output

    @pytest.mark.asyncio
    async def test_search_with_glob(self, sandbox: DockerSandbox) -> None:
        tool = SearchCodebaseTool()
        result = await tool.execute({"pattern": "print", "file_glob": "*.py"}, sandbox)
        assert result.exit_code == 0


class TestListDirectoryIntegration:
    @pytest.mark.asyncio
    async def test_list_workspace(self, sandbox: DockerSandbox) -> None:
        tool = ListDirectoryTool()
        result = await tool.execute({}, sandbox)
        assert result.exit_code == 0
        assert "hello.py" in result.output
