"""Deterministic, file-backed persistent repository memory (``.agent-forge/memory.md``)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agent_forge.agent.instructions import find_repo_boundary

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_MEMORY_PATH = ".agent-forge/memory.md"

_CATEGORY_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")
_STRUCTURED_LINE_RE = re.compile(
    r"^-\s+\[(mem-[0-9a-f]{8})\]\s+\(([^)]*)\)\s+\[category=([a-z0-9_-]+)\]\s+(.+)$"
)
_TRUNCATION_NOTE = "- [... additional memory entries truncated to fit budget ...]"


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4 if text else 0


@dataclass(frozen=True)
class MemoryEntry:
    """Single deterministic learning persisted across runs for a repository."""

    id: str
    category: str
    text: str
    created_at: str = ""


@dataclass(frozen=True)
class RenderedMemory:
    """Rendered repository memory block bounded for prompt-prefix injection."""

    path: Path
    entries: tuple[MemoryEntry, ...]
    rendered: str
    total_tokens: int
    truncated: bool


def _normalize_category(raw: str) -> str:
    cleaned = _CATEGORY_CLEAN_RE.sub("-", raw.strip().lower()).strip("-_")
    return cleaned or "general"


def _normalize_text(raw: str) -> str:
    collapsed = " ".join(raw.strip().split())
    if not collapsed:
        msg = "Memory entry text cannot be empty."
        raise ValueError(msg)
    return collapsed


def _compute_entry_id(category: str, text: str) -> str:
    digest = hashlib.sha256(f"{category}:{text.lower()}".encode()).hexdigest()
    return f"mem-{digest[:8]}"


def _resolve_memory_path(repo_path: Path, relative_path: str) -> Path:
    cleaned = relative_path.strip()
    if not cleaned:
        msg = "Memory path cannot be empty."
        raise ValueError(msg)
    if Path(cleaned).is_absolute():
        msg = f"Memory path must be repository-relative: '{relative_path}'"
        raise ValueError(msg)
    root = find_repo_boundary(repo_path).resolve()
    resolved = (root / cleaned).resolve()
    if not resolved.is_relative_to(root):
        msg = f"Memory path escapes repository root: '{relative_path}'"
        raise ValueError(msg)
    return resolved


def _parse_memory_line(raw_line: str) -> MemoryEntry | None:
    line = raw_line.strip()
    if not line:
        return None
    structured = _STRUCTURED_LINE_RE.fullmatch(line)
    if structured is not None:
        return MemoryEntry(
            id=structured.group(1),
            created_at=structured.group(2).strip(),
            category=_normalize_category(structured.group(3)),
            text=structured.group(4).strip(),
        )
    if line.startswith(("- ", "* ")):
        body = " ".join(line[2:].strip().split())
        if not body:
            return None
        return MemoryEntry(
            id=_compute_entry_id("general", body),
            category="general",
            text=body,
            created_at="",
        )
    return None


def _format_memory_markdown(entries: Sequence[MemoryEntry]) -> str:
    lines = ["# Repository Memory", ""]
    for entry in entries:
        date_str = entry.created_at or "undated"
        lines.append(f"- [{entry.id}] ({date_str}) [category={entry.category}] {entry.text}")
    return "\n".join(lines) + "\n"


def _format_prompt_bullet(entry: MemoryEntry) -> str:
    date_part = f" ({entry.created_at})" if entry.created_at else ""
    return f"- [{entry.id}]{date_part} [{entry.category}] {entry.text}"


class RepoMemoryStore:
    """Manage durable, human-editable Markdown repository memory."""

    def __init__(
        self,
        repo_path: str | Path,
        *,
        relative_path: str = DEFAULT_MEMORY_PATH,
    ) -> None:
        raw_root = Path(repo_path)
        self.repo_root = find_repo_boundary(raw_root).resolve()
        self.relative_path = relative_path.strip() or DEFAULT_MEMORY_PATH
        self.path = _resolve_memory_path(self.repo_root, self.relative_path)

    def load_entries(self) -> list[MemoryEntry]:
        """Load and deduplicate persisted entries from disk in file order."""
        if not self.path.is_file():
            return []
        entries: list[MemoryEntry] = []
        seen_ids: set[str] = set()
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_memory_line(raw_line)
            if parsed is None or parsed.id in seen_ids:
                continue
            seen_ids.add(parsed.id)
            entries.append(parsed)
        return entries

    def _write_entries(self, entries: Sequence[MemoryEntry]) -> None:
        if not entries:
            if self.path.exists():
                self.path.unlink()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(_format_memory_markdown(entries), encoding="utf-8")
        os.replace(tmp_path, self.path)

    def add(
        self,
        text: str,
        *,
        category: str = "general",
        created_at: str | None = None,
    ) -> tuple[MemoryEntry, bool]:
        """Persist *text* under *category* unless an identical entry already exists."""
        norm_cat = _normalize_category(category)
        norm_text = _normalize_text(text)
        entry_id = _compute_entry_id(norm_cat, norm_text)
        existing = self.load_entries()
        for item in existing:
            if item.id == entry_id:
                return item, False
        date_val = (created_at or datetime.now(UTC).strftime("%Y-%m-%d")).strip()
        entry = MemoryEntry(id=entry_id, category=norm_cat, text=norm_text, created_at=date_val)
        existing.append(entry)
        self._write_entries(existing)
        return entry, True

    def remove(self, entry_id: str) -> bool:
        """Delete an entry by exact ID or unambiguous ID prefix."""
        target = entry_id.strip().lower()
        if not target:
            return False
        entries = self.load_entries()
        exact = [item for item in entries if item.id == target]
        if exact:
            remaining = [item for item in entries if item.id != target]
            self._write_entries(remaining)
            return True
        matches = [item for item in entries if item.id.startswith(target)]
        if len(matches) != 1:
            return False
        matched_id = matches[0].id
        remaining = [item for item in entries if item.id != matched_id]
        self._write_entries(remaining)
        return True

    def clear(self) -> int:
        """Remove all persisted memory entries and return how many were cleared."""
        entries = self.load_entries()
        if not entries:
            return 0
        self._write_entries([])
        return len(entries)

    def search(self, query: str) -> list[MemoryEntry]:
        """Return entries whose category, text, or ID matches *query*."""
        terms = [tok for tok in query.strip().lower().split() if tok]
        entries = self.load_entries()
        if not terms:
            return entries
        return [
            item
            for item in entries
            if all(term in f"{item.id} {item.category} {item.text}".lower() for term in terms)
        ]

    def render_for_prompt(
        self,
        *,
        max_tokens: int = 800,
        max_entries: int = 30,
    ) -> RenderedMemory:
        """Render recent memory entries within *max_tokens* for static prompt injection."""
        entries = self.load_entries()
        if not entries or max_tokens <= 0 or max_entries <= 0:
            return RenderedMemory(self.path, (), "", 0, False)

        selected = entries[-max_entries:]
        truncated = len(entries) > max_entries
        try:
            rel_display = self.path.relative_to(self.repo_root).as_posix()
        except ValueError:
            rel_display = self.relative_path

        header = f"## Repository Memory ({rel_display})"
        lines = [header]
        kept: list[MemoryEntry] = []
        for item in selected:
            bullet = _format_prompt_bullet(item)
            candidate = "\n".join([*lines, bullet])
            if kept and _estimate_tokens(candidate) > max_tokens:
                truncated = True
                break
            if not kept and _estimate_tokens(candidate) > max_tokens:
                truncated = True
                lines.append(bullet[: max(32, max_tokens * 4 - len(header) - 8)].rstrip() + "...")
                kept.append(item)
                break
            lines.append(bullet)
            kept.append(item)

        if truncated and _estimate_tokens("\n".join([*lines, _TRUNCATION_NOTE])) <= max_tokens:
            lines.append(_TRUNCATION_NOTE)

        rendered = "\n".join(lines)
        return RenderedMemory(
            path=self.path,
            entries=tuple(kept),
            rendered=rendered,
            total_tokens=_estimate_tokens(rendered),
            truncated=truncated,
        )
