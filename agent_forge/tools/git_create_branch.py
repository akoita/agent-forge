"""git_create_branch tool — create and check out a new git branch."""

from __future__ import annotations

import shlex
import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult
from agent_forge.tools.git_common import validate_ref_name

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox


class GitCreateBranchTool(Tool):
    """Create and check out a branch from an optional base ref."""

    @property
    def name(self) -> str:
        return "git_create_branch"

    @property
    def description(self) -> str:
        return "Create and check out a new git branch in the workspace repository"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "branch_name": {
                    "type": "string",
                    "description": "New branch name to create and check out",
                },
                "base_ref": {
                    "type": "string",
                    "description": "Optional base ref to branch from (defaults to HEAD)",
                },
            },
            "required": ["branch_name"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Create the branch after validating ref names."""
        try:
            branch_name = validate_ref_name(
                str(arguments.get("branch_name", "")),
                field_name="branch_name",
            )
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        base_ref = ""
        base_ref_arg = arguments.get("base_ref", "")
        if base_ref_arg:
            try:
                base_ref = validate_ref_name(str(base_ref_arg), field_name="base_ref")
            except ValueError as exc:
                return ToolResult(output="", error=str(exc), exit_code=1)

        cmd = f"git -C /workspace checkout -b {shlex.quote(branch_name)}"
        if base_ref:
            cmd += f" {shlex.quote(base_ref)}"

        start = time.monotonic()
        result = await sandbox.exec(cmd, timeout_seconds=30)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if result.exit_code != 0:
            return ToolResult(
                output=result.stdout,
                error=result.stderr or "git checkout -b failed",
                exit_code=result.exit_code,
                execution_time_ms=elapsed_ms,
            )

        return ToolResult(
            output=result.stdout or f"Switched to a new branch '{branch_name}'",
            exit_code=0,
            execution_time_ms=elapsed_ms,
        )
