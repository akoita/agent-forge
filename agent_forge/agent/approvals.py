"""Human-in-the-loop approval gates for destructive tool operations."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

VALID_APPROVAL_MODES = frozenset({"auto", "prompt", "deny"})
_APPROVAL_STRICTNESS: dict[str, int] = {"auto": 0, "prompt": 1, "deny": 2}
_MUTATING_MCP_NAME_RE = re.compile(
    r"(?:^|_)(?:write|edit|delete|remove|create|update|put|post|patch|"
    r"push|merge|exec|run|shell|drop|destroy|send|close|archive|move|mkdir)(?:_|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ApprovalRequest:
    """Description of a destructive tool call awaiting an approval decision."""

    tool_name: str
    arguments: dict[str, Any]
    category: str
    reason: str


@dataclass(frozen=True)
class ApprovalDecision:
    """Result of evaluating an approval gate for a destructive operation."""

    approved: bool
    decided_by: str
    reason: str = ""


ApprovalCallback = Callable[[ApprovalRequest], Awaitable[bool]]

_SHELL_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("file_deletion", re.compile(r"\brm\s+"), "Shell command deletes files via 'rm'."),
    (
        "git_force_push",
        re.compile(r"\bgit\s+push\b.*(?:--force(?:-with-lease)?|\s-f(?:\s|$))"),
        "Shell command performs a force push.",
    ),
    (
        "git_reset_hard",
        re.compile(r"\bgit\s+reset\s+--hard\b"),
        "Shell command runs 'git reset --hard'.",
    ),
    (
        "git_clean",
        re.compile(r"\bgit\s+clean\b.*-[a-zA-Z]*f"),
        "Shell command removes untracked files via 'git clean'.",
    ),
    (
        "git_branch_delete",
        re.compile(r"\bgit\s+branch\s+-D\b"),
        "Shell command force-deletes a git branch via 'git branch -D'.",
    ),
    (
        "git_discard_changes",
        re.compile(r"\bgit\s+(?:checkout\s+--\s|restore\b)"),
        "Shell command discards working-tree changes.",
    ),
)


def _classify_apply_patch(arguments: dict[str, Any]) -> ApprovalRequest | None:
    operations = arguments.get("operations")
    if not isinstance(operations, list):
        return None
    deleted_paths = [
        str(op.get("path", "<unknown>"))
        for op in operations
        if isinstance(op, dict) and op.get("action") == "delete"
    ]
    if not deleted_paths:
        return None
    return ApprovalRequest(
        tool_name="apply_patch",
        arguments=arguments,
        category="file_deletion",
        reason=f"Patch deletes files: {', '.join(deleted_paths)}",
    )


def _classify_shell_command(tool_name: str, arguments: dict[str, Any]) -> ApprovalRequest | None:
    command = str(arguments.get("command", ""))
    for category, pattern, reason in _SHELL_PATTERNS:
        if pattern.search(command):
            return ApprovalRequest(
                tool_name=tool_name,
                arguments=arguments,
                category=category,
                reason=reason,
            )
    return None


def _classify_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
    tool: Any,
) -> ApprovalRequest | None:
    if getattr(tool, "is_mcp_tool", False) is not True:
        return None
    annotations = getattr(tool, "annotations", None)
    allow_ro = getattr(tool, "allow_read_only_without_approval", False) is True
    read_only_hint = getattr(annotations, "read_only", False) is True
    destructive_hint = getattr(annotations, "destructive", None)
    if read_only_hint and destructive_hint is not True and allow_ro:
        return None
    remote_name = str(getattr(tool, "remote_name", tool_name))
    server_name = str(getattr(tool, "server_name", "external"))
    is_destructive = destructive_hint is True or bool(_MUTATING_MCP_NAME_RE.search(remote_name))
    category = "mcp_destructive" if is_destructive else "mcp_external"
    reason = f"External MCP tool '{remote_name}' on server '{server_name}'."
    return ApprovalRequest(
        tool_name=tool_name,
        arguments=arguments,
        category=category,
        reason=reason,
    )


def classify_destructive_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    tool: Any = None,
) -> ApprovalRequest | None:
    """Return an ``ApprovalRequest`` if a tool call is destructive or gated, else ``None``."""
    if tool is not None:
        mcp_req = _classify_mcp_tool(tool_name, arguments, tool)
        if mcp_req is not None:
            return mcp_req
    if tool_name == "apply_patch":
        return _classify_apply_patch(arguments)
    if tool_name in ("run_shell", "shell_start"):
        return _classify_shell_command(tool_name, arguments)
    return None


def _resolve_effective_mode(global_mode: str, tool: Any) -> str:
    if tool is None:
        return global_mode
    tool_mode = getattr(tool, "approval_mode", "inherit")
    if not isinstance(tool_mode, str):
        return global_mode
    normalized = tool_mode.strip().lower()
    if normalized not in _APPROVAL_STRICTNESS:
        return global_mode
    if _APPROVAL_STRICTNESS[normalized] > _APPROVAL_STRICTNESS[global_mode]:
        return normalized
    return global_mode


class ApprovalGate:
    """Evaluate destructive tool calls under ``auto``, ``prompt``, or ``deny`` policy."""

    def __init__(
        self,
        mode: str = "auto",
        callback: ApprovalCallback | None = None,
    ) -> None:
        normalized = mode.strip().lower()
        if normalized not in VALID_APPROVAL_MODES:
            allowed = ", ".join(sorted(VALID_APPROVAL_MODES))
            msg = f"Invalid approval mode '{mode}'. Expected one of: {allowed}"
            raise ValueError(msg)
        self._mode = normalized
        self._callback = callback

    @property
    def mode(self) -> str:
        """Configured approval mode."""
        return self._mode

    async def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool: Any = None,
    ) -> tuple[ApprovalRequest | None, ApprovalDecision | None]:
        """Evaluate whether *tool_name* with *arguments* may execute."""
        request = classify_destructive_tool_call(tool_name, arguments, tool=tool)
        if request is None:
            return None, None

        effective_mode = _resolve_effective_mode(self._mode, tool)
        if effective_mode == "auto":
            return request, ApprovalDecision(approved=True, decided_by="policy:auto")
        if effective_mode == "deny":
            return request, ApprovalDecision(
                approved=False,
                decided_by="policy:deny",
                reason="Operation denied by policy.",
            )

        if self._callback is None:
            return request, ApprovalDecision(
                approved=False,
                decided_by="policy:no_approver",
                reason="No interactive approver available.",
            )

        approved = bool(await self._callback(request))
        return request, ApprovalDecision(
            approved=approved,
            decided_by="human",
            reason="" if approved else "Denied by operator.",
        )
