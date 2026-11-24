"""Tool system — base classes and built-in tools."""

from collections.abc import Callable, Iterable
from importlib.metadata import EntryPoint

from agent_forge.tools.apply_patch import ApplyPatchTool
from agent_forge.tools.base import Tool, ToolRegistry, ToolResult, validate_path
from agent_forge.tools.create_pr import CreatePRTool
from agent_forge.tools.edit_file import EditFileTool
from agent_forge.tools.git_commit import GitCommitTool
from agent_forge.tools.git_create_branch import GitCreateBranchTool
from agent_forge.tools.git_diff import GitDiffTool
from agent_forge.tools.git_push import GitPushTool
from agent_forge.tools.list_directory import ListDirectoryTool
from agent_forge.tools.mcp import (
    HttpMCPTransport,
    MCPCircuitBreaker,
    MCPCircuitOpenError,
    MCPClientManager,
    MCPError,
    MCPServerClient,
    MCPServerStatus,
    MCPTimeoutError,
    MCPTool,
    MCPToolAnnotations,
    MCPToolDescriptor,
    MCPTransport,
    MCPTransportError,
    StdioMCPTransport,
    normalize_mcp_tool_name,
)
from agent_forge.tools.plugins import (
    TOOL_PLUGIN_GROUP,
    ToolPluginError,
    discover_tool_plugins,
    load_plugin_tools,
    register_plugin_tools,
)
from agent_forge.tools.read_file import ReadFileTool
from agent_forge.tools.run_shell import RunShellTool
from agent_forge.tools.search_codebase import SearchCodebaseTool
from agent_forge.tools.subagent import SpawnSubagentTool
from agent_forge.tools.terminal import (
    ShellCancelTool,
    ShellPollTool,
    ShellStartTool,
    ShellWriteTool,
)
from agent_forge.tools.write_file import WriteFileTool

__all__ = [
    "TOOL_PLUGIN_GROUP",
    "ApplyPatchTool",
    "CreatePRTool",
    "EditFileTool",
    "GitCommitTool",
    "GitCreateBranchTool",
    "GitDiffTool",
    "GitPushTool",
    "HttpMCPTransport",
    "ListDirectoryTool",
    "MCPCircuitBreaker",
    "MCPCircuitOpenError",
    "MCPClientManager",
    "MCPError",
    "MCPServerClient",
    "MCPServerStatus",
    "MCPTimeoutError",
    "MCPTool",
    "MCPToolAnnotations",
    "MCPToolDescriptor",
    "MCPTransport",
    "MCPTransportError",
    "ReadFileTool",
    "RunShellTool",
    "SearchCodebaseTool",
    "ShellCancelTool",
    "ShellPollTool",
    "ShellStartTool",
    "ShellWriteTool",
    "SpawnSubagentTool",
    "StdioMCPTransport",
    "Tool",
    "ToolPluginError",
    "ToolRegistry",
    "ToolResult",
    "WriteFileTool",
    "create_default_registry",
    "discover_tool_plugins",
    "load_plugin_tools",
    "normalize_mcp_tool_name",
    "register_plugin_tools",
    "validate_path",
]


def create_default_registry(
    *,
    load_plugins: bool = True,
    entry_points_factory: Callable[[], Iterable[EntryPoint]] | None = None,
) -> ToolRegistry:
    """Create a ToolRegistry with built-ins and optional external plugins."""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(ApplyPatchTool())
    registry.register(ListDirectoryTool())
    registry.register(RunShellTool())
    registry.register(ShellStartTool())
    registry.register(ShellPollTool())
    registry.register(ShellWriteTool())
    registry.register(ShellCancelTool())
    registry.register(SearchCodebaseTool())
    registry.register(GitDiffTool())
    registry.register(GitCommitTool())
    registry.register(GitCreateBranchTool())
    registry.register(GitPushTool())
    registry.register(CreatePRTool())
    if load_plugins:
        register_plugin_tools(registry, entry_points_factory=entry_points_factory)
    return registry
