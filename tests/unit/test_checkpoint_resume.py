"""Unit tests for safe local checkpointing and run resumption (#154)."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from agent_forge.agent.checkpoint import load_checkpoint, save_checkpoint
from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.state import transition
from agent_forge.agent.verification import VerificationWorkspace
from agent_forge.cli import main as cli_main
from agent_forge.llm.base import LLMResponse, Message, Role, TokenUsage, ToolCall
from agent_forge.orchestration.events import Event, EventBus, EventType
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools import ToolRegistry, WriteFileTool


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class _WorkspaceSandbox(Sandbox):
    """Host-backed Sandbox double rooted at the active isolated checkout."""

    def __init__(self, *, fail_verify: bool = False) -> None:
        self._root: Path | None = None
        self._alive = False
        self.fail_verify = fail_verify

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del config
        self._root = Path(repo_path)
        self._alive = True

    async def stop(self) -> None:
        self._alive = False

    async def is_alive(self) -> bool:
        return self._alive

    def _resolve(self, path: str) -> Path:
        assert self._root is not None
        rel = path.removeprefix("/workspace").lstrip("/")
        return self._root / rel

    async def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.exists():
            raise FileNotFoundError(path)
        return target.read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del timeout_seconds
        assert self._root is not None
        if command.startswith("mkdir -p "):
            raw_dir = command.removeprefix("mkdir -p ").strip()
            self._resolve(raw_dir).mkdir(parents=True, exist_ok=True)
            return ExecResult(exit_code=0, stdout="", stderr="")
        if command == "check-both":
            a_ok = (self._root / "a.txt").read_text(encoding="utf-8") == "edited a\n"
            b_ok = (self._root / "b.txt").read_text(encoding="utf-8") == "edited b\n"
            code = 0 if (a_ok and b_ok and not self.fail_verify) else 1
            return ExecResult(exit_code=code, stdout="ok" if code == 0 else "fail", stderr="")
        return ExecResult(exit_code=1, stdout="", stderr=f"Unexpected command: {command}")


def _make_git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "a.txt").write_text("base a\n", encoding="utf-8")
    (repo / "b.txt").write_text("base b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base_rev = _git(repo, "rev-parse", "HEAD")
    return repo, base_rev


class TestCheckpointPersistence:
    """Verify save_checkpoint / load_checkpoint and VerificationWorkspace restoration."""

    def test_checkpoint_round_trip(self, tmp_path: Path) -> None:
        run = AgentRun(
            task="Fix two files",
            repo_path="/tmp/repo",
            config=AgentConfig(),
            state=RunState.PAUSED,
            iterations=1,
            messages=[
                Message(role=Role.TOOL, content="wrote a", tool_call_id="call-a"),
            ],
        )
        save_checkpoint(
            run,
            base_revision="abc123",
            partial_patch="diff --git a/a.txt b/a.txt\n",
            verify_command="pytest -q",
            base_dir=tmp_path,
        )

        loaded_run, checkpoint = load_checkpoint(run.id, base_dir=tmp_path)
        assert loaded_run.id == run.id
        assert loaded_run.state == RunState.PAUSED
        assert checkpoint.base_revision == "abc123"
        assert checkpoint.iteration == 1
        assert checkpoint.verify_command == "pytest -q"
        assert checkpoint.partial_patch == "diff --git a/a.txt b/a.txt\n"
        assert checkpoint.completed_tool_call_ids == ["call-a"]

    def test_workspace_restores_pinned_revision_and_partial_patch_with_dirty_head(
        self,
        tmp_path: Path,
    ) -> None:
        repo, base_rev = _make_git_repo(tmp_path)
        patch = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1 +1 @@\n"
            "-base a\n"
            "+edited a\n"
        )
        # Advance HEAD and leave dirty local edits in the original repo.
        (repo / "c.txt").write_text("newer commit\n", encoding="utf-8")
        _git(repo, "add", "c.txt")
        _git(repo, "commit", "-m", "newer")
        (repo / "a.txt").write_text("user local edit\n", encoding="utf-8")

        with VerificationWorkspace(
            str(repo),
            "check-both",
            base_revision=base_rev,
            initial_patch=patch,
        ) as ws:
            assert ws.base_revision == base_rev
            assert (ws.workspace / "a.txt").read_text(encoding="utf-8") == "edited a\n"
            assert not (ws.workspace / "c.txt").exists()
            assert "+edited a" in ws.capture_patch()

        assert (repo / "a.txt").read_text(encoding="utf-8") == "user local edit\n"


class TestCheckpointResumeLifecycle:
    """Verify state transitions, cancellation checkpointing, and cross-process resume."""

    def test_paused_state_transitions(self) -> None:
        run = AgentRun(task="t", repo_path="/tmp", config=AgentConfig())
        transition(run, RunState.RUNNING)
        transition(run, RunState.PAUSED)
        assert run.state == RunState.PAUSED
        transition(run, RunState.RUNNING)
        transition(run, RunState.PAUSED)
        transition(run, RunState.CANCELLED)
        assert run.state == RunState.CANCELLED

    @pytest.mark.asyncio
    async def test_cancellation_saves_paused_checkpoint_and_emits_event(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary",
            lambda *_args: tmp_path / "summary.json",
        )
        checkpoint_calls: list[RunState] = []

        async def _on_checkpoint(run: AgentRun) -> None:
            checkpoint_calls.append(run.state)
            save_checkpoint(
                run,
                base_revision="rev1",
                partial_patch="patch1",
                base_dir=tmp_path / "runs",
            )

        entered = asyncio.Event()
        llm = AsyncMock()

        async def _hang(**_kwargs: object) -> LLMResponse:
            entered.set()
            await asyncio.Future()
            raise AssertionError  # pragma: no cover

        llm.complete.side_effect = _hang
        bus = EventBus()
        paused_events: list[Event] = []

        async def _capture(event: Event) -> None:
            paused_events.append(event)

        await bus.subscribe(EventType.RUN_PAUSED, _capture)

        run = AgentRun(task="Interruptible", repo_path="/tmp", config=AgentConfig())
        task = asyncio.create_task(
            react_loop(
                run,
                llm,
                ToolRegistry(),
                _WorkspaceSandbox(),
                event_bus=bus,
                on_checkpoint=_on_checkpoint,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert checkpoint_calls == [RunState.PAUSED]
        assert len(paused_events) == 1
        reloaded, cp = load_checkpoint(run.id, base_dir=tmp_path / "runs")
        assert reloaded.state == RunState.PAUSED
        assert cp.partial_patch == "patch1"

    @pytest.mark.asyncio
    async def test_simulated_process_restart_resumes_without_repeating_edit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runs_dir = tmp_path / "runs"
        from agent_forge.agent.persistence import save_run

        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary",
            lambda *_args: tmp_path / "summary.json",
        )

        repo, base_rev = _make_git_repo(tmp_path)
        registry = ToolRegistry()
        write_tool = WriteFileTool()
        seen_paths: list[str] = []
        orig_execute = write_tool.execute

        async def _recording_write(arguments: dict[str, object], sandbox: Sandbox):
            seen_paths.append(str(arguments.get("path", "")))
            return await orig_execute(arguments, sandbox)

        write_tool.execute = _recording_write  # type: ignore[method-assign]
        registry.register(write_tool)

        # Phase 1: run edits a.txt in iter 1, checkpoints, and pauses due to max_iterations=1.
        llm_phase1 = AsyncMock()
        llm_phase1.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-edit-a",
                        name="write_file",
                        arguments={"path": "a.txt", "content": "edited a\n"},
                    )
                ],
                usage=TokenUsage(10, 10, 20),
                finish_reason="tool_calls",
            ),
        ]
        run1 = AgentRun(
            task="Edit a.txt and b.txt",
            repo_path=str(repo),
            config=AgentConfig(max_iterations=1),
        )
        with VerificationWorkspace(str(repo), "check-both") as ws1:
            sb1 = _WorkspaceSandbox()
            await sb1.start(str(ws1.workspace))

            async def _cp1(r: AgentRun) -> None:
                save_checkpoint(
                    r,
                    base_revision=ws1.base_revision,
                    partial_patch=ws1.capture_patch(),
                    verify_command="check-both",
                    base_dir=runs_dir,
                )

            res1 = await react_loop(run1, llm_phase1, registry, sb1, on_checkpoint=_cp1)
            assert res1.state == RunState.TIMEOUT
            # Mark paused as if interrupted after checkpoint 1.
            res1.state = RunState.PAUSED
            save_checkpoint(
                res1,
                base_revision=ws1.base_revision,
                partial_patch=ws1.capture_patch(),
                verify_command="check-both",
                base_dir=runs_dir,
            )

        assert seen_paths == ["a.txt"]

        # Phase 2: fresh process loads checkpoint from disk, skips call-edit-a, edits b.txt.
        loaded_run, cp = load_checkpoint(run1.id, base_dir=runs_dir)
        loaded_run.config.max_iterations = 4
        llm_phase2 = AsyncMock()
        llm_phase2.complete.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-edit-b",
                        name="write_file",
                        arguments={"path": "b.txt", "content": "edited b\n"},
                    )
                ],
                usage=TokenUsage(10, 10, 20),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Completed both edits.",
                tool_calls=[],
                usage=TokenUsage(10, 10, 20),
                finish_reason="stop",
            ),
        ]

        with VerificationWorkspace(
            str(repo),
            cp.verify_command or "check-both",
            base_revision=cp.base_revision,
            initial_patch=cp.partial_patch,
        ) as ws2:
            sb2 = _WorkspaceSandbox()
            await sb2.start(str(ws2.workspace))

            async def _verify():
                return await ws2.verify(SandboxConfig(backend="bwrap"), sb2)

            with patch("agent_forge.agent.verification.create_sandbox", return_value=sb2):
                res2 = await react_loop(loaded_run, llm_phase2, registry, sb2, verify=_verify)

        assert res2.state == RunState.COMPLETED
        assert res2.error is None
        assert res2.verification is not None
        assert res2.verification.passed is True
        assert res2.verification.base_revision == base_rev
        assert "+edited a" in res2.verification.patch
        assert "+edited b" in res2.verification.patch
        assert seen_paths == ["a.txt", "b.txt"]


def test_cli_resume_command_json_and_completed_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr("agent_forge.cli.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "agent_forge.observability.cost.save_summary",
        lambda *_args: tmp_path / "summary.json",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    repo, base_rev = _make_git_repo(tmp_path)
    paused_run = AgentRun(
        task="Resume via CLI",
        repo_path=str(repo),
        config=AgentConfig(max_iterations=1),
        state=RunState.PAUSED,
        iterations=1,
        messages=[
            Message(role=Role.SYSTEM, content="sys"),
            Message(role=Role.USER, content="Resume via CLI"),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write_file",
                        arguments={"path": "a.txt", "content": "edited a\n"},
                    )
                ],
            ),
            Message(role=Role.TOOL, content="wrote a", tool_call_id="c1"),
        ],
    )
    partial = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-base a\n+edited a\n"
    )
    save_checkpoint(
        paused_run,
        base_revision=base_rev,
        partial_patch=partial,
        verify_command="check-both",
        base_dir=runs_dir,
    )

    llm = AsyncMock()
    llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="c2",
                    name="write_file",
                    arguments={"path": "b.txt", "content": "edited b\n"},
                )
            ],
            usage=TokenUsage(10, 10, 20),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content="Done.",
            tool_calls=[],
            usage=TokenUsage(10, 10, 20),
            finish_reason="stop",
        ),
    ]
    llm.close = AsyncMock()
    sandbox = _WorkspaceSandbox()
    runner_options = (
        {"mix_stderr": False} if "mix_stderr" in inspect.signature(CliRunner).parameters else {}
    )
    runner = CliRunner(**runner_options)
    with (
        patch("agent_forge.sandbox.factory.create_sandbox", return_value=sandbox),
        patch("agent_forge.agent.verification.create_sandbox", return_value=sandbox),
        patch("agent_forge.cli.create_provider", return_value=llm),
    ):
        res = runner.invoke(
            cli_main,
            ["resume", paused_run.id, "--max-iterations", "4", "--output-format", "json"],
        )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["run_id"] == paused_run.id
    assert payload["state"] == "completed"
    assert payload["verification"]["reason"] == "verified"

    # Re-resuming a COMPLETED run is rejected.
    rejected = runner.invoke(cli_main, ["resume", paused_run.id])
    assert rejected.exit_code == 1
    assert "cannot resume" in " ".join(rejected.stderr.split())
