"""apply_patch tool — atomic multi-file patch transactions with preconditions & rollback."""

from __future__ import annotations

import difflib
import hashlib
import json
import shlex
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from agent_forge.tools.base import Tool, ToolResult, validate_path

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox

_VALID_ACTIONS = frozenset({"modify", "create", "delete"})
_MAX_DIFF_PREVIEW_CHARS = 8000
_PREVIEW_CHARS = 400


@dataclass(frozen=True)
class _PatchFailure:
    """Structured patch validation failure returned to the caller."""

    file: str
    code: str
    message: str
    hunk_index: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _StagedChange:
    """Validated file change prepared in memory before commit."""

    path: str
    resolved: str
    action: str
    original: str | None
    updated: str | None
    sha256_before: str | None
    sha256_after: str | None
    status: str


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _failure_result(failure: _PatchFailure, started_at: float) -> ToolResult:
    payload = {"status": "failed", "failure": asdict(failure)}
    return ToolResult(
        output="",
        error=json.dumps(payload, indent=2),
        exit_code=1,
        execution_time_ms=_elapsed_ms(started_at),
    )


def _validate_raw_operation(
    op: object,
    index: int,
) -> tuple[str, str, str, dict[str, Any]] | _PatchFailure:
    if not isinstance(op, dict):
        return _PatchFailure(
            file=f"<operation:{index}>",
            code="invalid_operation",
            message="Each operation must be a JSON object.",
        )

    path = op.get("path", "")
    if not isinstance(path, str) or not path.strip():
        return _PatchFailure(
            file=f"<operation:{index}>",
            code="invalid_path",
            message="Missing required string field 'path'.",
        )

    try:
        resolved = validate_path(path)
    except ValueError as exc:
        return _PatchFailure(file=path, code="invalid_path", message=str(exc))

    action = op.get("action", "modify")
    if not isinstance(action, str) or action not in _VALID_ACTIONS:
        return _PatchFailure(
            file=path,
            code="invalid_action",
            message=f"Unsupported action '{action}'. Expected one of: modify, create, delete.",
        )

    return path, resolved, action, op


async def _read_current_file(sandbox: Sandbox, resolved: str) -> str | None:
    try:
        return await sandbox.read_file(resolved)
    except FileNotFoundError:
        return None


def _check_precondition(
    path: str,
    original: str | None,
    actual_sha256: str | None,
    expected_sha256: object,
    action: str,
) -> _PatchFailure | None:
    if expected_sha256 is None:
        return None
    if not isinstance(expected_sha256, str) or not expected_sha256:
        return _PatchFailure(
            file=path,
            code="invalid_precondition",
            message="'expected_sha256' must be a non-empty string when provided.",
        )
    if original is None and action in {"modify", "delete"}:
        return _PatchFailure(
            file=path,
            code="file_not_found",
            message=f"Cannot verify precondition because '{path}' does not exist.",
        )
    if actual_sha256 != expected_sha256:
        return _PatchFailure(
            file=path,
            code="precondition_mismatch",
            message=f"SHA-256 precondition mismatch for '{path}'.",
            details={"expected_sha256": expected_sha256, "actual_sha256": actual_sha256},
        )
    return None


def _apply_single_hunk(
    path: str,
    working: str,
    hunk: object,
    index: int,
) -> tuple[str, bool] | _PatchFailure:
    if not isinstance(hunk, dict):
        return _PatchFailure(
            file=path,
            code="invalid_hunk",
            message="Each hunk must be an object with 'old_text' and 'new_text'.",
            hunk_index=index,
        )

    old_text = hunk.get("old_text")
    new_text = hunk.get("new_text")
    if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
        return _PatchFailure(
            file=path,
            code="invalid_hunk",
            message="Hunk requires non-empty string 'old_text' and string 'new_text'.",
            hunk_index=index,
        )

    matches = working.count(old_text)
    if matches == 1:
        return working.replace(old_text, new_text, 1), False
    if matches > 1:
        return _PatchFailure(
            file=path,
            code="hunk_ambiguous",
            message=f"Hunk matched {matches} locations; include more surrounding context.",
            hunk_index=index,
            details={"matches": matches},
        )
    if new_text and old_text != new_text and new_text in working:
        return working, True

    return _PatchFailure(
        file=path,
        code="hunk_not_found",
        message=f"Hunk text not found in '{path}'.",
        hunk_index=index,
        details={"file_preview": working[:_PREVIEW_CHARS]},
    )


def _apply_hunks(path: str, original: str, hunks: object) -> tuple[str, str] | _PatchFailure:
    if not isinstance(hunks, list) or not hunks:
        return _PatchFailure(
            file=path,
            code="invalid_hunks",
            message="Action 'modify' requires a non-empty 'hunks' list.",
        )

    working = original
    saw_already_applied = False
    for idx, hunk in enumerate(hunks):
        outcome = _apply_single_hunk(path, working, hunk, idx)
        if isinstance(outcome, _PatchFailure):
            return outcome
        working, already_applied = outcome
        saw_already_applied = saw_already_applied or already_applied

    if working == original and saw_already_applied:
        return working, "unchanged"
    return working, ("unchanged" if working == original else "applied")


def _stage_modify(
    path: str,
    resolved: str,
    original: str | None,
    actual_sha256: str | None,
    op: dict[str, Any],
) -> _StagedChange | _PatchFailure:
    if original is None:
        return _PatchFailure(file=path, code="file_not_found", message=f"File not found: '{path}'.")

    applied = _apply_hunks(path, original, op.get("hunks"))
    if isinstance(applied, _PatchFailure):
        return applied

    updated, status = applied
    return _StagedChange(
        path=path,
        resolved=resolved,
        action="modify",
        original=original,
        updated=updated,
        sha256_before=actual_sha256,
        sha256_after=_sha256(updated),
        status=status,
    )


def _stage_create(
    path: str,
    resolved: str,
    original: str | None,
    actual_sha256: str | None,
    op: dict[str, Any],
) -> _StagedChange | _PatchFailure:
    content = op.get("content")
    if not isinstance(content, str):
        return _PatchFailure(
            file=path,
            code="invalid_content",
            message="Action 'create' requires string field 'content'.",
        )
    if original is not None:
        if original == content:
            return _StagedChange(
                path=path,
                resolved=resolved,
                action="create",
                original=original,
                updated=content,
                sha256_before=actual_sha256,
                sha256_after=actual_sha256,
                status="unchanged",
            )
        return _PatchFailure(
            file=path,
            code="file_exists_conflict",
            message=f"File '{path}' already exists with different content.",
            details={"actual_sha256": actual_sha256},
        )
    return _StagedChange(
        path=path,
        resolved=resolved,
        action="create",
        original=None,
        updated=content,
        sha256_before=None,
        sha256_after=_sha256(content),
        status="applied",
    )


def _stage_delete(
    path: str,
    resolved: str,
    original: str | None,
    actual_sha256: str | None,
) -> _StagedChange:
    if original is None:
        return _StagedChange(
            path=path,
            resolved=resolved,
            action="delete",
            original=None,
            updated=None,
            sha256_before=None,
            sha256_after=None,
            status="unchanged",
        )
    return _StagedChange(
        path=path,
        resolved=resolved,
        action="delete",
        original=original,
        updated=None,
        sha256_before=actual_sha256,
        sha256_after=None,
        status="applied",
    )


async def _stage_operation(
    sandbox: Sandbox,
    raw_op: object,
    index: int,
    seen: set[str],
) -> _StagedChange | _PatchFailure:
    parsed = _validate_raw_operation(raw_op, index)
    if isinstance(parsed, _PatchFailure):
        return parsed

    path, resolved, action, op = parsed
    if resolved in seen:
        return _PatchFailure(
            file=path,
            code="duplicate_path",
            message=f"Duplicate path '{path}' in the same patch transaction.",
        )
    seen.add(resolved)

    original = await _read_current_file(sandbox, resolved)
    actual_sha256 = _sha256(original) if original is not None else None
    precond_err = _check_precondition(
        path, original, actual_sha256, op.get("expected_sha256"), action
    )
    if precond_err is not None:
        return precond_err

    if action == "modify":
        return _stage_modify(path, resolved, original, actual_sha256, op)
    if action == "create":
        return _stage_create(path, resolved, original, actual_sha256, op)
    return _stage_delete(path, resolved, original, actual_sha256)


def _build_diff_preview(changes: list[_StagedChange]) -> str:
    diffs: list[str] = []
    for change in changes:
        if change.status != "applied":
            continue
        before = (change.original or "").splitlines(keepends=True)
        after = (change.updated or "").splitlines(keepends=True)
        fromfile = "/dev/null" if change.original is None else f"a/{change.path}"
        tofile = "/dev/null" if change.updated is None else f"b/{change.path}"
        chunk = "".join(difflib.unified_diff(before, after, fromfile=fromfile, tofile=tofile))
        if chunk:
            diffs.append(chunk)

    combined = "\n".join(diffs)
    if len(combined) > _MAX_DIFF_PREVIEW_CHARS:
        return combined[:_MAX_DIFF_PREVIEW_CHARS] + "\n... [truncated]"
    return combined


def _build_manifest(changes: list[_StagedChange]) -> list[dict[str, Any]]:
    return [
        {
            "path": c.path,
            "action": c.action,
            "status": c.status,
            "sha256_before": c.sha256_before,
            "sha256_after": c.sha256_after,
        }
        for c in changes
    ]


async def _commit_change(sandbox: Sandbox, change: _StagedChange) -> None:
    if change.action in {"modify", "create"}:
        await sandbox.write_file(change.resolved, change.updated or "")
        return
    res = await sandbox.exec(f"rm -f {shlex.quote(change.resolved)}")
    if res.exit_code != 0:
        msg = res.stderr or f"Failed to delete {change.resolved}"
        raise OSError(msg)


async def _rollback_changes(sandbox: Sandbox, committed: list[_StagedChange]) -> list[str]:
    rolled_back: list[str] = []
    for change in reversed(committed):
        if change.original is not None:
            await sandbox.write_file(change.resolved, change.original)
        else:
            await sandbox.exec(f"rm -f {shlex.quote(change.resolved)}")
        rolled_back.append(change.path)
    return rolled_back


class ApplyPatchTool(Tool):
    """Apply an atomic multi-file patch transaction with preconditions and rollback."""

    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "Atomically apply multi-file patch operations (modify, create, delete) with "
            "optional SHA-256 preconditions, diff preview, and rollback."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "Ordered file operations validated and committed atomically.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Relative path within the workspace",
                            },
                            "action": {
                                "type": "string",
                                "enum": ["modify", "create", "delete"],
                                "description": "Operation type (defaults to 'modify')",
                            },
                            "expected_sha256": {
                                "type": "string",
                                "description": "Optional expected SHA-256 hex digest before edit",
                            },
                            "content": {
                                "type": "string",
                                "description": "Full file content when action is 'create'",
                            },
                            "hunks": {
                                "type": "array",
                                "description": "Ordered replacements when action is 'modify'",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "old_text": {"type": "string"},
                                        "new_text": {"type": "string"},
                                    },
                                    "required": ["old_text", "new_text"],
                                },
                            },
                        },
                        "required": ["path"],
                    },
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Validate and return diff preview without writing files",
                },
            },
            "required": ["operations"],
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Validate all operations, preview diffs, and commit atomically with rollback."""
        started_at = time.monotonic()
        operations = arguments.get("operations")
        dry_run = bool(arguments.get("dry_run", False))
        if not isinstance(operations, list) or not operations:
            return _failure_result(
                _PatchFailure(
                    file="<transaction>",
                    code="invalid_operations",
                    message="'operations' must be a non-empty list.",
                ),
                started_at,
            )

        staged: list[_StagedChange] = []
        seen: set[str] = set()
        for idx, raw_op in enumerate(operations):
            outcome = await _stage_operation(sandbox, raw_op, idx, seen)
            if isinstance(outcome, _PatchFailure):
                return _failure_result(outcome, started_at)
            staged.append(outcome)

        manifest = _build_manifest(staged)
        diff_preview = _build_diff_preview(staged)
        metadata: dict[str, Any] = {
            "changed_files": manifest,
            "diff_preview": diff_preview,
            "dry_run": dry_run,
        }

        if not dry_run:
            rollback_err = await self._commit_all(sandbox, staged, started_at)
            if rollback_err is not None:
                return rollback_err

        summary = {"status": "dry_run" if dry_run else "applied", "changed_files": manifest}
        output = json.dumps(summary, indent=2)
        if diff_preview:
            output = f"{output}\n\n{diff_preview}"
        return ToolResult(
            output=output,
            exit_code=0,
            execution_time_ms=_elapsed_ms(started_at),
            metadata=metadata,
        )

    async def _commit_all(
        self,
        sandbox: Sandbox,
        staged: list[_StagedChange],
        started_at: float,
    ) -> ToolResult | None:
        committed: list[_StagedChange] = []
        for change in staged:
            if change.status != "applied":
                continue
            try:
                await _commit_change(sandbox, change)
            except Exception as exc:  # noqa: BLE001 — roll back committed changes on any commit failure
                rolled_back = await _rollback_changes(sandbox, committed)
                payload = {
                    "status": "rolled_back",
                    "failed_path": change.path,
                    "reason": str(exc),
                    "rolled_back": rolled_back,
                }
                return ToolResult(
                    output="",
                    error=json.dumps(payload, indent=2),
                    exit_code=1,
                    execution_time_ms=_elapsed_ms(started_at),
                )
            committed.append(change)
        return None
