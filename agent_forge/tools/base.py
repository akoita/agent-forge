"""Tool base classes: Tool ABC, ToolResult, and ToolRegistry.

Defines the pluggable tool interface per spec § 4.2.
"""

from __future__ import annotations

import posixpath
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_forge.llm.base import ToolDefinition

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

WORKSPACE_ROOT = "/workspace"


@dataclass
class ToolResult:
    """Result of a tool execution."""

    output: str
    error: str | None = None
    exit_code: int = 0
    execution_time_ms: int = 0


# ---------------------------------------------------------------------------
# Path Validation
# ---------------------------------------------------------------------------


def validate_path(path: str) -> str:
    """Validate and normalize a workspace-relative path.

    Rejects path traversal (``..``) and ensures the resolved path is
    under ``/workspace``.

    Args:
        path: Relative or absolute path within the workspace.

    Returns:
        Normalized absolute path under ``/workspace``.

    Raises:
        ValueError: If the path escapes the workspace root.
    """
    # Reject obvious traversal
    if ".." in path.split("/"):
        msg = f"Path traversal detected: '{path}'"
        raise ValueError(msg)

    # Make absolute if relative
    resolved = posixpath.join(WORKSPACE_ROOT, path) if not posixpath.isabs(path) else path

    # Normalize (collapse //, resolve . etc.)
    resolved = posixpath.normpath(resolved)

    # Must be under workspace
    if not resolved.startswith(WORKSPACE_ROOT):
        msg = f"Path must be within {WORKSPACE_ROOT}: '{path}'"
        raise ValueError(msg)

    return resolved


# ---------------------------------------------------------------------------
# Tool ABC
# ---------------------------------------------------------------------------


class Tool(ABC):
    """Base class for all agent tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name, e.g. ``'read_file'``."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for the LLM."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema describing accepted arguments."""
        ...

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Execute the tool inside the given sandbox."""
        ...

    def to_definition(self) -> ToolDefinition:
        """Convert this tool to an LLM-facing ToolDefinition."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Manages available tools and dispatches executions."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool. Raises ValueError if name is already taken."""
        if tool.name in self._tools:
            msg = f"Tool '{tool.name}' already registered"
            raise ValueError(msg)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """Get a tool by name. Raises KeyError if not found."""
        if name not in self._tools:
            msg = f"Unknown tool: '{name}'"
            raise KeyError(msg)
        return self._tools[name]

    def list_definitions(self) -> list[ToolDefinition]:
        """Return LLM-facing definitions for all registered tools."""
        return [t.to_definition() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)
