"""Lightweight post-edit compiler & syntax diagnostics inside the sandbox."""

from __future__ import annotations

import posixpath
import re
import shlex
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_forge.agent.models import AgentRun
    from agent_forge.sandbox.base import Sandbox
    from agent_forge.tools.base import ToolResult

_EDIT_TOOL_NAMES = frozenset({"write_file", "edit_file", "apply_patch"})
_SKIP_MARKER = "__AGENT_FORGE_DIAG_SKIP__"
_COL_DIAG_RE = re.compile(r"^([^\s:][^:]*):(\d+):(?:(\d+):)?\s*(.+)$")
_PY_FILE_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)')


@dataclass(frozen=True)
class DiagnosticItem:
    """Single parsed syntax, type, or compiler diagnostic record."""

    path: str
    line: int | None
    column: int | None
    level: Literal["error", "warning"]
    code: str
    message: str
    source: str


@dataclass(frozen=True)
class DiagnosticsReport:
    """Structured result from running post-edit checks inside a sandbox."""

    checked_files: tuple[str, ...]
    checker: str
    exit_code: int
    items: tuple[DiagnosticItem, ...]
    raw_excerpt: str = ""
    elapsed_ms: int = 0

    @property
    def has_errors(self) -> bool:
        """Return ``True`` when the checker exited non-zero or emitted any error item."""
        return self.exit_code != 0 or any(item.level == "error" for item in self.items)

    def format_for_tool_output(self, *, max_items: int = 12) -> str:
        """Render a compact text block suitable for appending to a tool result."""
        lines = [f"[POST-EDIT DIAGNOSTICS ({self.checker})]"]
        if self.items:
            for item in self.items[:max_items]:
                loc = f"{item.path}:{item.line}" if item.line is not None else item.path
                if item.column is not None and item.line is not None:
                    loc = f"{loc}:{item.column}"
                code_part = f" [{item.code}]" if item.code else ""
                lines.append(f"- {loc}{code_part}: {item.message}")
            if len(self.items) > max_items:
                lines.append(f"- ... (+{len(self.items) - max_items} more diagnostics)")
        elif self.raw_excerpt:
            lines.append(self.raw_excerpt)
        return "\n".join(lines)

    def to_metadata(self) -> dict[str, Any]:
        """Serialize compact diagnostics summary for ``ToolResult.metadata``."""
        return {
            "checker": self.checker,
            "exit_code": self.exit_code,
            "errors": sum(1 for item in self.items if item.level == "error"),
            "checked_files": list(self.checked_files),
            "elapsed_ms": self.elapsed_ms,
        }


def _to_sandbox_path(raw_path: str) -> str:
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        return ""
    if cleaned.startswith("/workspace/"):
        return posixpath.normpath(cleaned)
    return posixpath.normpath(posixpath.join("/workspace", cleaned.lstrip("./")))


def _to_rel_path(raw_path: str) -> str:
    cleaned = raw_path.strip().replace("\\", "/")
    if cleaned.startswith("/workspace/"):
        return cleaned[len("/workspace/") :].lstrip("/")
    return cleaned.lstrip("./")


def _extract_patch_paths(arguments: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    changed = meta.get("changed_files")
    if isinstance(changed, list):
        paths = [str(item) for item in changed if isinstance(item, str) and item.strip()]
        if paths:
            return paths
    ops = arguments.get("operations")
    if not isinstance(ops, list):
        return []
    return [
        str(op["path"])
        for op in ops
        if isinstance(op, dict)
        and op.get("action") != "delete"
        and isinstance(op.get("path"), str)
        and str(op["path"]).strip()
    ]


def extract_edited_paths(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
) -> list[str]:
    """Extract normalized sandbox paths modified by a successful edit tool call."""
    if tool_name not in _EDIT_TOOL_NAMES:
        return []
    meta = result.metadata if isinstance(result.metadata, dict) else {}
    if meta.get("dry_run"):
        return []

    if tool_name in {"write_file", "edit_file"}:
        path = arguments.get("path")
        raw_paths = [path] if isinstance(path, str) and path.strip() else []
    else:
        raw_paths = _extract_patch_paths(arguments, meta)

    deduped: list[str] = []
    for raw in raw_paths:
        sb_path = _to_sandbox_path(raw)
        if sb_path and sb_path not in deduped:
            deduped.append(sb_path)
    return deduped


def _primary_language_group(paths: Sequence[str]) -> str | None:
    ext_map = {
        ".py": "py",
        ".js": "js",
        ".mjs": "js",
        ".cjs": "js",
        ".ts": "ts",
        ".tsx": "ts",
        ".go": "go",
        ".rs": "rs",
        ".sh": "sh",
        ".bash": "sh",
        ".json": "json",
    }
    for path in paths:
        suffix = posixpath.splitext(path.lower())[1]
        if suffix in ext_map:
            return ext_map[suffix]
    return None


def select_diagnostic_command(paths: Sequence[str]) -> tuple[str, str] | None:
    """Build ``(checker_label, shell_command)`` for *paths*, or ``None`` if unsupported."""
    sb_paths = [_to_sandbox_path(p) for p in paths if p.strip()]
    if not sb_paths:
        return None
    group = _primary_language_group(sb_paths)
    if group is None:
        return None

    if group == "py":
        py_files = [p for p in sb_paths if p.lower().endswith(".py")]
        q_files = " ".join(shlex.quote(p) for p in py_files)
        cmd = (
            f"python3 -m py_compile {q_files} && "
            f"if command -v ruff >/dev/null 2>&1; then "
            f"ruff check --output-format concise --select E9,F63,F7,F82 {q_files}; fi"
        )
        return "py_compile+ruff", cmd

    if group == "js":
        js_files = [
            p for p in sb_paths if posixpath.splitext(p.lower())[1] in {".js", ".mjs", ".cjs"}
        ]
        q_files = " ".join(shlex.quote(p) for p in js_files)
        cmd = (
            "if command -v node >/dev/null 2>&1; then "
            f'for f in {q_files}; do node --check "$f" || exit 1; done; '
            f"else echo {_SKIP_MARKER}; fi"
        )
        return "node", cmd

    if group == "ts":
        cmd = (
            "if command -v tsc >/dev/null 2>&1; then "
            "tsc --noEmit --pretty false; "
            f"else echo {_SKIP_MARKER}; fi"
        )
        return "tsc", cmd

    if group == "go":
        cmd = f"if command -v go >/dev/null 2>&1; then go vet ./...; else echo {_SKIP_MARKER}; fi"
        return "go-vet", cmd

    if group == "rs":
        cmd = (
            "if command -v cargo >/dev/null 2>&1; then "
            "cargo check --message-format short; "
            f"else echo {_SKIP_MARKER}; fi"
        )
        return "cargo-check", cmd

    if group == "sh":
        sh_files = [p for p in sb_paths if posixpath.splitext(p.lower())[1] in {".sh", ".bash"}]
        q_files = " ".join(shlex.quote(p) for p in sh_files)
        return "sh-n", f'for f in {q_files}; do sh -n "$f" || exit 1; done'

    json_files = [p for p in sb_paths if p.lower().endswith(".json")]
    q_files = " ".join(shlex.quote(p) for p in json_files)
    return (
        "json.tool",
        f'for f in {q_files}; do python3 -m json.tool "$f" >/dev/null || exit 1; done',
    )


def _parse_line_diag(
    line: str,
    *,
    checker: str,
    last_py_loc: tuple[str, int] | None,
) -> DiagnosticItem | None:
    m_col = _COL_DIAG_RE.match(line)
    if m_col is not None and not line.startswith(("File ", "Traceback")):
        path = _to_rel_path(m_col.group(1))
        line_no = int(m_col.group(2))
        col_no = int(m_col.group(3)) if m_col.group(3) else None
        msg = m_col.group(4).strip()
        code_m = re.match(r"^([A-Z]\d{3,4})\s+(.+)$", msg)
        code = code_m.group(1) if code_m else ""
        text = code_m.group(2) if code_m else msg
        return DiagnosticItem(
            path=path,
            line=line_no,
            column=col_no,
            level="error",
            code=code,
            message=text,
            source=checker,
        )
    if last_py_loc is not None and ("SyntaxError:" in line or "IndentationError:" in line):
        return DiagnosticItem(
            path=last_py_loc[0],
            line=last_py_loc[1],
            column=None,
            level="error",
            code="SyntaxError",
            message=line.strip(),
            source=checker,
        )
    return None


def parse_diagnostic_output(
    checker: str,
    output: str,
    *,
    default_path: str = "",
    allow_fallback: bool = True,
) -> list[DiagnosticItem]:
    """Parse compiler/syntax output into structured :class:`DiagnosticItem` objects."""
    items: list[DiagnosticItem] = []
    last_py_loc: tuple[str, int] | None = None
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        py_m = _PY_FILE_LINE_RE.search(stripped)
        if py_m is not None:
            last_py_loc = (_to_rel_path(py_m.group(1)), int(py_m.group(2)))
            continue
        parsed = _parse_line_diag(stripped, checker=checker, last_py_loc=last_py_loc)
        if parsed is not None:
            items.append(parsed)
            last_py_loc = None

    if allow_fallback and not items and output.strip():
        first = output.strip().splitlines()[0].strip()
        items.append(
            DiagnosticItem(
                path=_to_rel_path(default_path) if default_path else "<workspace>",
                line=None,
                column=None,
                level="error",
                code="",
                message=first[:240],
                source=checker,
            )
        )
    return items


async def run_post_edit_diagnostics(
    paths: Sequence[str],
    sandbox: Sandbox,
    *,
    timeout_seconds: int = 15,
) -> DiagnosticsReport | None:
    """Run a lightweight checker over *paths* inside *sandbox* cleanly."""
    selected = select_diagnostic_command(paths)
    if selected is None:
        return None
    checker, cmd = selected
    rel_paths = tuple(_to_rel_path(p) for p in paths if _to_rel_path(p))

    started = time.monotonic()
    exec_res = await sandbox.exec(cmd, timeout_seconds=timeout_seconds)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    combined = "\n".join(
        part for part in (exec_res.stdout, exec_res.stderr) if part.strip()
    ).strip()
    if _SKIP_MARKER in exec_res.stdout:
        return None

    items: tuple[DiagnosticItem, ...] = ()
    if exec_res.exit_code != 0 or combined:
        default_path = rel_paths[0] if rel_paths else ""
        parsed = parse_diagnostic_output(
            checker,
            combined,
            default_path=default_path,
            allow_fallback=exec_res.exit_code != 0,
        )
        items = tuple(parsed) if (exec_res.exit_code != 0 or parsed) else ()

    excerpt = combined[:600] if combined else ""
    return DiagnosticsReport(
        checked_files=rel_paths,
        checker=checker,
        exit_code=exec_res.exit_code,
        items=items,
        raw_excerpt=excerpt,
        elapsed_ms=elapsed_ms,
    )


async def maybe_attach_post_edit_diagnostics(
    run: AgentRun,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    sandbox: Sandbox,
) -> ToolResult:
    """Run post-edit diagnostics when enabled and append compact feedback to *result*."""
    if not getattr(run.config, "post_edit_diagnostics", False) or result.exit_code != 0:
        return result
    paths = extract_edited_paths(tool_name, arguments, result)
    if not paths:
        return result

    report = await run_post_edit_diagnostics(paths, sandbox)
    if report is None:
        return result

    meta = dict(result.metadata)
    meta["diagnostics"] = report.to_metadata()
    if not report.has_errors:
        return replace(result, metadata=meta)

    rendered = report.format_for_tool_output()
    updated_output = (
        f"{result.output.rstrip()}\n\n{rendered}".strip() if result.output else rendered
    )
    return replace(result, output=updated_output, metadata=meta)
