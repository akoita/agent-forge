"""Unit tests for Issue #223: ADE /subagents, /apply, [code-mode] formatting, & ACP parity."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from agent_forge.acp.protocol import classify_tool_kind, map_stream_event_to_acp_updates
from agent_forge.agent.chat import ChatSessionRuntime
from agent_forge.agent.session import DevSession, SessionStore
from agent_forge.config import ForgeConfig
from agent_forge.orchestration.event_stream import StreamEvent
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeCoordinator:
    statuses: list[str]
    deltas: list[str]

    def emit_delta(self, text: str) -> None:
        self.deltas.append(text)

    def emit_status(self, line: str) -> None:
        self.statuses.append(line)


class _DummySandbox(Sandbox):
    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del repo_path, config

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del command, timeout_seconds
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str) -> str:
        del path
        return ""

    async def write_file(self, path: str, content: str) -> None:
        del path, content

    async def list_files(self, path: str = "/workspace") -> list[str]:
        del path
        return []

    async def stop(self) -> None:
        return None

    async def is_alive(self) -> bool:
        return True


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    proc = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _init_git_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _make_runtime(repo: Path, tmp_path: Path) -> ChatSessionRuntime:
    cfg = ForgeConfig()
    cfg.agent.execution_mode = "code_mode"
    cfg.subagents.enabled = True
    store = SessionStore(base_dir=tmp_path / "sessions")
    session = DevSession(
        repo_path=str(repo),
        provider=cfg.agent.default_provider,
        model=cfg.agent.default_model,
    )
    return ChatSessionRuntime(
        cfg,
        session,
        store,
        sandbox_factory=lambda _c: _DummySandbox(),
        api_key_resolver=lambda _c, _p: "test-key",
    )


@pytest.mark.asyncio
async def test_chat_context_and_subagents_apply_commands(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    rt = _make_runtime(repo, tmp_path)

    help_res = await rt.handle_slash_command("/help")
    assert "/subagents" in help_res.output
    assert "/apply <branch>" in help_res.output

    ctx_res = await rt.handle_slash_command("/context")
    assert ctx_res.payload["execution_mode"] == "code_mode"
    assert ctx_res.payload["subagents_enabled"] is True
    assert "Mode: execution=code_mode, subagents=enabled" in ctx_res.output

    empty_res = await rt.handle_slash_command("/subagents")
    assert empty_res.payload["branches"] == []
    assert "No committed subagent branches" in empty_res.output

    # Create two committed subagent branches: one clean merge, one rebaseable, one conflicting
    _git(repo, "checkout", "-b", "agent-forge/subagent/sub-aaa")
    (repo / "feature.py").write_text("ENABLED = True\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-m", "subagent feature")
    _git(repo, "checkout", "main")

    _git(repo, "checkout", "-b", "agent-forge/subagent/sub-conflict")
    (repo / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "subagent conflicting edit")
    _git(repo, "checkout", "main")
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "main diverged")

    listed = await rt.handle_slash_command("/subagents")
    branch_names = [item["branch"] for item in listed.payload["branches"]]
    assert "agent-forge/subagent/sub-aaa" in branch_names
    assert "agent-forge/subagent/sub-conflict" in branch_names

    # Prefix guard prevents merging non-subagent branches
    bad_prefix = await rt.handle_slash_command("/apply main")
    assert "Apply failed" in bad_prefix.output

    bad_flag = await rt.handle_slash_command("/apply agent-forge/subagent/sub-aaa --bogus")
    assert "Unknown flag" in bad_flag.output

    # Clean merge deletes branch & applies file to main
    applied = await rt.handle_slash_command("/apply agent-forge/subagent/sub-aaa")
    assert applied.payload["status"] == "merged"
    assert (repo / "feature.py").read_text(encoding="utf-8") == "ENABLED = True\n"
    remaining = _git(repo, "branch", "--list", "agent-forge/subagent/sub-aaa").strip()
    assert remaining == ""

    # Conflict aborts cleanly without mutating main
    conflict = await rt.handle_slash_command(
        "/apply agent-forge/subagent/sub-conflict --strategy merge"
    )
    assert conflict.payload["status"] == "conflict"
    assert "app.py" in conflict.output
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_repl_event_formatting_for_code_mode_and_has_error() -> None:
    from agent_forge.agent.chat import _attach_repl_event_handlers

    bus = EventBus()
    coord = _FakeCoordinator(statuses=[], deltas=[])
    await _attach_repl_event_handlers(bus, coord)  # type: ignore[arg-type]

    await bus.publish(
        Event(
            type=EventType.TOOL_CALLED,
            run_id="r1",
            data={
                "tool_name": "read_file",
                "arguments": {"path": "/workspace/a.py"},
                "code_mode": True,
            },
        )
    )
    await bus.publish(
        Event(
            type=EventType.APPROVAL_REQUESTED,
            run_id="r1",
            data={
                "tool_name": "run_shell",
                "reason": "Shell command deletes files",
                "code_mode": True,
            },
        )
    )
    await bus.publish(
        Event(
            type=EventType.APPROVAL_DECIDED,
            run_id="r1",
            data={"tool_name": "run_shell", "approved": False, "code_mode": True},
        )
    )
    await bus.publish(
        Event(
            type=EventType.TOOL_COMPLETED,
            run_id="r1",
            data={"tool_name": "run_shell", "duration_ms": 4, "has_error": True, "code_mode": True},
        )
    )

    assert coord.statuses == [
        "  -> [code-mode] read_file(path=/workspace/a.py)",
        "  [?] [code-mode] Approval needed for run_shell: Shell command deletes files",
        "  [denied] [code-mode] run_shell",
        "  [err] [code-mode] run_shell (exit 0, 4ms)",
    ]


def test_acp_protocol_maps_code_mode_and_subagent_events() -> None:
    assert classify_tool_kind("search_tools") == "search"
    assert classify_tool_kind("execute_code") == "execute"
    assert classify_tool_kind("spawn_subagent") == "other"

    ts = "2026-11-25T00:00:00Z"
    cm_call = map_stream_event_to_acp_updates(
        StreamEvent(
            seq=1,
            timestamp=ts,
            run_id="r1",
            type="tool.called",
            data={"tool_name": "read_file", "arguments": {"path": "x.py"}, "code_mode": True},
        ),
        "sess-1",
    )
    assert cm_call[0]["update"]["title"] == "[code-mode] read_file"

    cm_apr = map_stream_event_to_acp_updates(
        StreamEvent(
            seq=2,
            timestamp=ts,
            run_id="r1",
            type="approval.requested",
            data={
                "tool_name": "run_shell",
                "category": "file_deletion",
                "reason": "rm",
                "code_mode": True,
            },
        ),
        "sess-1",
    )
    assert cm_apr[0]["update"]["content"]["text"].startswith("[code-mode] Approval requested")

    sub_start = map_stream_event_to_acp_updates(
        StreamEvent(
            seq=3,
            timestamp=ts,
            run_id="r1",
            type="subagent.started",
            data={"subagent_id": "sub-123", "mode": "worker", "task": "Fix bug"},
        ),
        "sess-1",
    )
    assert sub_start[0]["update"]["sessionUpdate"] == "agent_thought_chunk"
    assert (
        "Subagent sub-123 started (worker): Fix bug" in (sub_start[0]["update"]["content"]["text"])
    )

    sub_done = map_stream_event_to_acp_updates(
        StreamEvent(
            seq=4,
            timestamp=ts,
            run_id="r1",
            type="subagent.completed",
            data={
                "subagent_id": "sub-123",
                "mode": "worker",
                "state": "completed",
                "total_tokens": 1250,
                "branch": "agent-forge/subagent/sub-123",
            },
        ),
        "sess-1",
    )
    text = sub_done[0]["update"]["content"]["text"]
    assert "Subagent sub-123 completed (worker, 1,250 tokens" in text
    assert "branch=agent-forge/subagent/sub-123" in text
