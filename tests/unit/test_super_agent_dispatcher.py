"""Unit tests for the Super-Agent parallel worktree dispatcher and integration."""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from agent_forge.agent.approvals import ApprovalGate, ApprovalRequest
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.config import ForgeConfig
from agent_forge.llm.base import Message, Role, TokenUsage
from agent_forge.orchestration import (
    EventBus,
    JournalEventSink,
    RunEventStream,
    SubagentTicketWorker,
    SuperAgentDispatcher,
    SuperAgentRouter,
    Ticket,
    TicketDraft,
    TicketExecutionContext,
    TicketKind,
    TicketLedger,
    TicketState,
    TicketVerificationResult,
    WorkerExecutionResult,
)
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools.base import Tool, ToolRegistry, ToolResult


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    proc = subprocess.run(  # noqa: S603
        ["git", "-c", f"core.hooksPath={os.devnull}", *args],  # noqa: S607
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return proc.stdout.strip()


def _init_repo(path: Path, initial_files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    files = initial_files or {
        "README.md": "hello\n",
        "shared.py": "LINE_1 = 'base'\nLINE_2 = 'unchanged'\n",
    }
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path


class _RecordingSandbox(Sandbox):
    """Lightweight Sandbox double tracking lifecycle and --network none isolation."""

    def __init__(self, *, exit_code: int = 0, stdout: str = "ok", stderr: str = "") -> None:
        self.started_paths: list[str] = []
        self.started_configs: list[SandboxConfig] = []
        self.exec_commands: list[str] = []
        self.stop_calls = 0
        self.alive = False
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        self.alive = True
        self.started_paths.append(repo_path)
        if config is not None:
            self.started_configs.append(config)

    async def stop(self) -> None:
        self.alive = False
        self.stop_calls += 1

    async def is_alive(self) -> bool:
        return self.alive

    async def read_file(self, path: str) -> str:
        raise FileNotFoundError(path)

    async def write_file(self, path: str, content: str) -> None:
        del path, content

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del timeout_seconds
        self.exec_commands.append(command)
        return ExecResult(exit_code=self._exit_code, stdout=self._stdout, stderr=self._stderr)


class _DummyTool(Tool):
    def __init__(self, tool_name: str) -> None:
        self._name = tool_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Dummy {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        del arguments, sandbox
        return ToolResult(output="ok", exit_code=0)


class _DummyLLM:
    def __init__(self) -> None:
        self.closed = False

    async def complete(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        self.closed = True


class TestSuperAgentDispatcher:
    """Tests for parallel execution, verification gating, integration, conflict, and cleanup."""

    @pytest.mark.asyncio
    async def test_parallel_non_overlapping_tickets_execute_concurrently_and_integrate(
        self,
        tmp_path: Path,
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt_root = tmp_path / "wts"
        ledger = TicketLedger(tmp_path / "ledger")

        async def _planner(*_args: Any) -> list[TicketDraft]:
            return [
                TicketDraft(title="Add module A", goal="Create a.py", scope_globs=["a.py"]),
                TicketDraft(title="Add module B", goal="Create b.py", scope_globs=["b.py"]),
            ]

        router = SuperAgentRouter(repo_path=repo, ledger=ledger, planner_fn=_planner)
        route = await router.route_input("Add modules A and B")
        assert len(route.admitted_ids) == 2

        barrier = asyncio.Barrier(2)
        worker_dirs: dict[str, Path] = {}
        verifier_dirs: dict[str, Path] = {}

        async def _worker(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            worker_dirs[ctx.ticket.ticket_id] = ctx.worktree_path
            await barrier.wait()
            target_file = ctx.ticket.scope_globs[0]
            (ctx.worktree_path / target_file).write_text(
                f"CREATED_BY = '{ctx.ticket.ticket_id}'\n",
                encoding="utf-8",
            )
            return WorkerExecutionResult(success=True, summary=f"Created {target_file}")

        async def _verifier(
            ticket: Ticket,
            verifier_dir: Path,
            patch: str,
            base_rev: str,
        ) -> TicketVerificationResult:
            del patch, base_rev
            verifier_dirs[ticket.ticket_id] = verifier_dir
            expected = ticket.scope_globs[0]
            assert verifier_dir != worker_dirs[ticket.ticket_id]
            assert (verifier_dir / expected).is_file()
            return TicketVerificationResult(
                passed=True, summary="clean checkout verified", reason="verified"
            )

        dispatcher = SuperAgentDispatcher(
            repo_path=repo,
            ledger=ledger,
            worker_fn=_worker,
            verifier_fn=_verifier,
            worktree_root=wt_root,
            max_concurrent_workers=2,
        )
        batch = await dispatcher.run_until_idle(parent_input_id=route.project_input.input_id)

        assert set(batch.integrated_ids) == set(route.admitted_ids)
        assert not batch.blocked_ids
        assert not batch.failed_ids
        assert (repo / "a.py").is_file()
        assert (repo / "b.py").is_file()
        assert "HEAD" not in _git(repo, "status", "--porcelain")
        assert _git(repo, "status", "--porcelain") == ""
        assert _git(repo, "branch", "--list", "agent-forge/super/*") == ""
        wt_lines = [
            ln
            for ln in _git(repo, "worktree", "list", "--porcelain").splitlines()
            if ln.startswith("worktree ")
        ]
        assert len(wt_lines) == 1

    @pytest.mark.asyncio
    async def test_dependency_ordered_execution_and_integration(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        ledger = TicketLedger(tmp_path / "ledger")

        async def _planner(*_args: Any) -> list[TicketDraft]:
            return [
                TicketDraft(title="First edit", goal="Set header", scope_globs=["shared.py"]),
                TicketDraft(title="Second edit", goal="Append footer", scope_globs=["shared.py"]),
            ]

        router = SuperAgentRouter(repo_path=repo, ledger=ledger, planner_fn=_planner)
        route = await router.route_input("Two ordered edits to shared.py")
        first_id, second_id = (t.ticket_id for t in route.tickets)
        assert first_id in route.tickets[1].depends_on

        order: list[str] = []
        observed_in_second: list[str] = []

        async def _worker(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            order.append(ctx.ticket.ticket_id)
            target = ctx.worktree_path / "shared.py"
            if ctx.ticket.ticket_id == first_id:
                target.write_text("HEADER = 'first'\n", encoding="utf-8")
            else:
                text = target.read_text(encoding="utf-8")
                observed_in_second.append(text)
                target.write_text(text + "FOOTER = 'second'\n", encoding="utf-8")
            return WorkerExecutionResult(success=True, summary="edited")

        async def _verifier(
            _ticket: Ticket,
            verifier_dir: Path,
            _patch: str,
            _base: str,
        ) -> TicketVerificationResult:
            assert (verifier_dir / "shared.py").is_file()
            return TicketVerificationResult(passed=True, summary="ok", reason="verified")

        dispatcher = SuperAgentDispatcher(
            repo_path=repo,
            ledger=ledger,
            worker_fn=_worker,
            verifier_fn=_verifier,
            worktree_root=tmp_path / "wts",
            max_concurrent_workers=2,
        )
        batch = await dispatcher.run_until_idle()

        assert order == [first_id, second_id]
        assert observed_in_second == ["HEADER = 'first'\n"]
        assert batch.integrated_ids == (first_id, second_id)
        assert (repo / "shared.py").read_text(
            encoding="utf-8"
        ) == "HEADER = 'first'\nFOOTER = 'second'\n"

    @pytest.mark.asyncio
    async def test_verification_gate_blocks_unverified_patches_and_retries(
        self,
        tmp_path: Path,
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        ledger = TicketLedger(tmp_path / "ledger")

        async def _planner(*_args: Any) -> list[TicketDraft]:
            return [
                TicketDraft(title="Retry succeeds", goal="Fix retry.py", scope_globs=["retry.py"]),
                TicketDraft(title="Always fails", goal="Fix bad.py", scope_globs=["bad.py"]),
            ]

        router = SuperAgentRouter(repo_path=repo, ledger=ledger, planner_fn=_planner)
        route = await router.route_input("Run two tickets")
        retry_id, bad_id = (t.ticket_id for t in route.tickets)
        feedback_seen: list[str] = []

        async def _worker(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            if ctx.ticket.ticket_id == retry_id:
                if ctx.attempt == 1:
                    (ctx.worktree_path / "retry.py").write_text("BROKEN = True\n", encoding="utf-8")
                else:
                    feedback_seen.append(ctx.prior_verification_feedback)
                    (ctx.worktree_path / "retry.py").write_text("GOOD = True\n", encoding="utf-8")
            else:
                (ctx.worktree_path / "bad.py").write_text(
                    "ALWAYS_BROKEN = True\n", encoding="utf-8"
                )
            return WorkerExecutionResult(success=True, summary="attempted")

        async def _verifier(
            ticket: Ticket,
            verifier_dir: Path,
            _patch: str,
            _base: str,
        ) -> TicketVerificationResult:
            if ticket.ticket_id == retry_id:
                content = (verifier_dir / "retry.py").read_text(encoding="utf-8")
                if "GOOD = True" in content:
                    return TicketVerificationResult(
                        passed=True, summary="passed", reason="verified"
                    )
                return TicketVerificationResult(
                    passed=False,
                    summary="missing GOOD=True",
                    stderr="AssertionError: expected GOOD = True",
                    reason="check_failed",
                )
            return TicketVerificationResult(
                passed=False, summary="always failing", reason="check_failed"
            )

        dispatcher = SuperAgentDispatcher(
            repo_path=repo,
            ledger=ledger,
            worker_fn=_worker,
            verifier_fn=_verifier,
            worktree_root=tmp_path / "wts",
            max_verification_retries=1,
        )
        batch = await dispatcher.run_until_idle()

        assert batch.integrated_ids == (retry_id,)
        assert batch.blocked_ids == (bad_id,)
        assert feedback_seen and "AssertionError" in feedback_seen[0]
        assert (repo / "retry.py").is_file()
        assert not (repo / "bad.py").exists()
        assert _git(repo, "branch", "--list", "agent-forge/super/*") == ""

    @pytest.mark.asyncio
    async def test_verification_fails_closed_on_missing_verifier(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        ledger = TicketLedger(tmp_path / "ledger")

        async def _planner(*_args: Any) -> list[TicketDraft]:
            return [TicketDraft(title="Unconfigured", goal="Touch app.py", scope_globs=["app.py"])]

        router = SuperAgentRouter(repo_path=repo, ledger=ledger, planner_fn=_planner)
        route = await router.route_input("Touch app.py")

        async def _worker(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            (ctx.worktree_path / "app.py").write_text("X = 1\n", encoding="utf-8")
            return WorkerExecutionResult(success=True, summary="done")

        dispatcher = SuperAgentDispatcher(
            repo_path=repo,
            ledger=ledger,
            worker_fn=_worker,
            verify_command="",
            verifier_fn=None,
            worktree_root=tmp_path / "wts",
            max_verification_retries=0,
        )
        batch = await dispatcher.run_until_idle()

        assert batch.integrated_ids == ()
        assert batch.blocked_ids == route.admitted_ids
        assert not (repo / "app.py").exists()
        assert _git(repo, "branch", "--list", "agent-forge/super/*") == ""

    @pytest.mark.asyncio
    async def test_conflict_aborts_cleanly_without_mutating_target_and_escalates(
        self,
        tmp_path: Path,
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        ledger = TicketLedger(tmp_path / "ledger")

        # Declare disjoint scope_globs so both dispatch concurrently, then collide on shared.py.
        async def _planner(*_args: Any) -> list[TicketDraft]:
            return [
                TicketDraft(title="Change one", goal="Edit shared line 1", scope_globs=["tag1/*"]),
                TicketDraft(
                    title="Change two", goal="Conflicting edit line 1", scope_globs=["tag2/*"]
                ),
            ]

        router = SuperAgentRouter(repo_path=repo, ledger=ledger, planner_fn=_planner)
        route = await router.route_input("Two conflicting changes")
        t1_id, _t2_id = (t.ticket_id for t in route.tickets)
        barrier = asyncio.Barrier(2)

        async def _worker(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            await barrier.wait()
            val = "ALPHA" if ctx.ticket.ticket_id == t1_id else "BETA"
            (ctx.worktree_path / "shared.py").write_text(
                f"LINE_1 = '{val}'\nLINE_2 = 'unchanged'\n",
                encoding="utf-8",
            )
            return WorkerExecutionResult(success=True, summary=f"wrote {val}")

        async def _verifier(
            _ticket: Ticket,
            verifier_dir: Path,
            _patch: str,
            _base: str,
        ) -> TicketVerificationResult:
            assert (verifier_dir / "shared.py").is_file()
            return TicketVerificationResult(passed=True, summary="verified", reason="verified")

        dispatcher = SuperAgentDispatcher(
            repo_path=repo,
            ledger=ledger,
            worker_fn=_worker,
            verifier_fn=_verifier,
            worktree_root=tmp_path / "wts",
            max_concurrent_workers=2,
            max_conflict_remediations=0,
        )
        batch = await dispatcher.run_until_idle()

        assert len(batch.integrated_ids) == 1
        assert len(batch.blocked_ids) == 1
        winner = "ALPHA" if batch.integrated_ids[0] == t1_id else "BETA"
        blocked_ticket = ledger.get_ticket(batch.blocked_ids[0])
        assert blocked_ticket.state == TicketState.BLOCKED_ON_HUMAN
        assert "shared.py" in blocked_ticket.metadata["conflict_files"]
        assert any(
            it.kind == "conflict" and blocked_ticket.ticket_id in it.ticket_ids
            for it in batch.inbox_items
        )
        assert (repo / "shared.py").read_text(
            encoding="utf-8"
        ) == f"LINE_1 = '{winner}'\nLINE_2 = 'unchanged'\n"
        assert _git(repo, "status", "--porcelain") == ""
        assert _git(repo, "branch", "--list", "agent-forge/super/*") == ""

    @pytest.mark.asyncio
    async def test_conflict_bounded_remediation_succeeds_after_independent_verification(
        self,
        tmp_path: Path,
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        ledger = TicketLedger(tmp_path / "ledger")

        async def _planner(*_args: Any) -> list[TicketDraft]:
            return [
                TicketDraft(title="First edit", goal="Edit shared line 1", scope_globs=["tag1/*"]),
                TicketDraft(
                    title="Second edit", goal="Conflict then remediate", scope_globs=["tag2/*"]
                ),
            ]

        router = SuperAgentRouter(repo_path=repo, ledger=ledger, planner_fn=_planner)
        route = await router.route_input("Remediate conflict cleanly")
        t1_id, t2_id = (t.ticket_id for t in route.tickets)
        barrier = asyncio.Barrier(2)

        async def _worker(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            await barrier.wait()
            val = "FIRST" if ctx.ticket.ticket_id == t1_id else "SECOND"
            (ctx.worktree_path / "shared.py").write_text(
                f"LINE_1 = '{val}'\nLINE_2 = 'unchanged'\n",
                encoding="utf-8",
            )
            return WorkerExecutionResult(success=True, summary=val)

        remediation_calls = 0

        async def _remediator(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            nonlocal remediation_calls
            remediation_calls += 1
            assert ctx.conflict_context is not None
            assert "shared.py" in ctx.conflict_context.conflicted_files
            current = (ctx.worktree_path / "shared.py").read_text(encoding="utf-8")
            (ctx.worktree_path / "shared.py").write_text(
                current + "REMEDIATED = True\n",
                encoding="utf-8",
            )
            return WorkerExecutionResult(success=True, summary="remediated cleanly")

        async def _verifier(
            _ticket: Ticket,
            verifier_dir: Path,
            _patch: str,
            _base: str,
        ) -> TicketVerificationResult:
            assert (verifier_dir / "shared.py").is_file()
            return TicketVerificationResult(passed=True, summary="ok", reason="verified")

        dispatcher = SuperAgentDispatcher(
            repo_path=repo,
            ledger=ledger,
            worker_fn=_worker,
            verifier_fn=_verifier,
            remediator_fn=_remediator,
            worktree_root=tmp_path / "wts",
            max_concurrent_workers=2,
            max_conflict_remediations=1,
        )
        batch = await dispatcher.run_until_idle()

        assert remediation_calls == 1
        assert set(batch.integrated_ids) == {t1_id, t2_id}
        assert "REMEDIATED = True" in (repo / "shared.py").read_text(encoding="utf-8")
        assert _git(repo, "branch", "--list", "agent-forge/super/*") == ""

    @pytest.mark.asyncio
    async def test_cancellation_cleans_up_worktrees_branches_and_sandboxes_synchronously(
        self,
        tmp_path: Path,
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt_root = tmp_path / "wts"
        ledger = TicketLedger(tmp_path / "ledger")

        async def _planner(*_args: Any) -> list[TicketDraft]:
            return [
                TicketDraft(title="Long 1", goal="Edit a.py", scope_globs=["a.py"]),
                TicketDraft(title="Long 2", goal="Edit b.py", scope_globs=["b.py"]),
            ]

        router = SuperAgentRouter(repo_path=repo, ledger=ledger, planner_fn=_planner)
        await router.route_input("Long tasks")

        sandboxes: list[_RecordingSandbox] = []
        both_started = asyncio.Event()

        def _sb_factory(_cfg: SandboxConfig) -> Sandbox:
            sb = _RecordingSandbox()
            sandboxes.append(sb)
            return sb

        async def _worker(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            sb = _sb_factory(SandboxConfig())
            await sb.start(str(ctx.worktree_path), SandboxConfig())
            if len(sandboxes) == 2:
                both_started.set()
            try:
                await asyncio.sleep(30)
            finally:
                await sb.stop()
            return WorkerExecutionResult(success=True)

        dispatcher = SuperAgentDispatcher(
            repo_path=repo,
            ledger=ledger,
            worker_fn=_worker,
            worktree_root=wt_root,
            max_concurrent_workers=2,
        )
        run_task = asyncio.create_task(dispatcher.run_until_idle())
        await both_started.wait()
        run_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await run_task

        wt_lines = [
            ln
            for ln in _git(repo, "worktree", "list", "--porcelain").splitlines()
            if ln.startswith("worktree ")
        ]
        assert len(wt_lines) == 1
        assert _git(repo, "branch", "--list", "agent-forge/super/*") == ""
        assert len(sandboxes) == 2
        assert all(sb.stop_calls >= 1 for sb in sandboxes)

    @pytest.mark.asyncio
    async def test_subagent_ticket_worker_reuses_dev_session_approval_gate_and_network_none(
        self,
        tmp_path: Path,
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        sandboxes: list[_RecordingSandbox] = []
        llms: list[_DummyLLM] = []
        seen_gate: list[ApprovalGate | None] = []
        seen_tool_names: list[set[str]] = []

        async def _fake_react_loop(
            run: AgentRun,
            _llm: Any,
            tools: ToolRegistry,
            _sandbox: Sandbox,
            **kwargs: Any,
        ) -> AgentRun:
            seen_gate.append(kwargs.get("approval_gate"))
            seen_tool_names.append({d.name for d in tools.list_definitions()})
            run.iterations = 2
            run.total_tokens = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
            run.messages.append(Message(role=Role.ASSISTANT, content="Implemented safely."))
            run.state = RunState.COMPLETED
            return run

        registry = ToolRegistry()
        for name in (
            "read_file",
            "apply_patch",
            "run_shell",
            "git_push",
            "create_pr",
            "spawn_subagent",
        ):
            registry.register(_DummyTool(name))

        def _sb_factory(_cfg: SandboxConfig) -> Sandbox:
            sb = _RecordingSandbox()
            sandboxes.append(sb)
            return sb

        def _llm_factory() -> Any:
            llm = _DummyLLM()
            llms.append(llm)
            return llm

        async def _approve_all(_req: ApprovalRequest) -> bool:
            return True

        gate = ApprovalGate(mode="prompt", callback=_approve_all)
        worker = SubagentTicketWorker(
            parent_config=AgentConfig(max_iterations=20, max_tokens_per_run=100_000),
            sandbox_config=SandboxConfig(network_enabled=True, writable_cache_mounts=True),
            llm_factory=_llm_factory,
            sandbox_factory=_sb_factory,
            parent_tools=registry,
            react_loop_fn=_fake_react_loop,
        )
        ctx = TicketExecutionContext(
            ticket=Ticket(
                parent_input_id="inp-1",
                title="Implement parser",
                goal="Update parser logic",
                kind=TicketKind.IMPLEMENT,
                scope_globs=["parser.py"],
            ),
            worktree_path=repo,
            branch="agent-forge/super/tkt-1",
            base_revision="HEAD",
            attempt=1,
            approval_gate=gate,
        )

        result = await worker(ctx)

        assert result.success is True
        assert result.session_id
        assert result.tokens_used == 15
        assert seen_gate == [gate]
        assert {"read_file", "apply_patch", "run_shell"}.issubset(seen_tool_names[0])
        assert {"git_push", "create_pr", "spawn_subagent"}.isdisjoint(seen_tool_names[0])
        assert sandboxes[0].started_configs[0].network_enabled is False
        assert sandboxes[0].started_configs[0].writable_cache_mounts is False
        assert sandboxes[0].stop_calls == 1
        assert llms[0].closed is True

    @pytest.mark.asyncio
    async def test_structured_lifecycle_events_stream_to_run_event_stream(
        self,
        tmp_path: Path,
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        ledger = TicketLedger(tmp_path / "ledger")
        bus = EventBus()
        runs_dir = tmp_path / "runs"
        sink = JournalEventSink(base_dir=runs_dir)
        await sink.attach(bus)

        async def _planner(*_args: Any) -> list[TicketDraft]:
            return [TicketDraft(title="Add x", goal="Write x.py", scope_globs=["x.py"])]

        router = SuperAgentRouter(repo_path=repo, ledger=ledger, planner_fn=_planner, event_bus=bus)
        route = await router.route_input("Create x.py")

        async def _worker(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            (ctx.worktree_path / "x.py").write_text("X = 1\n", encoding="utf-8")
            return WorkerExecutionResult(success=True, summary="wrote x.py")

        sb = _RecordingSandbox(exit_code=0, stdout="1 passed")
        dispatcher = SuperAgentDispatcher(
            repo_path=repo,
            ledger=ledger,
            worker_fn=_worker,
            event_bus=bus,
            verify_command="pytest -q",
            sandbox_factory=lambda _cfg: sb,
            worktree_root=tmp_path / "wts",
        )
        await dispatcher.run_until_idle(parent_input_id=route.project_input.input_id)
        await sink.detach(bus)

        page = RunEventStream(route.project_input.input_id, base_dir=runs_dir).read_page()
        event_types = [ev.type for ev in page.events]
        assert event_types[0] == "super.input.received"
        assert "super.ticket.dispatched" in event_types
        assert "super.ticket.verified" in event_types
        assert "super.ticket.integrated" in event_types
        assert event_types[-1] == "super.run.completed"
        assert page.terminal is True
        assert sb.exec_commands == ["pytest -q"]
        assert sb.stop_calls == 1

    @pytest.mark.asyncio
    async def test_auto_integrate_false_emits_review_bundle_and_manual_integrate_succeeds(
        self,
        tmp_path: Path,
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        ledger = TicketLedger(tmp_path / "ledger")

        async def _planner(*_args: Any) -> list[TicketDraft]:
            return [TicketDraft(title="Staged", goal="Add staged.py", scope_globs=["staged.py"])]

        router = SuperAgentRouter(repo_path=repo, ledger=ledger, planner_fn=_planner)
        route = await router.route_input("Stage verified patch")

        async def _worker(ctx: TicketExecutionContext) -> WorkerExecutionResult:
            (ctx.worktree_path / "staged.py").write_text("READY = True\n", encoding="utf-8")
            return WorkerExecutionResult(success=True, summary="staged")

        async def _verifier(
            _ticket: Ticket,
            _verifier_dir: Path,
            _patch: str,
            _base: str,
        ) -> TicketVerificationResult:
            return TicketVerificationResult(passed=True, summary="checks green", reason="verified")

        dispatcher = SuperAgentDispatcher(
            repo_path=repo,
            ledger=ledger,
            worker_fn=_worker,
            verifier_fn=_verifier,
            worktree_root=tmp_path / "wts",
            auto_integrate=False,
        )
        batch = await dispatcher.run_until_idle(parent_input_id=route.project_input.input_id)

        assert batch.ready_for_review_ids == route.admitted_ids
        assert batch.integrated_ids == ()
        assert not (repo / "staged.py").exists()
        assert any(it.kind == "review_bundle" for it in batch.inbox_items)

        int_outcomes = await dispatcher.integrate_ready_tickets(
            parent_input_id=route.project_input.input_id
        )
        assert len(int_outcomes) == 1
        assert int_outcomes[0].ticket.state == TicketState.INTEGRATED
        assert (repo / "staged.py").is_file()

    def test_config_phase2_defaults(self) -> None:
        cfg = ForgeConfig()
        assert cfg.super_agent.max_verification_retries == 1
        assert cfg.super_agent.max_conflict_remediations == 1
        assert cfg.super_agent.merge_strategy == "merge"
        assert cfg.super_agent.worktree_branch_prefix == "agent-forge/super"
        assert cfg.super_agent.auto_integrate is True
        assert cfg.super_agent.approval_mode == "prompt"
