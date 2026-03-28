"""Tool system — base classes and built-in tools."""

from agent_forge.tools.base import Tool, ToolRegistry, ToolResult, validate_path
from agent_forge.tools.create_pr import CreatePRTool
from agent_forge.tools.edit_file import EditFileTool
from agent_forge.tools.git_commit import GitCommitTool
from agent_forge.tools.git_create_branch import GitCreateBranchTool
from agent_forge.tools.git_diff import GitDiffTool
from agent_forge.tools.list_directory import ListDirectoryTool
from agent_forge.tools.read_file import ReadFileTool
from agent_forge.tools.run_shell import RunShellTool
from agent_forge.tools.search_codebase import SearchCodebaseTool
from agent_forge.tools.write_file import WriteFileTool

__all__ = [
    "CreatePRTool",
    "EditFileTool",
    "GitCommitTool",
    "GitCreateBranchTool",
    "GitDiffTool",
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
    registry.register(GitDiffTool())
    registry.register(GitCommitTool())
    registry.register(GitCreateBranchTool())
    registry.register(CreatePRTool())
    return registry
