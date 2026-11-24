"""Durable multi-turn ADE DevSession model and disk-backed SessionStore."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_forge.agent.context import ContextEngine, ContextPolicy
from agent_forge.agent.models import AgentConfig, AgentRun, ToolInvocation
from agent_forge.agent.persistence import (
    _atomic_write_text,
    deserialize_message,
    deserialize_token_usage,
    deserialize_tool_invocation,
    serialize_message,
    serialize_token_usage,
    serialize_tool_invocation,
)
from agent_forge.config import USER_CONFIG_DIR
from agent_forge.llm.base import Message, Role, TokenUsage

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_session_id(session_id: str) -> str:
    """Validate *session_id* to prevent directory traversal under the sessions root."""
    cleaned = session_id.strip()
    if not cleaned or ".." in cleaned or not _SESSION_ID_RE.match(cleaned):
        msg = f"Invalid session ID: '{session_id}'"
        raise ValueError(msg)
    return cleaned


def _summarize_title(text: str, *, max_len: int = 80) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first_line:
        return "Untitled session"
    return first_line[: max_len - 3] + "..." if len(first_line) > max_len else first_line


def _extract_turn_reply(new_messages: list[Message]) -> str:
    fallback = ""
    for msg in reversed(new_messages):
        if msg.role == Role.ASSISTANT:
            if msg.content.strip():
                return msg.content
            fallback = msg.content
    return fallback


@dataclass
class SessionTurn:
    """Record of a single user prompt and agent response within a :class:`DevSession`."""

    turn_index: int
    prompt: str
    response: str = ""
    run_id: str = ""
    state: str = "completed"
    iterations: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    tokens: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0, 0))
    message_start_index: int = 0
    message_end_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize this turn to a JSON-compatible dict."""
        return {
            "turn_index": self.turn_index,
            "prompt": self.prompt,
            "response": self.response,
            "run_id": self.run_id,
            "state": self.state,
            "iterations": self.iterations,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "tokens": serialize_token_usage(self.tokens),
            "message_start_index": self.message_start_index,
            "message_end_index": self.message_end_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionTurn:
        """Restore a :class:`SessionTurn` from a persisted dict."""
        completed_raw = data.get("completed_at")
        return cls(
            turn_index=int(data["turn_index"]),
            prompt=str(data.get("prompt", "")),
            response=str(data.get("response", "")),
            run_id=str(data.get("run_id", "")),
            state=str(data.get("state", "completed")),
            iterations=int(data.get("iterations", 0)),
            started_at=datetime.fromisoformat(str(data["started_at"])),
            completed_at=datetime.fromisoformat(completed_raw) if completed_raw else None,
            tokens=deserialize_token_usage(data.get("tokens")),
            message_start_index=int(data.get("message_start_index", 0)),
            message_end_index=int(data.get("message_end_index", 0)),
        )


@dataclass(frozen=True)
class SessionSummary:
    """Lightweight session metadata returned when listing stored sessions."""

    id: str
    repo_path: str
    title: str
    provider: str
    model: str
    turn_count: int
    message_count: int
    total_tokens: TokenUsage
    created_at: datetime
    updated_at: datetime


@dataclass
class DevSession:
    """Multi-turn interactive development session persisted across restarts."""

    repo_path: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    provider: str = "gemini"
    model: str = "gemini-3.1-flash-lite-preview"
    profile_id: str | None = None
    approval_mode: str = "auto"
    sandbox_backend: str = "docker"
    sandbox_image: str = "agent-forge-sandbox:latest"
    system_prompt: str | None = None
    messages: list[Message] = field(default_factory=list)
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    turns: list[SessionTurn] = field(default_factory=list)
    context_state: dict[str, Any] = field(default_factory=dict)
    total_tokens: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0, 0))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def build_context_engine(
        self,
        *,
        task: str = "",
        policy: ContextPolicy | None = None,
    ) -> ContextEngine:
        """Create or restore a :class:`ContextEngine` bound to this session."""
        if self.context_state:
            return ContextEngine.from_state(
                self.repo_path,
                self.context_state,
                policy=policy,
            )
        seed_task = task or (self.turns[0].prompt if self.turns else self.title)
        return ContextEngine(self.repo_path, task=seed_task, policy=policy)

    def capture_context_engine(self, engine: ContextEngine) -> None:
        """Persist *engine*'s full internal state into this session."""
        self.context_state = engine.export_state()
        self.updated_at = datetime.now(UTC)

    def prepare_next_run(
        self,
        prompt: str,
        *,
        config: AgentConfig,
        run_id: str | None = None,
    ) -> AgentRun:
        """Build an :class:`AgentRun` seeded with this session's prior conversation state."""
        run_kwargs: dict[str, Any] = {
            "task": prompt,
            "repo_path": self.repo_path,
            "config": config,
        }
        if run_id is not None:
            run_kwargs["id"] = run_id

        if self.messages:
            seeded_messages = [*self.messages, Message(role=Role.USER, content=prompt)]
            if self.system_prompt is None and seeded_messages[0].role == Role.SYSTEM:
                self.system_prompt = seeded_messages[0].content
            run_kwargs["messages"] = seeded_messages
            run_kwargs["tool_invocations"] = list(self.tool_invocations)

        return AgentRun(**run_kwargs)

    def record_turn(
        self,
        *,
        prompt: str,
        run: AgentRun,
        context_engine: ContextEngine | None = None,
        started_at: datetime | None = None,
    ) -> SessionTurn:
        """Append the completed *run* as the next session turn and capture updated state."""
        prev_len = len(self.messages)
        new_slice = list(run.messages[prev_len:])
        reply = _extract_turn_reply(new_slice)

        self.messages = list(run.messages)
        self.tool_invocations = list(run.tool_invocations)
        if self.messages and self.messages[0].role == Role.SYSTEM:
            self.system_prompt = self.messages[0].content
        if not self.title:
            self.title = _summarize_title(prompt)

        self.provider = run.config.provider
        self.model = run.config.model
        self.profile_id = run.config.profile_id
        self.approval_mode = run.config.approval_mode

        turn_tokens = TokenUsage(
            prompt_tokens=run.total_tokens.prompt_tokens,
            completion_tokens=run.total_tokens.completion_tokens,
            total_tokens=run.total_tokens.total_tokens,
            cached_prompt_tokens=run.total_tokens.cached_prompt_tokens,
            reasoning_tokens=run.total_tokens.reasoning_tokens,
        )
        self.total_tokens = TokenUsage(
            prompt_tokens=self.total_tokens.prompt_tokens + turn_tokens.prompt_tokens,
            completion_tokens=self.total_tokens.completion_tokens + turn_tokens.completion_tokens,
            total_tokens=self.total_tokens.total_tokens + turn_tokens.total_tokens,
            cached_prompt_tokens=(
                self.total_tokens.cached_prompt_tokens + turn_tokens.cached_prompt_tokens
            ),
            reasoning_tokens=self.total_tokens.reasoning_tokens + turn_tokens.reasoning_tokens,
        )

        now = datetime.now(UTC)
        turn = SessionTurn(
            turn_index=len(self.turns),
            prompt=prompt,
            response=reply,
            run_id=run.id,
            state=run.state.value,
            iterations=run.iterations,
            started_at=started_at or run.created_at,
            completed_at=run.completed_at or now,
            tokens=turn_tokens,
            message_start_index=prev_len,
            message_end_index=len(self.messages),
        )
        self.turns.append(turn)
        if context_engine is not None:
            self.context_state = context_engine.export_state()
        self.updated_at = now
        return turn

    def to_summary(self) -> SessionSummary:
        """Return compact metadata summary for listings."""
        return SessionSummary(
            id=self.id,
            repo_path=self.repo_path,
            title=self.title,
            provider=self.provider,
            model=self.model,
            turn_count=len(self.turns),
            message_count=len(self.messages),
            total_tokens=self.total_tokens,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


def _default_sessions_dir() -> Path:
    return USER_CONFIG_DIR / "sessions"


class SessionStore:
    """Disk-backed store persisting :class:`DevSession` instances across restarts."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir is not None else _default_sessions_dir()

    @property
    def base_dir(self) -> Path:
        """Root directory holding stored session folders."""
        return self._base_dir

    def _session_dir(self, session_id: str) -> Path:
        safe_id = validate_session_id(session_id)
        return self._base_dir / safe_id

    def exists(self, session_id: str) -> bool:
        """Return True if a session folder and metadata file exist on disk."""
        session_dir = self._session_dir(session_id)
        return (session_dir / "session.json").is_file()

    def save(self, session: DevSession) -> Path:
        """Persist *session*, its messages, tool events, and context engine snapshot atomically."""
        session_dir = self._session_dir(session.id)
        session_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "version": 1,
            "id": validate_session_id(session.id),
            "repo_path": str(Path(session.repo_path).resolve()),
            "title": session.title,
            "provider": session.provider,
            "model": session.model,
            "profile_id": session.profile_id,
            "approval_mode": session.approval_mode,
            "sandbox_backend": session.sandbox_backend,
            "sandbox_image": session.sandbox_image,
            "system_prompt": session.system_prompt,
            "turns": [turn.to_dict() for turn in session.turns],
            "message_count": len(session.messages),
            "total_tokens": serialize_token_usage(session.total_tokens),
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
        }
        _atomic_write_text(session_dir / "session.json", json.dumps(meta, indent=2) + "\n")

        msg_lines = [json.dumps(serialize_message(msg)) + "\n" for msg in session.messages]
        _atomic_write_text(session_dir / "messages.jsonl", "".join(msg_lines))

        event_lines = [
            json.dumps(serialize_tool_invocation(inv)) + "\n" for inv in session.tool_invocations
        ]
        _atomic_write_text(session_dir / "events.jsonl", "".join(event_lines))

        _atomic_write_text(
            session_dir / "context_state.json",
            json.dumps(session.context_state, indent=2) + "\n",
        )
        return session_dir

    def load(self, session_id: str) -> DevSession:
        """Restore a :class:`DevSession` from disk."""
        session_dir = self._session_dir(session_id)
        meta_path = session_dir / "session.json"
        if not meta_path.is_file():
            msg = f"Session not found: {session_id}"
            raise FileNotFoundError(msg)

        meta = json.loads(meta_path.read_text(encoding="utf-8"))

        messages: list[Message] = []
        messages_path = session_dir / "messages.jsonl"
        if messages_path.is_file():
            for line in messages_path.read_text(encoding="utf-8").strip().splitlines():
                if line.strip():
                    messages.append(deserialize_message(json.loads(line)))

        invocations: list[ToolInvocation] = []
        events_path = session_dir / "events.jsonl"
        if events_path.is_file():
            for line in events_path.read_text(encoding="utf-8").strip().splitlines():
                if line.strip():
                    invocations.append(deserialize_tool_invocation(json.loads(line)))

        context_state: dict[str, Any] = {}
        context_path = session_dir / "context_state.json"
        if context_path.is_file():
            loaded_ctx = json.loads(context_path.read_text(encoding="utf-8"))
            if isinstance(loaded_ctx, dict):
                context_state = loaded_ctx

        turns = [
            SessionTurn.from_dict(item) for item in meta.get("turns", []) if isinstance(item, dict)
        ]
        return DevSession(
            repo_path=str(meta["repo_path"]),
            id=str(meta["id"]),
            title=str(meta.get("title", "")),
            provider=str(meta.get("provider", "gemini")),
            model=str(meta.get("model", "gemini-3.1-flash-lite-preview")),
            profile_id=meta.get("profile_id"),
            approval_mode=str(meta.get("approval_mode", "auto")),
            sandbox_backend=str(meta.get("sandbox_backend", "docker")),
            sandbox_image=str(meta.get("sandbox_image", "agent-forge-sandbox:latest")),
            system_prompt=meta.get("system_prompt"),
            messages=messages,
            tool_invocations=invocations,
            turns=turns,
            context_state=context_state,
            total_tokens=deserialize_token_usage(meta.get("total_tokens")),
            created_at=datetime.fromisoformat(str(meta["created_at"])),
            updated_at=datetime.fromisoformat(str(meta["updated_at"])),
            metadata=dict(meta.get("metadata") or {}),
        )

    def list_sessions(
        self,
        *,
        repo_path: str | Path | None = None,
        limit: int | None = None,
    ) -> list[SessionSummary]:
        """List stored sessions sorted newest-first, optionally filtered by repository path."""
        if not self._base_dir.is_dir():
            return []
        resolved_filter = str(Path(repo_path).resolve()) if repo_path is not None else None
        summaries: list[SessionSummary] = []

        for entry in self._base_dir.iterdir():
            meta_path = entry / "session.json"
            if not entry.is_dir() or not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                entry_repo = str(Path(str(meta["repo_path"])).resolve())
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
            if resolved_filter is not None and entry_repo != resolved_filter:
                continue
            turns = meta.get("turns") or []
            summaries.append(
                SessionSummary(
                    id=str(meta["id"]),
                    repo_path=entry_repo,
                    title=str(meta.get("title", "")),
                    provider=str(meta.get("provider", "gemini")),
                    model=str(meta.get("model", "")),
                    turn_count=len(turns),
                    message_count=int(meta.get("message_count", 0)),
                    total_tokens=deserialize_token_usage(meta.get("total_tokens")),
                    created_at=datetime.fromisoformat(str(meta["created_at"])),
                    updated_at=datetime.fromisoformat(str(meta["updated_at"])),
                )
            )

        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return summaries[:limit] if limit is not None else summaries

    def latest_for_repo(self, repo_path: str | Path) -> DevSession | None:
        """Load the most recently updated session matching *repo_path*, if any."""
        matches = self.list_sessions(repo_path=repo_path, limit=1)
        if not matches:
            return None
        return self.load(matches[0].id)

    def delete(self, session_id: str) -> bool:
        """Delete a stored session directory. Returns True if it existed."""
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return False
        shutil.rmtree(session_dir)
        return True
