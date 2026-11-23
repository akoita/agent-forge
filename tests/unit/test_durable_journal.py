"""Tests for durable execution journal, idempotency, crash resume, and replay (#138)."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from agent_forge.agent.budget import CancellationToken
from agent_forge.agent.checkpoint import load_checkpoint, save_checkpoint
from agent_forge.agent.core import react_loop
from agent_forge.agent.journal import (
    JOURNAL_FILENAME,
    RunJournal,
    compute_tool_idempotency_key,
    load_journal_entries,
    replay_journal,
)
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.persistence import load_run, save_run
from agent_forge.cli import main as cli_main
from agent_forge.llm.base import LLMResponse, Message, Role, TokenUsage, ToolCall
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools import ToolRegistry, WriteFileTool, create_default_registry


def _cli_runner() -> CliRunner:
    kwargs: dict[str, object] = {}
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        kwargs["mix_stderr"] = False
    return CliRunner(**kwargs)  # type: ignore[arg-type]


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


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "a.txt").write_text("base a\n", encoding="utf-8")
    (repo / "b.txt").write_text("base b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


class _LocalWorkspaceSandbox(Sandbox):
    """Lightweight Sandbox backed by a local directory for fast unit testing."""

    def __init__(self) -> None:
        self._root: Path | None = None
        self._alive = False

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
        return self._resolve(path).read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        del timeout_seconds
        if command.startswith("mkdir -p "):
            self._resolve(command.removeprefix("mkdir -p ").strip()).mkdir(
                parents=True, exist_ok=True
            )
            return ExecResult(exit_code=0, stdout="", stderr="")
        return ExecResult(exit_code=0, stdout="ok", stderr="")


def test_kill9_mid_run_and_cli_resume_no_duplicate_side_effects(tmp_path: Path) -> None:
    """Real SIGKILL mid-batch followed by `agent-forge resume` never repeats step 1."""
    repo = _make_git_repo(tmp_path)
    config_dir = tmp_path / "af_config"
    runs_dir = config_dir / "runs"
    counter_file = tmp_path / "side_effects.log"
    ready_flag = tmp_path / "ready_to_kill.flag"
    phase2_marker = tmp_path / "phase2.flag"
    driver = tmp_path / "worker_driver.py"

    driver.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys
            from pathlib import Path
            from unittest.mock import patch

            from agent_forge.cli import main as cli_main
            from agent_forge.eval.deterministic import SubprocessEvalSandbox
            from agent_forge.llm.base import LLMResponse, TokenUsage, ToolCall

            phase = sys.argv[1]
            config_dir = Path(os.environ["AF_TEST_CONFIG_DIR"])
            repo_path = os.environ["AF_TEST_REPO"]
            counter_file = os.environ["AF_TEST_COUNTER"]
            ready_flag = os.environ["AF_TEST_READY"]
            phase2_marker = os.environ["AF_TEST_PHASE2"]

            step1_cmd = (
                "python3 -c \\"from pathlib import Path; "
                f"Path({counter_file!r}).open('a', encoding='utf-8').write('step1\\\\n'); "
                "Path('a.txt').write_text('edited a\\\\n', encoding='utf-8')\\""
            )
            step2_cmd = (
                "python3 -c \\"import time; from pathlib import Path; "
                f"marker = Path({phase2_marker!r}); "
                f"ready = Path({ready_flag!r}); "
                f"counter = Path({counter_file!r}); "
                "ready.write_text('ready', encoding='utf-8') if not marker.exists() else None; "
                "time.sleep(30) if not marker.exists() else ("
                "counter.open('a', encoding='utf-8').write('step2\\\\n'), "
                "Path('b.txt').write_text('edited b\\\\n', encoding='utf-8'))\\""
            )
            verify_cmd = (
                "python3 -c \\"from pathlib import Path; "
                "assert Path('a.txt').read_text(encoding='utf-8') == 'edited a\\\\n'; "
                "assert Path('b.txt').read_text(encoding='utf-8') == 'edited b\\\\n'\\""
            )

            class _DriverLLM:
                def __init__(self, mode: str) -> None:
                    self._mode = mode

                async def complete(self, messages, tools=None, config=None):
                    del messages, tools, config
                    if self._mode == "phase1":
                        return LLMResponse(
                            content="",
                            tool_calls=[
                                ToolCall(
                                    id="call-step-1",
                                    name="run_shell",
                                    arguments={"command": step1_cmd},
                                ),
                                ToolCall(
                                    id="call-step-2",
                                    name="run_shell",
                                    arguments={"command": step2_cmd},
                                ),
                            ],
                            usage=TokenUsage(10, 10, 20),
                            finish_reason="tool_calls",
                        )
                    return LLMResponse(
                        content="Completed after resume.",
                        tool_calls=[],
                        usage=TokenUsage(10, 5, 15),
                        finish_reason="stop",
                    )

                async def close(self) -> None:
                    return None

            import agent_forge.cli as cli_mod
            import agent_forge.config as cfg_mod
            import agent_forge.agent.persistence as persist_mod
            import agent_forge.agent.core as core_mod

            cli_mod.USER_CONFIG_DIR = config_dir
            cfg_mod.USER_CONFIG_DIR = config_dir
            persist_mod.USER_CONFIG_DIR = config_dir
            os.environ["GEMINI_API_KEY"] = "test-key"

            with (
                patch.object(core_mod, "save_summary", lambda *_a: config_dir / "summary.json"),
                patch(
                    "agent_forge.sandbox.factory.create_sandbox",
                    side_effect=lambda _cfg: SubprocessEvalSandbox(),
                ),
                patch(
                    "agent_forge.agent.verification.create_sandbox",
                    side_effect=lambda _cfg: SubprocessEvalSandbox(),
                ),
                patch.object(cli_mod, "create_provider", return_value=_DriverLLM(phase)),
            ):
                if phase == "phase1":
                    cli_main.main(
                        [
                            "run",
                            "--task",
                            "Edit two files atomically",
                            "--repo",
                            repo_path,
                            "--verify-command",
                            verify_cmd,
                        ],
                        standalone_mode=False,
                    )
                else:
                    cli_main.main(
                        ["resume", sys.argv[2], "--output-format", "json"],
                        standalone_mode=False,
                    )
            """
        ),
        encoding="utf-8",
    )

    child_env = {
        **os.environ,
        "AF_TEST_CONFIG_DIR": str(config_dir),
        "AF_TEST_REPO": str(repo),
        "AF_TEST_COUNTER": str(counter_file),
        "AF_TEST_READY": str(ready_flag),
        "AF_TEST_PHASE2": str(phase2_marker),
    }

    proc1 = subprocess.Popen(  # noqa: S603
        [sys.executable, str(driver), "phase1"],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20.0
        while not ready_flag.exists():
            if proc1.poll() is not None:
                out, err = proc1.communicate()
                pytest.fail(f"phase1 exited unexpectedly ({proc1.returncode}): {out!r} {err!r}")
            assert time.monotonic() < deadline, "Timed out waiting for ready_to_kill flag"
            time.sleep(0.05)

        os.killpg(proc1.pid, signal.SIGKILL)
        exit_code = proc1.wait(timeout=10)
    finally:
        if proc1.poll() is None:
            os.killpg(proc1.pid, signal.SIGKILL)
            proc1.wait(timeout=5)

    assert exit_code == -signal.SIGKILL
    assert counter_file.read_text(encoding="utf-8").splitlines() == ["step1"]

    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name

    crashed_replay = replay_journal(run_id, base_dir=runs_dir)
    assert crashed_replay.valid is True
    assert crashed_replay.final_state == "running"
    assert len(crashed_replay.incomplete_actions) == 1
    assert len(crashed_replay.actions) == 1

    phase2_marker.write_text("go", encoding="utf-8")
    proc2 = subprocess.run(  # noqa: S603
        [sys.executable, str(driver), "phase2", run_id],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stderr + proc2.stdout
    resumed_payload = json.loads(proc2.stdout)
    assert resumed_payload["run_id"] == run_id
    assert resumed_payload["state"] == "completed"
    assert resumed_payload["verification"]["reason"] == "verified"

    assert counter_file.read_text(encoding="utf-8").splitlines() == ["step1", "step2"]

    final_replay = replay_journal(run_id, base_dir=runs_dir)
    assert final_replay.valid is True
    assert final_replay.duplicate_side_effects == []
    assert final_replay.incomplete_actions == []
    assert final_replay.state_sequence == [
        "pending",
        "running",
        "paused",
        "running",
        "completed",
    ]


@pytest.mark.asyncio
async def test_crash_between_action_result_and_checkpoint_reconciles_without_reexecuting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconciling journal.jsonl skips completed side effects even if checkpoint.json lagged."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr("agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir))
    monkeypatch.setattr(
        "agent_forge.observability.cost.save_summary",
        lambda *_a: tmp_path / "summary.json",
    )

    repo = _make_git_repo(tmp_path)
    run = AgentRun(
        task="Two writes",
        repo_path=str(repo),
        config=AgentConfig(max_iterations=5),
        state=RunState.PAUSED,
        iterations=1,
        messages=[
            Message(role=Role.SYSTEM, content="sys"),
            Message(role=Role.USER, content="Two writes"),
            Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write_file",
                        arguments={"path": "a.txt", "content": "edited a\n"},
                    ),
                    ToolCall(
                        id="c2",
                        name="write_file",
                        arguments={"path": "b.txt", "content": "edited b\n"},
                    ),
                ],
            ),
        ],
    )
    # Persist lagging checkpoint without c1 in messages or checkpoint.json.
    save_checkpoint(run, base_revision="rev1", partial_patch="", base_dir=runs_dir)

    # Record c1 directly in journal.jsonl (crash between action_result and checkpoint write).
    journal = RunJournal(run.id, base_dir=runs_dir)
    journal.append(
        "run_started",
        {"initial_state": "pending", "resumed": False, "base_revision": "rev1"},
        iteration=0,
    )
    journal.append(
        "state_transition",
        {"from_state": "pending", "to_state": "running", "reason": "run_started"},
        iteration=0,
    )
    key_c1 = compute_tool_idempotency_key(
        run.id, 1, "c1", "write_file", {"path": "a.txt", "content": "edited a\n"}, call_index=0
    )
    journal.append(
        "action_requested",
        {
            "tool_call_id": "c1",
            "idempotency_key": key_c1,
            "tool_name": "write_file",
            "arguments": {"path": "a.txt", "content": "edited a\n"},
            "side_effecting": True,
        },
        iteration=1,
    )
    journal.append(
        "action_result",
        {
            "tool_call_id": "c1",
            "idempotency_key": key_c1,
            "tool_name": "write_file",
            "arguments": {"path": "a.txt", "content": "edited a\n"},
            "side_effecting": True,
            "reused": False,
            "duration_ms": 2,
            "result": {"output": "Wrote a.txt", "error": None, "exit_code": 0},
        },
        iteration=1,
    )
    journal.append(
        "state_transition",
        {"from_state": "running", "to_state": "paused", "reason": "crash_snapshot"},
        iteration=1,
    )

    loaded_run, cp = load_checkpoint(run.id, base_dir=runs_dir)
    assert "c1" in cp.completed_tool_call_ids
    assert key_c1 in cp.completed_idempotency_keys
    assert any(m.role == Role.TOOL and m.tool_call_id == "c1" for m in loaded_run.messages)

    executed_paths: list[str] = []
    registry = ToolRegistry()
    write_tool = WriteFileTool()
    orig_execute = write_tool.execute

    async def _recording_write(arguments, sandbox):
        executed_paths.append(str(arguments["path"]))
        return await orig_execute(arguments, sandbox)

    write_tool.execute = _recording_write  # type: ignore[method-assign]
    registry.register(write_tool)

    llm = AsyncMock()
    llm.complete.return_value = LLMResponse(
        content="All done.", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
    )
    sb = _LocalWorkspaceSandbox()
    await sb.start(str(repo))
    res = await react_loop(loaded_run, llm, registry, sb, journal=journal)
    assert res.state == RunState.COMPLETED
    assert executed_paths == ["b.txt"]


def test_torn_trailing_journal_line_recovery_and_corrupt_middle_error(tmp_path: Path) -> None:
    """Truncated tail lines are safely tolerated and repaired; corrupt middle lines fail."""
    journal = RunJournal("torn-run", base_dir=tmp_path)
    journal.append("run_started", {"initial_state": "pending"}, iteration=0)
    journal.append(
        "state_transition",
        {"from_state": "pending", "to_state": "running", "reason": "run_started"},
        iteration=0,
    )
    # Append torn partial JSON line simulating SIGKILL mid-write.
    with journal.path.open("ab") as f:
        f.write(b'{"schema_version": "run-journal-v1", "seq": 2, "trunc')

    entries, had_tail = load_journal_entries(journal.path, allow_truncated_tail=True)
    assert had_tail is True
    assert len(entries) == 2

    replayed = replay_journal(journal.path)
    assert replayed.valid is True
    assert replayed.had_truncated_tail is True
    assert replayed.state_sequence == ["pending", "running"]

    # Next append repairs the torn tail atomically.
    entry = journal.append(
        "state_transition",
        {"from_state": "running", "to_state": "completed", "reason": "ok"},
        iteration=1,
    )
    assert entry.seq == 2
    strict_entries, strict_tail = load_journal_entries(journal.path, allow_truncated_tail=False)
    assert strict_tail is False
    assert len(strict_entries) == 3

    # Middle corruption must still raise ValueError.
    bad_path = tmp_path / "corrupt.jsonl"
    bad_path.write_text(
        journal.path.read_text(encoding="utf-8").splitlines()[0]
        + "\nNOT_JSON\n"
        + journal.path.read_text(encoding="utf-8").splitlines()[2]
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Corrupt journal entry at line 2"):
        load_journal_entries(bad_path, allow_truncated_tail=True)


@pytest.mark.asyncio
async def test_explicit_idempotency_key_reuse_skips_side_effect_and_marks_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-recorded action_result with matching idempotency key is reused without execution."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr("agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir))
    monkeypatch.setattr(
        "agent_forge.observability.cost.save_summary",
        lambda *_a: tmp_path / "summary.json",
    )

    run = AgentRun(
        task="Idempotent write",
        repo_path=str(tmp_path),
        config=AgentConfig(max_iterations=3),
    )
    journal = RunJournal(run.id, base_dir=runs_dir)
    args = {"path": "a.txt", "content": "x"}
    key = compute_tool_idempotency_key(run.id, 1, "c-idem", "write_file", args, call_index=0)
    journal.append("run_started", {"initial_state": "pending", "resumed": False}, iteration=0)
    journal.append(
        "state_transition",
        {"from_state": "pending", "to_state": "running", "reason": "run_started"},
        iteration=0,
    )
    journal.append(
        "action_requested",
        {
            "tool_call_id": "c-idem",
            "idempotency_key": key,
            "tool_name": "write_file",
            "arguments": args,
            "side_effecting": True,
        },
        iteration=1,
    )
    journal.append(
        "action_result",
        {
            "tool_call_id": "c-idem",
            "idempotency_key": key,
            "tool_name": "write_file",
            "arguments": args,
            "side_effecting": True,
            "reused": False,
            "duration_ms": 4,
            "result": {"output": "cached write result", "error": None, "exit_code": 0},
        },
        iteration=1,
    )
    journal.append(
        "state_transition",
        {"from_state": "running", "to_state": "paused", "reason": "interrupted"},
        iteration=1,
    )
    run.state = RunState.PAUSED

    execute_calls: list[dict[str, object]] = []
    registry = ToolRegistry()
    write_tool = WriteFileTool()

    async def _fail_if_executed(arguments, sandbox):
        del sandbox
        execute_calls.append(dict(arguments))
        raise AssertionError("Side-effecting tool should have been skipped via idempotency key")

    write_tool.execute = _fail_if_executed  # type: ignore[method-assign]
    registry.register(write_tool)

    llm = AsyncMock()
    llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c-idem", name="write_file", arguments=args)],
            usage=TokenUsage(10, 10, 20),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content="Done.", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
        ),
    ]
    sb = _LocalWorkspaceSandbox()
    await sb.start(str(tmp_path))

    res = await react_loop(run, llm, registry, sb, journal=journal)
    assert res.state == RunState.COMPLETED
    assert execute_calls == []
    assert any(m.role == Role.TOOL and m.content == "cached write result" for m in res.messages)

    replay = replay_journal(run.id, base_dir=runs_dir)
    assert replay.valid is True
    assert replay.reused_actions == 1
    assert replay.duplicate_side_effects == []


@pytest.mark.asyncio
async def test_recoverable_interruption_stops_cleanly_without_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recoverable interruption transitions to PAUSED without raising and resumes cleanly."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr("agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir))
    monkeypatch.setattr(
        "agent_forge.observability.cost.save_summary",
        lambda *_a: tmp_path / "summary.json",
    )

    repo = _make_git_repo(tmp_path)
    token = CancellationToken()
    executed: list[str] = []
    registry = ToolRegistry()
    write_tool = WriteFileTool()
    orig_exec = write_tool.execute

    async def _interrupt_after_first(arguments, sandbox):
        executed.append(str(arguments["path"]))
        result = await orig_exec(arguments, sandbox)
        if arguments["path"] == "a.txt":
            token.interrupt("Operator paused mid-batch")
        return result

    write_tool.execute = _interrupt_after_first  # type: ignore[method-assign]
    registry.register(write_tool)

    llm = AsyncMock()
    llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="write_file",
                    arguments={"path": "a.txt", "content": "edited a\n"},
                ),
                ToolCall(
                    id="c2",
                    name="write_file",
                    arguments={"path": "b.txt", "content": "edited b\n"},
                ),
            ],
            usage=TokenUsage(10, 10, 20),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content="Resumed.", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
        ),
    ]

    run = AgentRun(
        task="Interrupt batch", repo_path=str(repo), config=AgentConfig(max_iterations=5)
    )
    journal = RunJournal(run.id, base_dir=runs_dir)
    sb = _LocalWorkspaceSandbox()
    await sb.start(str(repo))

    async def _cp(r: AgentRun) -> None:
        save_checkpoint(r, base_revision="HEAD", base_dir=runs_dir)

    paused = await react_loop(
        run, llm, registry, sb, cancellation_token=token, on_checkpoint=_cp, journal=journal
    )
    assert paused.state == RunState.PAUSED
    assert paused.error == "Operator paused mid-batch"
    assert executed == ["a.txt"]

    loaded, _cp_loaded = load_checkpoint(run.id, base_dir=runs_dir)
    completed = await react_loop(loaded, llm, registry, sb, on_checkpoint=_cp, journal=journal)
    assert completed.state == RunState.COMPLETED
    assert executed == ["a.txt", "b.txt"]

    replay = replay_journal(run.id, base_dir=runs_dir)
    assert replay.valid is True
    assert replay.duplicate_side_effects == []
    assert replay.state_sequence == ["pending", "running", "paused", "running", "completed"]
    assert any(item["recoverable"] is True for item in replay.interruptions)


@pytest.mark.asyncio
async def test_raise_on_cancel_false_returns_paused_and_cli_cancel_journals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`raise_on_cancel=False` returns PAUSED on CancelledError and CLI cancel records journal."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr("agent_forge.cli.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir))
    monkeypatch.setattr(
        "agent_forge.observability.cost.save_summary",
        lambda *_a: tmp_path / "summary.json",
    )

    entered = asyncio.Event()
    llm = AsyncMock()

    async def _hang(**_kwargs: object) -> LLMResponse:
        entered.set()
        await asyncio.Future()
        raise AssertionError  # pragma: no cover

    llm.complete.side_effect = _hang
    run = AgentRun(task="Cancel cleanly", repo_path=str(tmp_path), config=AgentConfig())
    journal = RunJournal(run.id, base_dir=runs_dir)
    sb = _LocalWorkspaceSandbox()
    await sb.start(str(tmp_path))

    task = asyncio.create_task(
        react_loop(
            run,
            llm,
            create_default_registry(load_plugins=False),
            sb,
            journal=journal,
            raise_on_cancel=False,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    paused = await task
    assert paused.state == RunState.PAUSED

    cancel_res = _cli_runner().invoke(cli_main, ["cancel", run.id, "--reason", "Done waiting"])
    assert cancel_res.exit_code == 0, cancel_res.output
    assert load_run(run.id, base_dir=runs_dir).state == RunState.CANCELLED

    replay = replay_journal(run.id, base_dir=runs_dir)
    assert replay.valid is True
    assert replay.state_sequence == ["pending", "running", "paused", "cancelled"]
    assert len(replay.interruptions) == 2


def test_cli_replay_json_text_and_anomaly_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`agent-forge replay` outputs valid JSON/text summaries and fails on anomalies."""
    monkeypatch.setattr("agent_forge.cli.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path)

    run_id = "replay-run-1"
    journal = RunJournal(run_id, base_dir=tmp_path / "runs")
    args = {"path": "x.py", "content": "ok"}
    key = compute_tool_idempotency_key(run_id, 1, "c1", "write_file", args, call_index=0)
    journal.append("run_started", {"initial_state": "pending"}, iteration=0)
    journal.append(
        "state_transition",
        {"from_state": "pending", "to_state": "running", "reason": "start"},
        iteration=0,
    )
    journal.append(
        "action_requested",
        {
            "tool_call_id": "c1",
            "idempotency_key": key,
            "tool_name": "write_file",
            "arguments": args,
            "side_effecting": True,
        },
        iteration=1,
    )
    journal.append(
        "action_result",
        {
            "tool_call_id": "c1",
            "idempotency_key": key,
            "tool_name": "write_file",
            "arguments": args,
            "side_effecting": True,
            "reused": False,
            "duration_ms": 5,
            "result": {"output": "wrote", "error": None, "exit_code": 0},
        },
        iteration=1,
    )
    journal.append(
        "state_transition",
        {"from_state": "running", "to_state": "completed", "reason": "done"},
        iteration=1,
    )

    runner = _cli_runner()
    json_res = runner.invoke(cli_main, ["replay", run_id, "--output-format", "json"])
    assert json_res.exit_code == 0, json_res.output
    data = json.loads(json_res.stdout)
    assert data["valid"] is True
    assert data["state_sequence"] == ["pending", "running", "completed"]
    assert len(data["actions"]) == 1

    text_res = runner.invoke(cli_main, ["replay", str(journal.path)])
    assert text_res.exit_code == 0, text_res.output
    assert "Journal Replay" in text_res.stdout
    assert "write_file" in text_res.stdout

    # Inject duplicate non-reused side effect -> replay reports anomaly and exits 1.
    journal.append(
        "action_result",
        {
            "tool_call_id": "c1",
            "idempotency_key": key,
            "tool_name": "write_file",
            "arguments": args,
            "side_effecting": True,
            "reused": False,
            "duration_ms": 5,
            "result": {"output": "wrote again", "error": None, "exit_code": 0},
        },
        iteration=1,
    )
    bad_res = runner.invoke(cli_main, ["replay", run_id, "--output-format", "json"])
    assert bad_res.exit_code == 1
    bad_data = json.loads(bad_res.stdout)
    assert bad_data["valid"] is False
    assert bad_data["duplicate_side_effects"] == [key]


def test_non_verified_run_checkpoints_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-verified CLI runs still persist checkpoints and resume cleanly."""
    monkeypatch.setattr("agent_forge.cli.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "agent_forge.observability.cost.save_summary",
        lambda *_a: tmp_path / "summary.json",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    repo = _make_git_repo(tmp_path)
    llm = AsyncMock()
    llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="r1", name="read_file", arguments={"path": "/workspace/a.txt"})
            ],
            usage=TokenUsage(10_000, 1_000, 11_000),
            model="gpt-4o",
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content="Completed.",
            tool_calls=[],
            usage=TokenUsage(10, 5, 15),
            model="gpt-4o",
            finish_reason="stop",
        ),
    ]
    llm.close = AsyncMock()
    sb = _LocalWorkspaceSandbox()
    runner = _cli_runner()

    with (
        patch("agent_forge.sandbox.factory.create_sandbox", return_value=sb),
        patch("agent_forge.cli.create_provider", return_value=llm),
    ):
        run_res = runner.invoke(
            cli_main,
            [
                "run",
                "--task",
                "Inspect repo",
                "--repo",
                str(repo),
                "--max-cost",
                "0.000001",
                "--budget-action",
                "pause",
                "--output-format",
                "json",
            ],
        )
        assert run_res.exit_code == 0, run_res.output + run_res.stderr
        paused_payload = json.loads(run_res.stdout)
        assert paused_payload["state"] == "paused"
        run_id = paused_payload["run_id"]
        assert (tmp_path / "runs" / run_id / "checkpoint.json").is_file()
        assert (tmp_path / "runs" / run_id / JOURNAL_FILENAME).is_file()

        resumed = runner.invoke(
            cli_main,
            ["resume", run_id, "--max-cost", "1.0", "--output-format", "json"],
        )
        assert resumed.exit_code == 0, resumed.output + resumed.stderr
        completed_payload = json.loads(resumed.stdout)
        assert completed_payload["state"] == "completed"


def test_builtin_tools_side_effecting_classification() -> None:
    """Read-only inspection tools opt out while mutating/execution tools default to True."""
    registry = create_default_registry(load_plugins=False)
    expected_readonly = {"read_file", "list_directory", "search_codebase", "git_diff", "shell_poll"}
    actual_readonly = {
        defn.name
        for defn in registry.list_definitions()
        if not registry.get(defn.name).side_effecting
    }
    assert actual_readonly == expected_readonly
    assert len(registry.list_definitions()) == 15


def test_sigterm_pauses_cleanly_with_resume_hint(tmp_path: Path) -> None:
    """POSIX SIGTERM pauses an in-flight CLI run cleanly and emits a resume hint."""
    repo = _make_git_repo(tmp_path)
    config_dir = tmp_path / "af_sigterm"
    ready_flag = tmp_path / "sigterm_ready.flag"
    driver = tmp_path / "sigterm_driver.py"
    driver.write_text(
        textwrap.dedent(
            """
            import asyncio
            import os
            import sys
            from pathlib import Path
            from unittest.mock import patch

            from agent_forge.cli import main as cli_main
            from agent_forge.eval.deterministic import SubprocessEvalSandbox
            from agent_forge.llm.base import LLMResponse, TokenUsage

            config_dir = Path(os.environ["AF_TEST_CONFIG_DIR"])
            repo_path = os.environ["AF_TEST_REPO"]
            ready_flag = Path(os.environ["AF_TEST_READY"])

            class _SlowLLM:
                async def complete(self, messages, tools=None, config=None):
                    del messages, tools, config
                    ready_flag.write_text("ready", encoding="utf-8")
                    await asyncio.sleep(30)
                    return LLMResponse(content="Done", finish_reason="stop")

                async def close(self) -> None:
                    return None

            import agent_forge.cli as cli_mod
            import agent_forge.config as cfg_mod
            import agent_forge.agent.persistence as persist_mod
            import agent_forge.agent.core as core_mod

            cli_mod.USER_CONFIG_DIR = config_dir
            cfg_mod.USER_CONFIG_DIR = config_dir
            persist_mod.USER_CONFIG_DIR = config_dir
            os.environ["GEMINI_API_KEY"] = "test-key"

            with (
                patch.object(core_mod, "save_summary", lambda *_a: config_dir / "summary.json"),
                patch(
                    "agent_forge.sandbox.factory.create_sandbox",
                    side_effect=lambda _cfg: SubprocessEvalSandbox(),
                ),
                patch.object(cli_mod, "create_provider", return_value=_SlowLLM()),
            ):
                cli_main.main(
                    [
                        "run",
                        "--task",
                        "Wait for signal",
                        "--repo",
                        repo_path,
                        "--output-format",
                        "json",
                    ],
                    standalone_mode=False,
                )
            """
        ),
        encoding="utf-8",
    )

    child_env = {
        **os.environ,
        "AF_TEST_CONFIG_DIR": str(config_dir),
        "AF_TEST_REPO": str(repo),
        "AF_TEST_READY": str(ready_flag),
    }
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(driver)],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20.0
        while not ready_flag.exists():
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"sigterm driver exited early ({proc.returncode}): {out!r} {err!r}")
            assert time.monotonic() < deadline, "Timed out waiting for LLM readiness"
            time.sleep(0.05)

        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    assert proc.returncode == 0, err + out
    payload = json.loads(out)
    assert payload["state"] == "paused"
    assert f"agent-forge resume {payload['run_id']}" in err

    replayed = replay_journal(payload["run_id"], base_dir=config_dir / "runs")
    assert replayed.valid is True
    assert replayed.state_sequence == ["pending", "running", "paused"]
    assert len(replayed.interruptions) == 1
    assert replayed.interruptions[0]["recoverable"] is True


@pytest.mark.asyncio
async def test_fresh_pending_run_reusing_id_resets_stale_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brand-new PENDING run reusing a run ID clears any stale journal and executes fresh."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "agent_forge.agent.core.save_summary",
        lambda *_a: tmp_path / "summary.json",
    )

    same_id = "fixed-reused-run-id"
    registry = ToolRegistry()
    registry.register(WriteFileTool())

    repo1 = tmp_path / "repo1"
    repo1.mkdir()
    sb1 = _LocalWorkspaceSandbox()
    await sb1.start(str(repo1))

    llm1 = AsyncMock()
    llm1.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="c-write",
                    name="write_file",
                    arguments={"path": "out.txt", "content": "first\n"},
                )
            ],
            usage=TokenUsage(10, 10, 20),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content="Done 1.", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
        ),
    ]
    run1 = AgentRun(
        id=same_id,
        task="Write file 1",
        repo_path=str(repo1),
        config=AgentConfig(max_iterations=3),
    )
    res1 = await react_loop(run1, llm1, registry, sb1)
    assert res1.state == RunState.COMPLETED
    assert (repo1 / "out.txt").read_text(encoding="utf-8") == "first\n"

    # Second brand-new PENDING run with identical id on a clean repo must not reuse run1's journal.
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    sb2 = _LocalWorkspaceSandbox()
    await sb2.start(str(repo2))

    llm2 = AsyncMock()
    llm2.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="c-write",
                    name="write_file",
                    arguments={"path": "out.txt", "content": "first\n"},
                )
            ],
            usage=TokenUsage(10, 10, 20),
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content="Done 2.", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
        ),
    ]
    run2 = AgentRun(
        id=same_id,
        task="Write file 2",
        repo_path=str(repo2),
        config=AgentConfig(max_iterations=3),
    )
    res2 = await react_loop(run2, llm2, registry, sb2)
    assert res2.state == RunState.COMPLETED
    assert (repo2 / "out.txt").read_text(encoding="utf-8") == "first\n"

    replayed = replay_journal(same_id, base_dir=runs_dir)
    assert replayed.valid is True
    assert replayed.reused_actions == 0
    assert replayed.state_sequence == ["pending", "running", "completed"]
