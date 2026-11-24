"""search_codebase tool — ripgrep with POSIX grep and host-side fallbacks."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from agent_forge.sandbox.base import ExecResult, Sandbox


def _is_rg_missing(result: ExecResult) -> bool:
    if result.exit_code == 127:
        return True
    if result.exit_code == 0:
        return False
    lower = result.stderr.lower()
    return "rg" in lower and ("not found" in lower or "no such file" in lower)


def _matches_glob(rel_posix: str, filename: str, file_glob: str) -> bool:
    if not file_glob:
        return True
    return fnmatch.fnmatch(rel_posix, file_glob) or fnmatch.fnmatch(filename, file_glob)


def _search_host_workspace(
    host_root: Path,
    pattern: str,
    file_glob: str,
    max_results: int,
) -> tuple[int, list[dict[str, Any]], str]:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return 2, [], f"Invalid regex pattern '{pattern}': {exc}"

    matches: list[dict[str, Any]] = []
    limit = max(1, int(max_results))
    for dirpath, dirnames, filenames in os.walk(host_root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for fname in sorted(filenames):
            file_path = Path(dirpath) / fname
            rel_posix = file_path.relative_to(host_root).as_posix()
            if not _matches_glob(rel_posix, fname, file_glob):
                continue
            try:
                raw = file_path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw:
                continue
            text = raw.decode("utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "file": f"/workspace/{rel_posix}",
                            "line": line_no,
                            "content": line.rstrip(),
                        }
                    )
                    if len(matches) >= limit:
                        return 0, matches, ""
    return 0, matches, ""


async def _search_with_grep(
    sandbox: Sandbox,
    pattern: str,
    file_glob: str,
    max_results: int,
) -> tuple[int, list[dict[str, Any]], str]:
    parts = ["grep", "-rnE", "--exclude-dir=.git"]
    if file_glob:
        parts.append(f"--include={shlex.quote(file_glob)}")
    parts.extend([shlex.quote(pattern), "/workspace"])
    result = await sandbox.exec(" ".join(parts), timeout_seconds=30)
    if result.exit_code == 1:
        return 0, [], ""
    if result.exit_code != 0:
        return (
            result.exit_code,
            [],
            result.stderr or f"grep failed with exit code {result.exit_code}",
        )

    matches: list[dict[str, Any]] = []
    limit = max(1, int(max_results))
    for raw_line in result.stdout.splitlines():
        split_parts = raw_line.split(":", 2)
        if len(split_parts) != 3 or not split_parts[1].isdigit():
            continue
        matches.append(
            {
                "file": split_parts[0],
                "line": int(split_parts[1]),
                "content": split_parts[2].rstrip(),
            }
        )
        if len(matches) >= limit:
            break
    return 0, matches, ""


def _parse_rg_json_matches(stdout: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for line in stdout.strip().splitlines():
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
    return matches


class SearchCodebaseTool(Tool):
    """Search for a pattern across files in the workspace using ripgrep or fallbacks."""

    @property
    def name(self) -> str:
        return "search_codebase"

    @property
    def side_effecting(self) -> bool:
        return False

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
        """Search for a pattern using ``rg --json``, host search, or POSIX ``grep``."""
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

        if _is_rg_missing(result):
            host_ws = getattr(sandbox, "host_workspace_path", None)
            if isinstance(host_ws, str) and Path(host_ws).is_dir():
                code, matches, err = _search_host_workspace(
                    Path(host_ws), pattern, file_glob, max_results
                )
            else:
                code, matches, err = await _search_with_grep(
                    sandbox, pattern, file_glob, max_results
                )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if code != 0:
                return ToolResult(
                    output="", error=err, exit_code=code, execution_time_ms=elapsed_ms
                )
            output = json.dumps(matches, indent=2) if matches else "No matches found."
            return ToolResult(output=output, exit_code=0, execution_time_ms=elapsed_ms)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if result.exit_code == 1:
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

        matches = _parse_rg_json_matches(result.stdout)
        return ToolResult(
            output=json.dumps(matches, indent=2),
            exit_code=0,
            execution_time_ms=elapsed_ms,
        )
