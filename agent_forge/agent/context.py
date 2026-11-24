"""Working-set management, two-stage compaction, and prompt-cache-aligned assembly."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_forge.agent.instructions import (
    DiscoveredInstructions,
    InstructionFile,
    discover_repo_instructions,
    discover_scoped_instructions_for_path,
)
from agent_forge.agent.repo_map import RepoMap, build_repo_map
from agent_forge.llm.base import Message, Role, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_forge.agent.models import AgentRun, ToolInvocation
    from agent_forge.config import ContextSettings

_WORKSPACE_PREFIX = "/workspace"
_SUMMARY_HEADER_PREFIX = "[CONTEXT SUMMARY"
_VERIFY_CMD_RE = re.compile(
    r"\b(pytest|unittest|ruff|mypy|flake8|npm\s+test|cargo\s+test|go\s+test|make\s+test)\b",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    """Estimate token count cleanly with a 4-chars-per-token heuristic."""
    return (len(text) + 3) // 4 if text else 0


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    """Estimate aggregate prompt tokens across a list of chat messages."""
    total = 0
    for msg in messages:
        total += 4 + estimate_tokens(msg.content)
        if msg.tool_call_id:
            total += estimate_tokens(msg.tool_call_id)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                args_json = json.dumps(tc.arguments, sort_keys=True, default=str)
                total += 12 + estimate_tokens(tc.name) + estimate_tokens(args_json)
    return total


def normalize_workspace_path(raw_path: str) -> str:
    """Normalize a sandbox ``/workspace/...`` or relative path for consistent tracking."""
    text = raw_path.strip().replace("\\", "/")
    if text == _WORKSPACE_PREFIX:
        return "."
    if text.startswith(_WORKSPACE_PREFIX + "/"):
        return text[len(_WORKSPACE_PREFIX) + 1 :].lstrip("/") or "."
    return text.lstrip("./") or "."


def _first_line(text: str, *, max_len: int = 160) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    line = stripped.splitlines()[0].strip()
    return line[: max_len - 3] + "..." if len(line) > max_len else line


def _looks_like_verification(command: str) -> bool:
    return bool(_VERIFY_CMD_RE.search(command))


def _is_error_tool_content(content: str) -> bool:
    return content.startswith("[ERROR]") or "\n[ERROR] " in content


@dataclass(frozen=True)
class ContextPolicy:
    """Budget and compaction policy governing :class:`ContextEngine`."""

    enabled: bool = True
    discover_instructions: bool = True
    max_instruction_tokens: int = 4_000
    repo_map_enabled: bool = True
    max_repo_map_tokens: int = 1_500
    max_repo_files: int = 500
    max_working_set_files: int = 12
    context_window_tokens: int = 100_000
    auto_size_context_window: bool = False
    compaction_trigger_ratio: float = 0.75
    compaction_target_ratio: float = 0.45
    keep_recent_turns: int = 4
    stale_tool_result_max_chars: int = 600

    @property
    def trigger_tokens(self) -> int:
        """Return token threshold at which compaction activates."""
        return max(200, int(self.context_window_tokens * self.compaction_trigger_ratio))

    @property
    def target_tokens(self) -> int:
        """Return desired prompt token count after compaction."""
        return max(150, int(self.context_window_tokens * self.compaction_target_ratio))

    @classmethod
    def from_settings(cls, settings: ContextSettings) -> ContextPolicy:
        """Build a :class:`ContextPolicy` from Pydantic :class:`ContextSettings`."""
        return cls(
            enabled=settings.enabled,
            discover_instructions=settings.discover_instructions,
            max_instruction_tokens=settings.max_instruction_tokens,
            repo_map_enabled=settings.repo_map_enabled,
            max_repo_map_tokens=settings.max_repo_map_tokens,
            max_repo_files=settings.max_repo_files,
            max_working_set_files=settings.max_working_set_files,
            context_window_tokens=settings.context_window_tokens,
            auto_size_context_window=(
                settings.auto_size_context_window and settings.context_window_tokens == 100_000
            ),
            compaction_trigger_ratio=settings.compaction_trigger_ratio,
            compaction_target_ratio=settings.compaction_target_ratio,
            keep_recent_turns=settings.keep_recent_turns,
            stale_tool_result_max_chars=settings.stale_tool_result_max_chars,
        )


@dataclass
class WorkingFileEntry:
    """Tracked workspace file inside the active working set."""

    rel_path: str
    pinned: bool = False
    last_iteration: int = 0
    last_action: str = "read"
    reads: int = 0
    writes: int = 0
    touched_order: int = 0


@dataclass(frozen=True)
class FailureRecord:
    """Unresolved failure preserved across compaction."""

    iteration: int
    tool_name: str
    target: str
    summary: str


@dataclass(frozen=True)
class VerificationSnapshot:
    """Latest observed verification command result."""

    iteration: int
    command: str
    exit_code: int
    summary: str


@dataclass
class WorkingSet:
    """Bounded working set of active files, failures, and verification state."""

    max_files: int = 12
    files: dict[str, WorkingFileEntry] = field(default_factory=dict)
    loaded_instruction_paths: list[str] = field(default_factory=list)
    active_failures: list[FailureRecord] = field(default_factory=list)
    last_verification: VerificationSnapshot | None = None
    evicted_tool_results: int = 0
    evicted_tokens: int = 0
    _seq: int = 0

    @property
    def modified_files(self) -> tuple[str, ...]:
        """Return pinned/modified file paths in deterministic touch order."""
        entries = [entry for entry in self.files.values() if entry.pinned]
        entries.sort(key=lambda item: item.touched_order)
        return tuple(entry.rel_path for entry in entries)

    def touch_file(self, path: str, *, action: str, iteration: int) -> str:
        """Record a read or edit access and evict oldest unpinned files over cap."""
        norm = normalize_workspace_path(path)
        if not norm or norm == ".":
            return norm
        self._seq += 1
        is_edit = action in {"write", "edit", "create", "delete"}
        entry = self.files.get(norm)
        if entry is None:
            entry = WorkingFileEntry(rel_path=norm)
            self.files[norm] = entry
        entry.pinned = entry.pinned or is_edit
        entry.last_iteration = iteration
        entry.last_action = action
        entry.touched_order = self._seq
        if is_edit:
            entry.writes += 1
        else:
            entry.reads += 1
        self._enforce_capacity()
        return norm

    def _enforce_capacity(self) -> None:
        if len(self.files) <= self.max_files:
            return
        unpinned = sorted(
            (e for e in self.files.values() if not e.pinned),
            key=lambda item: item.touched_order,
        )
        while len(self.files) > self.max_files and unpinned:
            victim = unpinned.pop(0)
            self.files.pop(victim.rel_path, None)

    def record_failure(
        self,
        *,
        iteration: int,
        tool_name: str,
        target: str,
        summary: str,
    ) -> None:
        """Record an active failure, replacing any older entry for the same target."""
        norm_target = normalize_workspace_path(target) if target else tool_name
        self.active_failures = [
            f
            for f in self.active_failures
            if not (f.tool_name == tool_name and f.target == norm_target)
        ]
        self.active_failures.append(
            FailureRecord(
                iteration=iteration,
                tool_name=tool_name,
                target=norm_target,
                summary=summary,
            )
        )

    def clear_failure_for(self, target: str) -> None:
        """Clear unresolved failures tied to *target*."""
        norm = normalize_workspace_path(target) if target else ""
        self.active_failures = [
            f for f in self.active_failures if f.target != target and f.target != norm
        ]

    def record_verification(
        self,
        *,
        iteration: int,
        command: str,
        exit_code: int,
        summary: str,
    ) -> None:
        """Store the latest verification command outcome."""
        self.last_verification = VerificationSnapshot(
            iteration=iteration,
            command=command,
            exit_code=exit_code,
            summary=summary,
        )

    def active_file_paths(self) -> list[str]:
        """Return active file paths ordered with pinned modified files first."""
        ordered = sorted(
            self.files.values(),
            key=lambda item: (not item.pinned, -item.touched_order, item.rel_path),
        )
        return [entry.rel_path for entry in ordered]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the working set into plain JSON-compatible data."""
        return {
            "active_files": self.active_file_paths(),
            "modified_files": list(self.modified_files),
            "loaded_instructions": list(self.loaded_instruction_paths),
            "active_failures": len(self.active_failures),
            "last_verification": (
                {
                    "command": self.last_verification.command,
                    "exit_code": self.last_verification.exit_code,
                    "iteration": self.last_verification.iteration,
                }
                if self.last_verification is not None
                else None
            ),
            "evicted_tool_results": self.evicted_tool_results,
            "evicted_tokens": self.evicted_tokens,
        }


@dataclass(frozen=True)
class CompactionEvent:
    """Telemetry recorded when a compaction pass trims stale context."""

    epoch: int
    iteration: int
    raw_tokens: int
    compacted_tokens: int
    tokens_saved: int
    pruned_tool_results: int
    summarized_turns: int
    working_set_files: tuple[str, ...]
    stable_prefix_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload for journaling."""
        return {
            "epoch": self.epoch,
            "iteration": self.iteration,
            "raw_tokens": self.raw_tokens,
            "compacted_tokens": self.compacted_tokens,
            "tokens_saved": self.tokens_saved,
            "pruned_tool_results": self.pruned_tool_results,
            "summarized_turns": self.summarized_turns,
            "working_set_files": list(self.working_set_files),
            "stable_prefix_hash": self.stable_prefix_hash,
        }


@dataclass(frozen=True)
class ContextAssembly:
    """Assembled prompt messages and prompt-cache metadata for one LLM turn."""

    messages: list[Message]
    stable_prefix_hash: str
    cache_breakpoint_index: int
    compaction_epoch: int
    raw_prompt_tokens: int
    assembled_prompt_tokens: int
    tokens_saved: int
    compacted_this_turn: bool
    compaction_event: CompactionEvent | None
    working_set_files: tuple[str, ...]


@dataclass(frozen=True)
class _TurnGroup:
    index: int
    entries: tuple[tuple[int, Message], ...]


def _partition_turn_groups(messages: Sequence[Message]) -> tuple[list[Message], list[_TurnGroup]]:
    """Split messages into immutable initial header and atomic assistant/tool turn groups."""
    if not messages:
        return [], []

    header_end = 0
    if messages[0].role == Role.SYSTEM:
        header_end = 1
    if (
        len(messages) > header_end
        and messages[header_end].role == Role.USER
        and not messages[header_end].content.startswith(_SUMMARY_HEADER_PREFIX)
    ):
        header_end += 1

    header = list(messages[:header_end])
    groups: list[_TurnGroup] = []
    idx = header_end
    total = len(messages)

    while idx < total:
        msg = messages[idx]
        entries: list[tuple[int, Message]] = [(idx, msg)]
        idx += 1
        if msg.role == Role.ASSISTANT and msg.tool_calls:
            while idx < total and messages[idx].role == Role.TOOL:
                entries.append((idx, messages[idx]))
                idx += 1
        groups.append(_TurnGroup(index=len(groups), entries=tuple(entries)))

    return header, groups


def compute_stable_prefix_hash(
    system_prompt: str,
    tool_definitions: Sequence[ToolDefinition],
) -> str:
    """Compute a deterministic hash covering static instructions, repo map, and tool schemas."""
    tool_payload = [
        {"name": td.name, "description": td.description, "parameters": td.parameters}
        for td in sorted(tool_definitions, key=lambda item: item.name)
    ]
    blob = json.dumps(
        {"system": system_prompt, "tools": tool_payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _extract_apply_patch_paths(arguments: dict[str, object]) -> list[tuple[str, str]]:
    if arguments.get("dry_run"):
        return []
    raw_ops = arguments.get("operations")
    if not isinstance(raw_ops, list):
        return []
    pairs: list[tuple[str, str]] = []
    for raw_op in raw_ops:
        if isinstance(raw_op, dict):
            path = raw_op.get("path")
            action = str(raw_op.get("action") or "modify")
            if isinstance(path, str) and path.strip():
                pairs.append((path, action))
    return pairs


def _build_call_lookup(
    groups: Sequence[_TurnGroup],
) -> dict[str, tuple[int, ToolCall]]:
    lookup: dict[str, tuple[int, ToolCall]] = {}
    for group in groups:
        for msg_idx, msg in group.entries:
            if msg.role == Role.ASSISTANT and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.id:
                        lookup[tc.id] = (msg_idx, tc)
    return lookup


def _latest_file_touch_indices(
    groups: Sequence[_TurnGroup],
    call_lookup: dict[str, tuple[int, ToolCall]],
) -> dict[str, int]:
    latest: dict[str, int] = {}
    for group in groups:
        for msg_idx, msg in group.entries:
            if msg.role != Role.TOOL or not msg.tool_call_id:
                continue
            call_info = call_lookup.get(msg.tool_call_id)
            if call_info is None:
                continue
            _, tc = call_info
            if tc.name in {"read_file", "write_file", "edit_file"}:
                raw_path = tc.arguments.get("path")
                if isinstance(raw_path, str) and raw_path.strip():
                    latest[normalize_workspace_path(raw_path)] = msg_idx
            elif tc.name == "apply_patch":
                for p, _ in _extract_apply_patch_paths(tc.arguments):
                    latest[normalize_workspace_path(p)] = msg_idx
    return latest


def _format_tool_stub(tc: ToolCall | None, raw_content: str, *, superseded: bool) -> str:
    chars = len(raw_content)
    if tc is None:
        return f"[Compacted tool output — {chars} chars omitted]"
    if tc.name == "read_file" and superseded:
        path = normalize_workspace_path(str(tc.arguments.get("path", "")))
        return f"[Compacted tool output: read_file('{path}') superseded — {chars} chars omitted]"
    if _is_error_tool_content(raw_content):
        head = raw_content[:240].strip()
        tail = raw_content[-240:].strip()
        return f"{head}\n[... {chars - 480} error chars omitted ...]\n{tail}"
    target = str(tc.arguments.get("path") or tc.arguments.get("command") or "").strip()
    label = f"{tc.name}({target[:60]})" if target else tc.name
    return f"[Compacted tool output: {label} — {chars} chars omitted]"


def _format_tool_note(msg: Message, calls_by_id: dict[str, ToolCall]) -> str:
    tc = calls_by_id.get(msg.tool_call_id or "")
    name = tc.name if tc is not None else "tool"
    arg = ""
    if tc is not None:
        raw = tc.arguments.get("path") or tc.arguments.get("command") or ""
        arg = f"({str(raw)[:50]})" if raw else ""
    status = "error" if _is_error_tool_content(msg.content) else "ok"
    return f"{name}{arg} -> {status}"


def _extract_group_note(group: _TurnGroup) -> str:
    notes: list[str] = []
    calls_by_id: dict[str, ToolCall] = {}
    for _, msg in group.entries:
        if msg.role == Role.ASSISTANT:
            for tc in msg.tool_calls or ():
                if tc.id:
                    calls_by_id[tc.id] = tc
            summary = _first_line(msg.content, max_len=120)
            if summary and not summary.startswith(_SUMMARY_HEADER_PREFIX):
                notes.append(f"assistant: {summary}")
        elif msg.role == Role.TOOL:
            notes.append(_format_tool_note(msg, calls_by_id))
        elif msg.role == Role.USER:
            summary = _first_line(msg.content, max_len=120)
            if summary:
                notes.append(f"user: {summary}")
    return "; ".join(notes) if notes else "turn step"


def _render_durable_summary(
    task: str,
    working_set: WorkingSet,
    notes: Sequence[str],
    summarized_turns: int,
) -> str:
    sections = [
        f"{_SUMMARY_HEADER_PREFIX} — {summarized_turns} earlier turns]",
        f"Goal: {_first_line(task, max_len=240)}",
    ]
    modified = ", ".join(working_set.modified_files) or "(none yet)"
    inspected = ", ".join(working_set.active_file_paths()[:10]) or "(none)"
    sections.append(f"Working Set (modified): {modified}")
    sections.append(f"Working Set (active): {inspected}")

    if working_set.loaded_instruction_paths:
        sections.append("Loaded Instructions: " + ", ".join(working_set.loaded_instruction_paths))
    if working_set.active_failures:
        fail_lines = [
            f"- iter {f.iteration} {f.tool_name}({f.target}): {f.summary}"
            for f in working_set.active_failures[-5:]
        ]
        sections.append("Unresolved Failures:\n" + "\n".join(fail_lines))
    if working_set.last_verification is not None:
        v = working_set.last_verification
        status = "PASSED" if v.exit_code == 0 else f"FAILED (exit {v.exit_code})"
        sections.append(f"Verification State: `{v.command}` -> {status} ({v.summary})")
    if notes:
        tail_notes = list(notes[-16:])
        sections.append("Key Decisions & Edits:\n" + "\n".join(f"- {n}" for n in tail_notes))
    return "\n".join(sections)


class ContextEngine:
    """Assembles bounded, instruction-aware, prompt-cache-stable LLM context."""

    def __init__(
        self,
        repo_path: Path | str,
        *,
        task: str = "",
        policy: ContextPolicy | None = None,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.task = task
        self.policy = policy or ContextPolicy()
        self.working_set = WorkingSet(max_files=self.policy.max_working_set_files)

        self._initial_prepared = False
        self._base_instructions = DiscoveredInstructions((), "", 0, False)
        self._task_scoped_instructions = DiscoveredInstructions((), "", 0, False)
        self._repo_map = RepoMap("", (), 0, 0, 0, False)
        self._loaded_instruction_resolved: set[Path] = set()
        self._pending_scoped_instructions: list[InstructionFile] = []

        self._observed_invocations = 0
        self._compaction_epoch = 0
        self._compacted_group_cutoff = 0
        self._folded_notes: list[str] = []
        self._summary_message_text = ""
        self._pruned_tool_stubs: dict[int, str] = {}
        self._tokens_saved_total = 0

    def sync_context_window(self, max_context_tokens: int) -> None:
        """Align auto-sized context window budget with resolved model capabilities."""
        if (
            self.policy.auto_size_context_window
            and max_context_tokens > 0
            and self.policy.context_window_tokens != max_context_tokens
        ):
            self.policy = replace(self.policy, context_window_tokens=max_context_tokens)

    def prepare_initial_context(self) -> tuple[DiscoveredInstructions, RepoMap]:
        """Discover repo instructions and build the structural repository map once."""
        if self._initial_prepared:
            return self._base_instructions, self._repo_map
        self._initial_prepared = True

        if not self.policy.enabled:
            return self._base_instructions, self._repo_map

        if self.policy.discover_instructions:
            base = discover_repo_instructions(
                self.repo_path,
                task="",
                max_tokens=self.policy.max_instruction_tokens,
            )
            self._base_instructions = base
            self._record_discovered_instructions(base)

            remaining = max(0, self.policy.max_instruction_tokens - base.total_tokens)
            if self.task and remaining >= 32:
                scoped = discover_repo_instructions(
                    self.repo_path,
                    task=self.task,
                    max_tokens=remaining,
                    exclude_resolved=self._loaded_instruction_resolved,
                )
                self._task_scoped_instructions = scoped
                self._record_discovered_instructions(scoped)

        if self.policy.repo_map_enabled:
            self._repo_map = build_repo_map(
                self.repo_path,
                task="",
                max_tokens=self.policy.max_repo_map_tokens,
                max_files=self.policy.max_repo_files,
            )
        return self._base_instructions, self._repo_map

    @property
    def task_scoped_instructions_rendered(self) -> str:
        """Return initial child-directory instructions discovered from *task* path mentions."""
        return self._task_scoped_instructions.rendered

    def _record_discovered_instructions(self, discovered: DiscoveredInstructions) -> None:
        for item in discovered.files:
            try:
                self._loaded_instruction_resolved.add(item.path.resolve())
            except OSError:
                self._loaded_instruction_resolved.add(item.path)
            if item.rel_path not in self.working_set.loaded_instruction_paths:
                self.working_set.loaded_instruction_paths.append(item.rel_path)

    def _discover_scoped_for_path(self, raw_path: str) -> None:
        if not self.policy.discover_instructions or not raw_path.strip():
            return
        discovered = discover_scoped_instructions_for_path(
            self.repo_path,
            raw_path,
            already_loaded=self._loaded_instruction_resolved,
            max_tokens=max(256, self.policy.max_instruction_tokens // 2),
        )
        if not discovered.files:
            return
        self._record_discovered_instructions(discovered)
        self._pending_scoped_instructions.extend(discovered.files)

    def _observe_file_invocation(self, inv: ToolInvocation, action: str) -> None:
        raw_path = str(inv.arguments.get("path", ""))
        if not raw_path.strip():
            return
        norm = self.working_set.touch_file(raw_path, action=action, iteration=inv.iteration)
        self._discover_scoped_for_path(raw_path)
        if inv.result.exit_code == 0 and action in {"write", "edit"}:
            self.working_set.clear_failure_for(norm)
        elif inv.result.exit_code != 0:
            summary = _first_line(inv.result.error or inv.result.output) or "tool failed"
            self.working_set.record_failure(
                iteration=inv.iteration,
                tool_name=inv.tool_name,
                target=norm,
                summary=summary,
            )

    def _observe_patch_invocation(self, inv: ToolInvocation) -> None:
        pairs = _extract_apply_patch_paths(inv.arguments)
        for raw_path, action in pairs:
            norm = self.working_set.touch_file(raw_path, action=action, iteration=inv.iteration)
            self._discover_scoped_for_path(raw_path)
            if inv.result.exit_code == 0:
                self.working_set.clear_failure_for(norm)
        if inv.result.exit_code != 0:
            target = pairs[0][0] if pairs else inv.tool_name
            summary = _first_line(inv.result.error or inv.result.output) or "patch failed"
            self.working_set.record_failure(
                iteration=inv.iteration,
                tool_name=inv.tool_name,
                target=target,
                summary=summary,
            )

    def _observe_shell_invocation(self, inv: ToolInvocation) -> None:
        cmd = str(inv.arguments.get("command", "")).strip()
        summary = (
            _first_line(inv.result.error or inv.result.output) or f"exit {inv.result.exit_code}"
        )
        if cmd and _looks_like_verification(cmd):
            self.working_set.record_verification(
                iteration=inv.iteration,
                command=cmd,
                exit_code=inv.result.exit_code,
                summary=summary,
            )
            if inv.result.exit_code == 0:
                self.working_set.clear_failure_for(cmd)
        if inv.result.exit_code != 0:
            self.working_set.record_failure(
                iteration=inv.iteration,
                tool_name=inv.tool_name,
                target=cmd or inv.tool_name,
                summary=summary,
            )
        elif cmd:
            self.working_set.clear_failure_for(cmd)

    def observe_tool_invocation(self, invocation: ToolInvocation) -> None:
        """Update the working set and queue scoped instructions from one tool result."""
        name = invocation.tool_name
        if name == "read_file":
            self._observe_file_invocation(invocation, "read")
        elif name in {"write_file", "edit_file"}:
            self._observe_file_invocation(invocation, "write")
        elif name == "apply_patch":
            self._observe_patch_invocation(invocation)
        elif name in {"list_directory", "search_codebase", "git_diff"}:
            raw = str(invocation.arguments.get("path", "")).strip()
            if raw and normalize_workspace_path(raw) != ".":
                self._observe_file_invocation(invocation, "read")
        elif name in {"run_shell", "shell_start"}:
            self._observe_shell_invocation(invocation)
        elif invocation.result.exit_code != 0:
            summary = _first_line(invocation.result.error or invocation.result.output) or "failed"
            self.working_set.record_failure(
                iteration=invocation.iteration,
                tool_name=name,
                target=name,
                summary=summary,
            )

    def sync_from_run(self, run: AgentRun) -> None:
        """Catch up working-set observations from newly appended ``run.tool_invocations``."""
        if not self._initial_prepared:
            self.prepare_initial_context()
        while self._observed_invocations < len(run.tool_invocations):
            self.observe_tool_invocation(run.tool_invocations[self._observed_invocations])
            self._observed_invocations += 1

    def pop_scoped_instruction_message(self, run: AgentRun) -> Message | None:
        """Return an append-only user message carrying newly unlocked scoped instructions."""
        self.sync_from_run(run)
        if not self._pending_scoped_instructions:
            return None
        items = list(self._pending_scoped_instructions)
        self._pending_scoped_instructions.clear()
        lines = [
            "[SCOPED REPOSITORY INSTRUCTIONS]",
            "More specific directory instructions for touched paths (override broader rules):",
        ]
        for item in items:
            lines.append(f"\n### Instructions: {item.rel_path} ({item.scope})\n{item.content}")
        return Message(role=Role.USER, content="\n".join(lines))

    def _render_view(
        self,
        header: Sequence[Message],
        groups: Sequence[_TurnGroup],
        *,
        cutoff: int,
        summary_text: str,
    ) -> list[Message]:
        view: list[Message] = list(header)
        if cutoff > 0 and summary_text:
            view.append(Message(role=Role.USER, content=summary_text))
        for group in groups[cutoff:]:
            for msg_idx, msg in group.entries:
                stub = self._pruned_tool_stubs.get(msg_idx)
                if stub is not None and msg.role == Role.TOOL:
                    view.append(
                        Message(
                            role=msg.role,
                            content=stub,
                            tool_call_id=msg.tool_call_id,
                            tool_calls=msg.tool_calls,
                        )
                    )
                else:
                    view.append(msg)
        return view

    def _apply_stage1_pruning(
        self,
        groups: Sequence[_TurnGroup],
        *,
        start_group: int,
        protected_start: int,
        call_lookup: dict[str, tuple[int, ToolCall]],
        latest_touches: dict[str, int],
    ) -> int:
        pruned_count = 0
        max_chars = self.policy.stale_tool_result_max_chars
        for group in groups[start_group:protected_start]:
            for msg_idx, msg in group.entries:
                if msg.role != Role.TOOL or msg_idx in self._pruned_tool_stubs:
                    continue
                call_info = call_lookup.get(msg.tool_call_id or "")
                tc = call_info[1] if call_info is not None else None
                superseded = False
                if tc is not None and tc.name == "read_file":
                    path = normalize_workspace_path(str(tc.arguments.get("path", "")))
                    superseded = latest_touches.get(path, -1) > msg_idx
                is_err = _is_error_tool_content(msg.content)
                oversized = len(msg.content) > (max_chars * 2 if is_err else max_chars)
                if not superseded and not oversized:
                    continue
                stub = _format_tool_stub(tc, msg.content, superseded=superseded)
                if len(stub) >= len(msg.content):
                    continue
                self._pruned_tool_stubs[msg_idx] = stub
                self.working_set.evicted_tool_results += 1
                saved = max(0, estimate_tokens(msg.content) - estimate_tokens(stub))
                self.working_set.evicted_tokens += saved
                pruned_count += 1
        return pruned_count

    def _apply_stage2_summarization(
        self,
        run: AgentRun,
        header: Sequence[Message],
        groups: Sequence[_TurnGroup],
        *,
        protected_start: int,
    ) -> int:
        if protected_start <= self._compacted_group_cutoff:
            return 0
        notes = list(self._folded_notes)
        cutoff = self._compacted_group_cutoff
        summary_text = self._summary_message_text

        while cutoff < protected_start:
            notes.append(_extract_group_note(groups[cutoff]))
            cutoff += 1
            summary_text = _render_durable_summary(run.task, self.working_set, notes, cutoff)
            candidate_view = self._render_view(
                header,
                groups,
                cutoff=cutoff,
                summary_text=summary_text,
            )
            if estimate_messages_tokens(candidate_view) <= self.policy.target_tokens:
                break

        summarized = cutoff - self._compacted_group_cutoff
        self._compacted_group_cutoff = cutoff
        self._folded_notes = notes
        self._summary_message_text = summary_text
        return summarized

    def assemble(
        self,
        run: AgentRun,
        *,
        tool_definitions: Sequence[ToolDefinition] = (),
    ) -> ContextAssembly:
        """Assemble the prompt view, running two-stage compaction when over budget."""
        self.sync_from_run(run)
        raw_tokens = estimate_messages_tokens(run.messages)
        system_text = (
            run.messages[0].content if run.messages and run.messages[0].role == Role.SYSTEM else ""
        )
        prefix_hash = compute_stable_prefix_hash(system_text, tool_definitions)

        if not self.policy.enabled:
            return ContextAssembly(
                messages=list(run.messages),
                stable_prefix_hash=prefix_hash,
                cache_breakpoint_index=0,
                compaction_epoch=0,
                raw_prompt_tokens=raw_tokens,
                assembled_prompt_tokens=raw_tokens,
                tokens_saved=0,
                compacted_this_turn=False,
                compaction_event=None,
                working_set_files=tuple(self.working_set.active_file_paths()),
            )

        header, groups = _partition_turn_groups(run.messages)
        current_view = self._render_view(
            header,
            groups,
            cutoff=self._compacted_group_cutoff,
            summary_text=self._summary_message_text,
        )
        current_tokens = estimate_messages_tokens(current_view)
        compaction_event: CompactionEvent | None = None

        if current_tokens > self.policy.trigger_tokens:
            protected_start = max(
                self._compacted_group_cutoff,
                len(groups) - self.policy.keep_recent_turns,
            )
            call_lookup = _build_call_lookup(groups)
            latest_touches = _latest_file_touch_indices(groups, call_lookup)
            pruned_count = self._apply_stage1_pruning(
                groups,
                start_group=self._compacted_group_cutoff,
                protected_start=protected_start,
                call_lookup=call_lookup,
                latest_touches=latest_touches,
            )
            current_view = self._render_view(
                header,
                groups,
                cutoff=self._compacted_group_cutoff,
                summary_text=self._summary_message_text,
            )
            summarized_turns = 0
            if estimate_messages_tokens(current_view) > self.policy.target_tokens:
                summarized_turns = self._apply_stage2_summarization(
                    run,
                    header,
                    groups,
                    protected_start=protected_start,
                )
                current_view = self._render_view(
                    header,
                    groups,
                    cutoff=self._compacted_group_cutoff,
                    summary_text=self._summary_message_text,
                )
            if pruned_count > 0 or summarized_turns > 0:
                self._compaction_epoch += 1
                new_tokens = estimate_messages_tokens(current_view)
                saved_now = max(0, current_tokens - new_tokens)
                self._tokens_saved_total += saved_now
                compaction_event = CompactionEvent(
                    epoch=self._compaction_epoch,
                    iteration=run.iterations,
                    raw_tokens=raw_tokens,
                    compacted_tokens=new_tokens,
                    tokens_saved=saved_now,
                    pruned_tool_results=pruned_count,
                    summarized_turns=summarized_turns,
                    working_set_files=tuple(self.working_set.active_file_paths()),
                    stable_prefix_hash=prefix_hash,
                )
                current_tokens = new_tokens

        breakpoint_idx = len(header) if self._compacted_group_cutoff > 0 else 0
        return ContextAssembly(
            messages=current_view,
            stable_prefix_hash=prefix_hash,
            cache_breakpoint_index=breakpoint_idx,
            compaction_epoch=self._compaction_epoch,
            raw_prompt_tokens=raw_tokens,
            assembled_prompt_tokens=current_tokens,
            tokens_saved=max(0, raw_tokens - current_tokens),
            compacted_this_turn=compaction_event is not None,
            compaction_event=compaction_event,
            working_set_files=tuple(self.working_set.active_file_paths()),
        )

    def stats_dict(self, assembly: ContextAssembly | None = None) -> dict[str, Any]:
        """Return JSON-serializable telemetry describing assembled context state."""
        stats: dict[str, Any] = {
            "enabled": self.policy.enabled,
            "discovered_instructions": len(self.working_set.loaded_instruction_paths),
            "instruction_paths": list(self.working_set.loaded_instruction_paths),
            "repo_map_files": len(self._repo_map.ranked_files),
            "repo_map_tokens": self._repo_map.estimated_tokens,
            "working_set_files": self.working_set.active_file_paths(),
            "modified_files": list(self.working_set.modified_files),
            "compactions": self._compaction_epoch,
            "evicted_tool_results": self.working_set.evicted_tool_results,
            "tokens_saved": self._tokens_saved_total,
        }
        if assembly is not None:
            stats["stable_prefix_hash"] = assembly.stable_prefix_hash[:16]
            stats["cache_breakpoint_index"] = assembly.cache_breakpoint_index
            stats["last_raw_prompt_tokens"] = assembly.raw_prompt_tokens
            stats["last_assembled_prompt_tokens"] = assembly.assembled_prompt_tokens
            stats["tokens_saved"] = max(self._tokens_saved_total, assembly.tokens_saved)
        return stats
