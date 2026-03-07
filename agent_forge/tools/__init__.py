"""Tool system — base classes and built-in tools."""

from agent_forge.tools.base import Tool, ToolRegistry, ToolResult, validate_path
from agent_forge.tools.list_directory import ListDirectoryTool
from agent_forge.tools.read_file import ReadFileTool
from agent_forge.tools.write_file import WriteFileTool

__all__ = [
    "ListDirectoryTool",
    "ReadFileTool",
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
    registry.register(ListDirectoryTool())
    return registry
