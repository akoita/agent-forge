"""Tool system — base classes and built-in tools."""

from agent_forge.tools.base import Tool, ToolRegistry, ToolResult, validate_path
from agent_forge.tools.edit_file import EditFileTool
from agent_forge.tools.list_directory import ListDirectoryTool
from agent_forge.tools.read_file import ReadFileTool
from agent_forge.tools.run_shell import RunShellTool
from agent_forge.tools.search_codebase import SearchCodebaseTool
from agent_forge.tools.write_file import WriteFileTool

__all__ = [
    "EditFileTool",
    "ListDirectoryTool",
    "ReadFileTool",
    "RunShellTool",
    "SearchCodebaseTool",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "WriteFileTool",
    "create_default_registry",
    "validate_path",
]


def create_default_registry() -> ToolRegistry:
    """Create a ToolRegistry with all built-in tools registered."""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(ListDirectoryTool())
    registry.register(RunShellTool())
    registry.register(SearchCodebaseTool())
    return registry
