"""Unit tests for Git worktree isolation, lifecycle cleanup, and merge integration."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.cli import main
from agent_forge.config import load_config
from agent_forge.orchestration import (
    Event,
    EventBus,
    EventType,
    InMemoryQueue,
    Task,
    TaskStatus,
    Worker,
    WorktreeError,
    WorktreeManager,
    WorktreeState,
    is_git_repo,
)


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
    files = initial_files or {"README.md": "hello\n", "app.py": "VALUE = 1\n"}
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path


class TestWorktreeBasics:
    """Repository detection, creation guards, and diff inspection."""

    def test_is_git_repo(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        plain = tmp_path / "plain"
        plain.mkdir()
        assert is_git_repo(repo) is True
        assert is_git_repo(plain) is False
        assert is_git_repo(tmp_path / "missing") is False

    @pytest.mark.asyncio
    async def test_rejects_non_git_or_empty_repo_and_duplicate_lease(
        self,
        tmp_path: Path,
    ) -> None:
        non_git = tmp_path / "not-a-repo"
        non_git.mkdir()
        with pytest.raises(WorktreeError):
            WorktreeManager(non_git)

        empty_repo = tmp_path / "empty"
        empty_repo.mkdir()
        _git(empty_repo, "init")
        mgr_empty = WorktreeManager(empty_repo, worktrees_root=tmp_path / "wt-empty")
        with pytest.raises(WorktreeError):
            await mgr_empty.create("run-empty")

        repo = _init_repo(tmp_path / "repo")
        wt_root = tmp_path / "worktrees"
        mgr = WorktreeManager(repo, worktrees_root=wt_root)
        lease = await mgr.create("run-1")
        assert lease.worktree_path.is_dir()
        assert lease.state == WorktreeState.ACTIVE
        with pytest.raises(WorktreeError, match="already active"):
            await mgr.create("run-1")
        await mgr.finalize("run-1")
        assert not lease.worktree_path.exists()

    @pytest.mark.asyncio
    async def test_has_changes_and_capture_diff(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        mgr = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
        lease = await mgr.create("run-diff")

        assert await mgr.has_changes(lease) is False
        assert await mgr.capture_diff(lease) == ""

        (lease.worktree_path / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
        (lease.worktree_path / "notes.txt").write_text("new file\n", encoding="utf-8")
        assert await mgr.has_changes(lease) is True

        diff = await mgr.capture_diff(lease)
        assert "+VALUE = 42" in diff
        assert "notes.txt" in diff
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"

        await mgr.finalize("run-diff")
        assert lease.state == WorktreeState.COMMITTED
        assert lease.commit_sha is not None
        assert not lease.worktree_path.exists()


class TestWorktreeLeaseLifecycle:
    """Automatic cleanup of unchanged, failed, and cancelled runs."""

    @pytest.mark.asyncio
    async def test_unchanged_lease_auto_removed(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt_root = tmp_path / "wts"
        mgr = WorktreeManager(repo, worktrees_root=wt_root)

        async with mgr.lease("noop-run") as lease:
            assert lease.worktree_path.is_dir()

        assert lease.state == WorktreeState.REMOVED
        assert lease.has_changes is False
        assert not lease.worktree_path.exists()
        assert list(wt_root.iterdir()) == []
        assert _git(repo, "branch", "--list", lease.branch) == ""

    @pytest.mark.asyncio
    async def test_exception_aborts_and_leaves_no_orphan(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt_root = tmp_path / "wts"
        mgr = WorktreeManager(repo, worktrees_root=wt_root)
        captured: list[object] = []

        async def _failing_run() -> None:
            async with mgr.lease("err-run") as lease:
                captured.append(lease)
                (lease.worktree_path / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
                msg = "boom"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="boom"):
            await _failing_run()

        lease = mgr.get_lease("err-run")
        assert lease is not None
        assert lease.state == WorktreeState.REMOVED
        assert not lease.worktree_path.exists()
        assert _git(repo, "branch", "--list", lease.branch) == ""
        listed = await mgr.list_worktrees()
        assert [item.path for item in listed] == [repo.resolve()]
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    @pytest.mark.asyncio
    async def test_cancelled_task_aborts_worktree_synchronously(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt_root = tmp_path / "wts"
        mgr = WorktreeManager(repo, worktrees_root=wt_root)
        entered = asyncio.Event()
        holder: dict[str, Path] = {}

        async def _worker() -> None:
            async with mgr.lease("cancel-run") as lease:
                holder["path"] = lease.worktree_path
                (lease.worktree_path / "dirty.txt").write_text("partial", encoding="utf-8")
                entered.set()
                await asyncio.sleep(10)

        bg = asyncio.create_task(_worker())
        await entered.wait()
        assert holder["path"].exists()
        bg.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bg

        assert not holder["path"].exists()
        listed = await mgr.list_worktrees()
        assert [item.path for item in listed] == [repo.resolve()]
        assert _git(repo, "for-each-ref", "refs/heads/agent-forge/run/") == ""

    @pytest.mark.asyncio
    async def test_cleanup_orphans_removes_stale_but_keeps_active(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt_root = tmp_path / "wts"
        mgr = WorktreeManager(repo, worktrees_root=wt_root)

        active = await mgr.create("active-run")
        stale_path = wt_root / "stale-manual"
        _git(repo, "worktree", "add", "-b", "agent-forge/run/stale-manual", str(stale_path), "HEAD")
        leftover_dir = wt_root / "leftover-dir"
        leftover_dir.mkdir(parents=True)
        (leftover_dir / "temp.txt").write_text("x", encoding="utf-8")
        _git(repo, "branch", "agent-forge/run/dangling-branch", "HEAD")

        cleaned = await mgr.cleanup_orphans()
        assert str(stale_path.resolve()) in cleaned
        assert str(leftover_dir.resolve()) in cleaned
        assert "branch:agent-forge/run/dangling-branch" in cleaned
        assert active.worktree_path.is_dir()

        await mgr.remove("active-run")
        assert not active.worktree_path.exists()


class TestWorktreeIntegration:
    """Merge-time integration via git merge and rebase, including conflicts."""

    @pytest.mark.asyncio
    async def test_merge_and_rebase_strategies_integrate_cleanly(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo", {"a.txt": "base-a\n", "b.txt": "base-b\n"})
        mgr = WorktreeManager(repo, worktrees_root=tmp_path / "wts")

        async with mgr.lease("run-a") as lease_a:
            (lease_a.worktree_path / "a.txt").write_text("updated-a\n", encoding="utf-8")
        async with mgr.lease("run-b") as lease_b:
            (lease_b.worktree_path / "b.txt").write_text("updated-b\n", encoding="utf-8")

        res_a = await mgr.integrate("run-a", strategy="merge")
        res_b = await mgr.integrate("run-b", strategy="rebase")

        assert res_a.status == "merged"
        assert res_b.status == "merged"
        assert (repo / "a.txt").read_text(encoding="utf-8") == "updated-a\n"
        assert (repo / "b.txt").read_text(encoding="utf-8") == "updated-b\n"
        assert lease_a.state == WorktreeState.INTEGRATED
        assert lease_b.state == WorktreeState.INTEGRATED
        assert _git(repo, "for-each-ref", "refs/heads/agent-forge/run/") == ""

    @pytest.mark.asyncio
    async def test_merge_and_rebase_conflicts_abort_cleanly(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo", {"shared.txt": "line1\n"})
        mgr = WorktreeManager(repo, worktrees_root=tmp_path / "wts")

        async with mgr.lease("run-1") as l1:
            (l1.worktree_path / "shared.txt").write_text("from-run-1\n", encoding="utf-8")
        async with mgr.lease("run-2") as l2:
            (l2.worktree_path / "shared.txt").write_text("from-run-2\n", encoding="utf-8")

        first = await mgr.integrate("run-1", strategy="merge")
        assert first.status == "merged"

        conflict_merge = await mgr.integrate("run-2", strategy="merge")
        assert conflict_merge.status == "conflict"
        assert conflict_merge.conflicted_files == ["shared.txt"]
        assert l2.state == WorktreeState.CONFLICT
        assert _git(repo, "status", "--porcelain") == ""
        assert (repo / "shared.txt").read_text(encoding="utf-8") == "from-run-1\n"

        conflict_rebase = await mgr.integrate("run-2", strategy="rebase")
        assert conflict_rebase.status == "conflict"
        assert conflict_rebase.conflicted_files == ["shared.txt"]
        assert _git(repo, "status", "--porcelain") == ""
        assert _git(repo, "branch", "--list", l2.branch) != ""

    @pytest.mark.asyncio
    async def test_integrate_dirty_primary_returns_error(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo", {"a.txt": "1\n"})
        mgr = WorktreeManager(repo, worktrees_root=tmp_path / "wts")
        async with mgr.lease("run-1") as lease:
            (lease.worktree_path / "a.txt").write_text("2\n", encoding="utf-8")

        (repo / "dirty.txt").write_text("uncommitted", encoding="utf-8")
        res = await mgr.integrate("run-1")
        assert res.status == "error"
        assert "uncommitted" in res.detail


class TestWorkerWorktreeExecution:
    """Concurrent Worker execution with per-run worktree isolation."""

    @pytest.mark.asyncio
    async def test_worker_runs_concurrently_without_cross_contamination(
        self,
        tmp_path: Path,
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt_root = tmp_path / "wts"
        queue = InMemoryQueue()
        bus = EventBus()
        events: list[Event] = []
        active = 0
        peak = 0
        seen_paths: dict[str, Path] = {}

        async def _record(event: Event) -> None:
            events.append(event)

        await bus.subscribe(EventType.WORKTREE_CREATED, _record)
        await bus.subscribe(EventType.WORKTREE_CLEANED, _record)

        async def runner(task: Task) -> AgentRun:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                wt_path = Path(task.repo_path)
                seen_paths[task.id] = wt_path
                await asyncio.sleep(0.05)
                if task.id != "task-noop":
                    (wt_path / f"{task.id}.txt").write_text(
                        f"content-{task.id}\n",
                        encoding="utf-8",
                    )
                return AgentRun(
                    id=task.id,
                    task=task.task_description,
                    repo_path=task.repo_path,
                    config=AgentConfig(),
                    state=RunState.COMPLETED,
                )
            finally:
                active -= 1

        worker = Worker(
            queue,
            bus,
            runner,
            poll_interval=0.01,
            max_concurrent_tasks=4,
            worktree_isolation=True,
            worktree_root=wt_root,
        )
        for tid in ("task-1", "task-2", "task-3", "task-noop"):
            await queue.enqueue(
                Task(id=tid, task_description=tid, repo_path=str(repo), config=AgentConfig())
            )

        await worker.start()
        for _ in range(100):
            statuses = [
                await queue.get_status(t) for t in ("task-1", "task-2", "task-3", "task-noop")
            ]
            if all(s == TaskStatus.COMPLETED for s in statuses):
                break
            await asyncio.sleep(0.02)
        await worker.stop()

        assert peak >= 3
        assert len(set(seen_paths.values())) == 4
        assert all(p != repo.resolve() for p in seen_paths.values())
        assert not any(p.exists() for p in seen_paths.values())

        manager = worker.get_worktree_manager(repo)
        assert manager is not None
        assert manager.get_lease("task-noop") is not None
        assert manager.get_lease("task-noop").state == WorktreeState.REMOVED  # type: ignore[union-attr]

        results = await manager.integrate_all()
        assert [r.status for r in results] == ["merged", "merged", "merged"]
        for tid in ("task-1", "task-2", "task-3"):
            assert (repo / f"{tid}.txt").read_text(encoding="utf-8") == f"content-{tid}\n"
        assert _git(repo, "for-each-ref", "refs/heads/agent-forge/run/") == ""
        assert len([e for e in events if e.type == EventType.WORKTREE_CREATED]) == 4
        assert len([e for e in events if e.type == EventType.WORKTREE_CLEANED]) == 4

    @pytest.mark.asyncio
    async def test_worker_stop_aborts_inflight_worktrees_cleanly(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt_root = tmp_path / "wts"
        queue = InMemoryQueue()
        bus = EventBus()
        started = asyncio.Event()
        entered_count = 0

        async def slow_runner(task: Task) -> AgentRun:
            nonlocal entered_count
            (Path(task.repo_path) / f"{task.id}.txt").write_text("partial\n", encoding="utf-8")
            entered_count += 1
            if entered_count >= 2:
                started.set()
            await asyncio.sleep(10)
            return AgentRun(
                task=task.task_description, repo_path=task.repo_path, config=AgentConfig()
            )

        worker = Worker(
            queue,
            bus,
            slow_runner,
            poll_interval=0.01,
            max_concurrent_tasks=3,
            worktree_isolation=True,
            worktree_root=wt_root,
        )
        await queue.enqueue(
            Task(id="abort-1", task_description="a1", repo_path=str(repo), config=None)
        )
        await queue.enqueue(
            Task(id="abort-2", task_description="a2", repo_path=str(repo), config=None)
        )

        await worker.start()
        await asyncio.wait_for(started.wait(), timeout=3.0)
        await worker.stop()

        manager = worker.get_worktree_manager(repo)
        assert manager is not None
        assert [item.path for item in await manager.list_worktrees()] == [repo.resolve()]
        assert _git(repo, "for-each-ref", "refs/heads/agent-forge/run/") == ""
        assert list(wt_root.iterdir()) == []


class TestConfigAndCLIWorktrees:
    """Queue worktree config env vars and ``agent-forge worktrees`` CLI commands."""

    def test_queue_worktree_env_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_FORGE_QUEUE_WORKTREE_ISOLATION", "false")
        monkeypatch.setenv("AGENT_FORGE_QUEUE_MERGE_STRATEGY", "rebase")
        monkeypatch.setenv("AGENT_FORGE_QUEUE_WORKTREE_ROOT", str(tmp_path / "custom-wts"))

        cfg = load_config(project_path=tmp_path / "none.toml", user_path=tmp_path / "u.toml")
        assert cfg.queue.worktree_isolation is False
        assert cfg.queue.merge_strategy == "rebase"
        assert cfg.queue.worktree_root == str(tmp_path / "custom-wts")

    def test_cli_worktrees_list_integrate_and_cleanup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _init_repo(tmp_path / "repo", {"file.txt": "base\n"})
        wt_root = tmp_path / "wts"
        monkeypatch.setenv("AGENT_FORGE_QUEUE_WORKTREE_ROOT", str(wt_root))

        async def _setup() -> None:
            mgr = WorktreeManager(repo, worktrees_root=wt_root)
            async with mgr.lease("cli-good") as lease:
                (lease.worktree_path / "good.txt").write_text("ok\n", encoding="utf-8")
            await mgr.create("cli-stale")

        asyncio.run(_setup())
        runner = CliRunner()

        list_res = runner.invoke(main, ["worktrees", "list", "--repo", str(repo)])
        assert list_res.exit_code == 0, list_res.output
        assert "cli-stale" in list_res.output

        int_res = runner.invoke(main, ["worktrees", "integrate", "cli-good", "--repo", str(repo)])
        assert int_res.exit_code == 0, int_res.output
        assert (repo / "good.txt").read_text(encoding="utf-8") == "ok\n"

        clean_res = runner.invoke(main, ["worktrees", "cleanup", "--repo", str(repo)])
        assert clean_res.exit_code == 0, clean_res.output
        assert _git(repo, "for-each-ref", "refs/heads/agent-forge/run/") == ""

    def test_cli_worktrees_integrate_conflict_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _init_repo(tmp_path / "repo", {"file.txt": "base\n"})
        wt_root = tmp_path / "wts"
        monkeypatch.setenv("AGENT_FORGE_QUEUE_WORKTREE_ROOT", str(wt_root))

        async def _setup() -> None:
            mgr = WorktreeManager(repo, worktrees_root=wt_root)
            async with mgr.lease("c1") as l1:
                (l1.worktree_path / "file.txt").write_text("one\n", encoding="utf-8")
            async with mgr.lease("c2") as l2:
                (l2.worktree_path / "file.txt").write_text("two\n", encoding="utf-8")
            await mgr.integrate("c1")

        asyncio.run(_setup())
        runner = CliRunner()
        res = runner.invoke(main, ["worktrees", "integrate", "c2", "--repo", str(repo)])
        assert res.exit_code == 1
        assert "Conflict" in res.output
