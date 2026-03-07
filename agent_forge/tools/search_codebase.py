"""search_codebase tool — ripgrep wrapper for searching files in the sandbox."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox


class SearchCodebaseTool(Tool):
    """Search for a pattern across files in the workspace using ripgrep."""

    @property
    def name(self) -> str:
        return "search_codebase"

    @property
    def description(self) -> str:
        return "Search for a pattern across files in the workspace using ripgrep"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Search pattern (regex)",
                },
                "file_glob": {
                    "type": "string",
                    "description": "File glob filter (e.g. '*.py')",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 20)",
                    "default": 20,
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Search for a pattern using rg --json."""
        pattern = arguments.get("pattern", "")
        file_glob = arguments.get("file_glob", "")
        max_results = arguments.get("max_results", 20)

        if not pattern:
            return ToolResult(output="", error="Missing required argument: pattern", exit_code=1)

        start = time.monotonic()

        cmd = f"rg --json --max-count {max_results} {pattern!r}"
        if file_glob:
            cmd += f" --glob {file_glob!r}"
        cmd += " /workspace"

        result = await sandbox.exec(cmd, timeout_seconds=30)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if result.exit_code == 1:
            # rg exit code 1 = no matches
            return ToolResult(
                output="No matches found.",
                exit_code=0,
                execution_time_ms=elapsed_ms,
            )

        if result.exit_code != 0:
            return ToolResult(
                output="",
                error=result.stderr or f"ripgrep failed with exit code {result.exit_code}",
                exit_code=result.exit_code,
                execution_time_ms=elapsed_ms,
            )

        # Parse JSON lines from rg output
        matches: list[dict[str, Any]] = []
        for line in result.stdout.strip().splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "match":
                match_data = data.get("data", {})
                path_data = match_data.get("path", {})
                matches.append(
                    {
                        "file": path_data.get("text", ""),
                        "line": match_data.get("line_number", 0),
                        "content": match_data.get("lines", {}).get("text", "").rstrip(),
                    }
                )

        output = json.dumps(matches, indent=2)
        return ToolResult(
            output=output,
            exit_code=0,
            execution_time_ms=elapsed_ms,
        )
