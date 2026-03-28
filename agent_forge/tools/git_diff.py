"""git_diff tool — inspect tracked changes in the git workspace."""

from __future__ import annotations

import shlex
import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult
from agent_forge.tools.git_common import quote_pathspec, resolve_git_path, validate_revision

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox


class GitDiffTool(Tool):
    """Show staged, unstaged, or committed diffs from the current repository."""

    @property
    def name(self) -> str:
        return "git_diff"

    @property
    def description(self) -> str:
        return "Show staged, unstaged, or committed git changes in the workspace"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional workspace-relative path to diff",
                },
                "staged": {
                    "type": "boolean",
                    "description": "Show staged changes instead of unstaged changes",
                    "default": False,
                },
                "base_ref": {
                    "type": "string",
                    "description": "Optional git ref to diff against, e.g. 'HEAD~1' or 'main'",
                },
            },
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Run git diff with argument validation."""
        staged = bool(arguments.get("staged", False))
        base_ref_arg = arguments.get("base_ref", "")
        base_ref = ""
        if base_ref_arg:
            try:
                base_ref = validate_revision(str(base_ref_arg), field_name="base_ref")
            except ValueError as exc:
                return ToolResult(output="", error=str(exc), exit_code=1)

        if staged and base_ref:
            return ToolResult(
                output="",
                error="Arguments 'staged' and 'base_ref' are mutually exclusive",
                exit_code=1,
            )

        pathspec = ""
        path_arg = arguments.get("path", "")
        if path_arg:
            try:
                pathspec = resolve_git_path(str(path_arg))
            except ValueError as exc:
                return ToolResult(output="", error=str(exc), exit_code=1)

        cmd_parts = ["git", "-C", "/workspace", "diff", "--no-ext-diff", "--"]
        if base_ref:
            cmd_parts = [
                "git",
                "-C",
                "/workspace",
                "diff",
                "--no-ext-diff",
                shlex.quote(base_ref),
                "--",
            ]
        elif staged:
            cmd_parts = ["git", "-C", "/workspace", "diff", "--cached", "--no-ext-diff", "--"]

        if pathspec:
            cmd_parts.append(quote_pathspec(pathspec))

        start = time.monotonic()
        result = await sandbox.exec(" ".join(cmd_parts), timeout_seconds=30)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if result.exit_code != 0:
            return ToolResult(
                output="",
                error=result.stderr or "git diff failed",
                exit_code=result.exit_code,
                execution_time_ms=elapsed_ms,
            )

        output = result.stdout or "No changes found."
        return ToolResult(output=output, exit_code=0, execution_time_ms=elapsed_ms)
