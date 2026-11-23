"""Hierarchical repository instruction discovery with symlink deduplication."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

INSTRUCTION_FILENAMES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".agent-forge/instructions.md",
)

_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
    }
)

_TRUNCATION_MARKER = "\n[... truncated to fit instruction budget ...]"
_WORKSPACE_PREFIX = "/workspace"


@dataclass(frozen=True)
class InstructionFile:
    """Single discovered repository instruction file."""

    path: Path
    rel_path: str
    scope: Literal["parent", "root", "child"]
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class DiscoveredInstructions:
    """Ordered set of discovered instruction files and rendered prompt block."""

    files: tuple[InstructionFile, ...]
    rendered: str
    total_tokens: int
    truncated: bool


def _estimate_tokens(text: str) -> int:
    """Estimate token count cleanly with a 4-chars-per-token heuristic."""
    return (len(text) + 3) // 4 if text else 0


def find_repo_boundary(repo_path: Path) -> Path:
    """Return the enclosing Git repository root, or *repo_path* if none exists."""
    resolved = repo_path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved


def _normalize_candidate_path(raw_path: str | Path, repo_root: Path) -> Path | None:
    """Normalize a workspace or relative path onto *repo_root*, staying within bounds."""
    text = str(raw_path).strip().replace("\\", "/")
    if not text:
        return None
    if text == _WORKSPACE_PREFIX:
        candidate = repo_root
    elif text.startswith(_WORKSPACE_PREFIX + "/"):
        rel = text[len(_WORKSPACE_PREFIX) + 1 :].lstrip("/")
        candidate = repo_root / rel
    elif Path(text).is_absolute():
        candidate = Path(text)
    else:
        rel = text.lstrip("./")
        candidate = repo_root / rel if rel else repo_root

    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(repo_root):
        return None
    return resolved


def extract_mentioned_paths(task: str, repo_root: Path) -> list[Path]:
    """Return existing paths inside *repo_root* explicitly referenced in *task*."""
    if not task:
        return []

    found: list[Path] = []
    seen: set[Path] = set()
    for token in re.findall(r"[A-Za-z0-9_./\\-]+", task):
        cleaned = token.strip(".,:;!?\"'()[]{}")
        if not cleaned or ("/" not in cleaned and "." not in cleaned):
            continue
        candidate = _normalize_candidate_path(cleaned, repo_root)
        if candidate is None or candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        found.append(candidate)
    return found


def _collect_parent_dirs(
    repo_root: Path,
) -> list[tuple[Path, Literal["parent", "root", "child"]]]:
    boundary = find_repo_boundary(repo_root)
    if boundary == repo_root or not repo_root.is_relative_to(boundary):
        return []
    rel = repo_root.relative_to(boundary)
    chain: list[tuple[Path, Literal["parent", "root", "child"]]] = [(boundary, "parent")]
    cur = boundary
    for part in rel.parts[:-1]:
        cur = cur / part
        chain.append((cur, "parent"))
    return chain


def _collect_child_dirs(
    repo_root: Path,
    target_paths: Sequence[str | Path],
    task: str,
) -> list[tuple[Path, Literal["parent", "root", "child"]]]:
    targets: list[Path] = []
    for raw in target_paths:
        norm = _normalize_candidate_path(raw, repo_root)
        if norm is not None:
            targets.append(norm)
    targets.extend(extract_mentioned_paths(task, repo_root))

    child_rels: set[Path] = set()
    for target in targets:
        target_dir = target if target.is_dir() else target.parent
        if target_dir == repo_root or not target_dir.is_relative_to(repo_root):
            continue
        rel = target_dir.relative_to(repo_root)
        if any(part in _IGNORED_DIRS for part in rel.parts):
            continue
        prefix = Path()
        for part in rel.parts:
            prefix = prefix / part
            child_rels.add(prefix)

    ordered = sorted(child_rels, key=lambda p: (len(p.parts), p.as_posix()))
    return [(repo_root / rel, "child") for rel in ordered]


def _build_directory_chain(
    repo_root: Path,
    target_paths: Sequence[str | Path],
    task: str,
) -> list[tuple[Path, Literal["parent", "root", "child"]]]:
    return [
        *_collect_parent_dirs(repo_root),
        (repo_root, "root"),
        *_collect_child_dirs(repo_root, target_paths, task),
    ]


def _fit_content_to_budget(content: str, remaining_tokens: int) -> tuple[str, bool] | None:
    needed = _estimate_tokens(content)
    if needed <= remaining_tokens:
        return content, False
    if remaining_tokens < 32:
        return None
    marker_tokens = _estimate_tokens(_TRUNCATION_MARKER)
    char_budget = max(0, (remaining_tokens - marker_tokens) * 4)
    snippet = content[:char_budget].rstrip()
    if not snippet:
        return None
    return snippet + _TRUNCATION_MARKER, True


def _load_candidate(
    candidate: Path,
    *,
    repo_root: Path,
    scope: Literal["parent", "root", "child"],
    seen_resolved: set[Path],
    remaining_tokens: int,
) -> tuple[InstructionFile | None, bool]:
    if not candidate.is_file():
        return None, False
    try:
        resolved = candidate.resolve()
    except OSError:
        return None, False
    if resolved in seen_resolved:
        return None, False
    try:
        raw_text = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        return None, False
    if not raw_text:
        return None, False

    fitted = _fit_content_to_budget(raw_text, remaining_tokens)
    if fitted is None:
        return None, True
    text, truncated = fitted
    seen_resolved.add(resolved)
    rel_path = Path(os.path.relpath(candidate, repo_root)).as_posix()
    return (
        InstructionFile(
            path=candidate,
            rel_path=rel_path,
            scope=scope,
            content=text,
            truncated=truncated,
        ),
        truncated,
    )


def _render_instructions(files: Sequence[InstructionFile]) -> str:
    if not files:
        return ""
    sections = [
        "## Repository Instructions",
        "More specific directory instructions override broader instructions.",
    ]
    for item in files:
        sections.append(f"### Instructions: {item.rel_path} ({item.scope})\n{item.content}")
    return "\n\n".join(sections)


def discover_repo_instructions(
    repo_path: Path | str,
    *,
    target_paths: Sequence[str | Path] = (),
    task: str = "",
    max_tokens: int = 4_000,
    exclude_resolved: set[Path] | None = None,
) -> DiscoveredInstructions:
    """Discover instruction files across parent, root, and target child directories."""
    base = Path(repo_path)
    if not base.is_dir() or max_tokens <= 0:
        return DiscoveredInstructions(files=(), rendered="", total_tokens=0, truncated=False)

    repo_root = base.resolve()
    seen_resolved: set[Path] = set(exclude_resolved or ())
    collected: list[InstructionFile] = []
    remaining_tokens = max_tokens
    any_truncated = False

    for directory, scope in _build_directory_chain(repo_root, target_paths, task):
        for filename in INSTRUCTION_FILENAMES:
            item, item_truncated = _load_candidate(
                directory / filename,
                repo_root=repo_root,
                scope=scope,
                seen_resolved=seen_resolved,
                remaining_tokens=remaining_tokens,
            )
            any_truncated = any_truncated or item_truncated
            if item is not None:
                collected.append(item)
                remaining_tokens = max(0, remaining_tokens - _estimate_tokens(item.content))
            if remaining_tokens == 0 and any_truncated:
                break
        if remaining_tokens == 0 and any_truncated:
            break

    rendered = _render_instructions(collected)
    return DiscoveredInstructions(
        files=tuple(collected),
        rendered=rendered,
        total_tokens=_estimate_tokens(rendered),
        truncated=any_truncated,
    )


def discover_scoped_instructions_for_path(
    repo_path: Path | str,
    target_path: str | Path,
    *,
    already_loaded: set[Path],
    max_tokens: int = 1_500,
) -> DiscoveredInstructions:
    """Discover unseen child-directory instructions along *target_path* for mid-run injection."""
    return discover_repo_instructions(
        repo_path,
        target_paths=(target_path,),
        max_tokens=max_tokens,
        exclude_resolved=already_loaded,
    )
