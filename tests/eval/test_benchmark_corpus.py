"""Deterministic grader checks for every built-in benchmark corpus task."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_forge.agent.journal import replay_journal
from agent_forge.eval import (
    BUILTIN_TASKS,
    BenchmarkTask,
    check_deterministic_gates,
    materialize_task_repo,
    run_deterministic_eval,
)


def _run_unittest(repo: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    return subprocess.run(  # noqa: S603
        cmd,
        cwd=repo,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _write_files(repo: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("task", BUILTIN_TASKS, ids=[t.id for t in BUILTIN_TASKS])
def test_builtin_task_discriminates_base_empty_incomplete_and_fixed(
    tmp_path: Path,
    task: BenchmarkTask,
) -> None:
    """Verify every pinned task fails on base/empty/incomplete and passes on its known fix."""
    assert len(BUILTIN_TASKS) >= 12
    assert task.hidden_tests, f"{task.id} must define hidden verifier tests"
    assert task.incomplete_files, f"{task.id} must define an incomplete partial fix"

    repo = materialize_task_repo(task, tmp_path / task.id)
    _write_files(repo, task.hidden_tests)

    base_check = _run_unittest(repo)
    assert base_check.returncode != 0, f"{task.id} unexpectedly passed on base"

    # Empty patch check (working tree clean against HEAD)
    empty_diff = subprocess.run(
        ["git", "diff", "HEAD"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert empty_diff.stdout == ""
    assert _run_unittest(repo).returncode != 0

    # Incomplete / partial fix modifies tracked source but fails combined visible + hidden checks
    _write_files(repo, task.incomplete_files)
    incomplete_diff = subprocess.run(
        ["git", "diff", "--name-only"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert incomplete_diff.stdout.strip() != ""
    assert _run_unittest(repo).returncode != 0, f"{task.id} incomplete fix unexpectedly passed"

    # Reset and apply known-good fix -> produces non-empty diff and passes all tests
    subprocess.run(
        ["git", "checkout", "--", "."],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=10,
    )
    _write_files(repo, task.known_good_files)

    fixed_diff = subprocess.run(
        ["git", "diff"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert fixed_diff.stdout.strip() != ""

    fixed_check = _run_unittest(repo)
    assert fixed_check.returncode == 0, fixed_check.stdout + fixed_check.stderr


@pytest.mark.asyncio
async def test_deterministic_eval_smoke_gates_and_trajectories(tmp_path: Path) -> None:
    """Offline smoke eval passes deterministic oracle and rejects noop/incomplete runs."""
    traj_dir = tmp_path / "trajectories"
    report_json = tmp_path / "report.json"
    report_md = tmp_path / "report.md"

    report = await run_deterministic_eval(
        suite="smoke",
        output_json=report_json,
        output_md=report_md,
        trajectory_dir=traj_dir,
    )

    assert len(report.tasks) == 4
    assert check_deterministic_gates(report) == []
    assert report_json.is_file()
    assert report_md.is_file()

    by_profile = {item.profile: item for item in report.profiles}
    assert by_profile["deterministic-oracle"].verified_fixes == 4
    assert by_profile["deterministic-noop"].verified_fixes == 0
    assert by_profile["deterministic-incomplete"].verified_fixes == 0

    for summary in report.profiles:
        expected_final = (
            "completed" if summary.profile == "deterministic-oracle" else "verification_failed"
        )
        assert summary.false_positive_rate == 0.0
        for attempt in summary.results:
            assert attempt.trajectory_path is not None
            traj_file = (
                tmp_path / "trajectories" / f"{summary.profile}__{attempt.task_id}__attempt-1.jsonl"
            )
            assert traj_file.is_file()
            entries = [
                json.loads(line) for line in traj_file.read_text(encoding="utf-8").splitlines()
            ]
            assert all(row["schema_version"] == "run-journal-v1" for row in entries)
            kinds = [row["kind"] for row in entries]
            assert "run_started" in kinds
            assert "state_transition" in kinds
            assert "message_recorded" in kinds
            assert "action_requested" in kinds
            assert "action_result" in kinds
            assert "verification_recorded" in kinds
            assert "contract_evaluated" in kinds
            assert kinds[-1] == "attempt_result"

            replayed = replay_journal(traj_file)
            assert replayed.valid is True
            assert replayed.duplicate_side_effects == []
            assert replayed.state_sequence == ["pending", "running", expected_final]
            assert replayed.contract_evaluation is not None
            assert replayed.attempt_result is not None
            assert replayed.attempt_result["task_id"] == attempt.task_id


@pytest.mark.asyncio
async def test_completion_contract_eliminates_false_positive_completions() -> None:
    """Measure that uncontracted stops produce false positives while contracts block all."""
    import tempfile

    from agent_forge.agent.core import react_loop
    from agent_forge.agent.models import AgentConfig, AgentRun
    from agent_forge.agent.verification import VerificationWorkspace
    from agent_forge.config import ModelProfileSettings
    from agent_forge.eval.benchmark import (
        build_completed_result,
        materialize_task_repo,
        summarize_profile,
    )
    from agent_forge.eval.corpus import resolve_tasks
    from agent_forge.eval.deterministic import SubprocessEvalSandbox, _ScriptedEvalLLM
    from agent_forge.sandbox.base import SandboxConfig
    from agent_forge.tools import create_default_registry

    smoke_tasks = resolve_tasks(suite="smoke")
    tools = create_default_registry(load_plugins=False)
    sandbox_cfg = SandboxConfig(
        backend="subprocess-eval",
        network_enabled=False,
        writable_cache_mounts=False,
        timeout_seconds=20,
    )

    uncontracted_results = []
    contracted_results = []

    for mode in ("noop", "incomplete"):
        for task in smoke_tasks:
            with tempfile.TemporaryDirectory() as tmp_dir:
                repo_path = materialize_task_repo(task, Path(tmp_dir) / "repo")
                cfg = AgentConfig(
                    model=f"scripted-{mode}", provider="deterministic", max_iterations=10
                )

                # Without verifier/contract: react_loop stops cleanly -> false-positive COMPLETED.
                run_no_contract = AgentRun(task=task.prompt, repo_path=str(repo_path), config=cfg)
                sb1 = SubprocessEvalSandbox()
                await sb1.start(repo_path=str(repo_path), config=sandbox_cfg)
                try:
                    await react_loop(run_no_contract, _ScriptedEvalLLM(task, mode), tools, sb1)
                finally:
                    await sb1.stop()
                uncontracted_results.append(
                    build_completed_result(
                        task.id, 1, run_no_contract, cfg.model, None, [cfg.model], 0.1
                    )
                )

                # With harness completion contract + independent verifier: 0 false positives.
                run_contracted = AgentRun(task=task.prompt, repo_path=str(repo_path), config=cfg)
                sb2 = SubprocessEvalSandbox()
                with VerificationWorkspace(
                    str(repo_path), task.verify_command, extra_verifier_files=task.hidden_tests
                ) as ws:
                    await sb2.start(repo_path=str(ws.workspace), config=sandbox_cfg)
                    try:
                        await react_loop(
                            run_contracted,
                            _ScriptedEvalLLM(task, mode),
                            tools,
                            sb2,
                            verify=lambda ws=ws, sb=sb2: ws.verify(
                                sandbox_cfg,
                                sb,
                                sandbox_factory=lambda _c: SubprocessEvalSandbox(),
                            ),
                        )
                    finally:
                        await sb2.stop()
                contracted_results.append(
                    build_completed_result(
                        task.id, 1, run_contracted, cfg.model, None, [cfg.model], 0.1
                    )
                )

    profile = ModelProfileSettings(
        provider="deterministic", model="scripted-wrong", temperature=0.0
    )
    before = summarize_profile(
        "uncontracted",
        profile,
        uncontracted_results,
        temperature=0.0,
        max_iterations=10,
        max_tokens_per_run=40_000,
    )
    after = summarize_profile(
        "contracted",
        profile,
        contracted_results,
        temperature=0.0,
        max_iterations=10,
        max_tokens_per_run=40_000,
    )

    assert before.false_positive_rate == 1.0
    assert after.false_positive_rate == 0.0
    assert all(r.run_state == "verification_failed" and r.unmet_clauses for r in contracted_results)


@pytest.mark.asyncio
async def test_context_engine_reduces_tokens_per_task_on_m0_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify M2 Context Engine cuts prompt tokens on M0 while holding 4/4 fix rate."""
    import tempfile

    from agent_forge.agent.context import ContextEngine, ContextPolicy, estimate_messages_tokens
    from agent_forge.agent.core import react_loop
    from agent_forge.agent.models import AgentConfig, AgentRun, RunState
    from agent_forge.agent.verification import VerificationWorkspace
    from agent_forge.eval.corpus import resolve_tasks
    from agent_forge.eval.deterministic import SubprocessEvalSandbox
    from agent_forge.llm.base import LLMResponse, TokenUsage, ToolCall
    from agent_forge.sandbox.base import SandboxConfig
    from agent_forge.tools import create_default_registry

    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.core.save_summary", lambda *_a: tmp_path / "s.json")

    class _TokenAccountingScriptedLLM:
        def __init__(self, task: BenchmarkTask) -> None:
            src_file = next(iter(task.files))
            test_file = next(path for path in task.tests if path.endswith(".py") and "/" in path)
            steps = [
                ToolCall(
                    id="c-log", name="read_file", arguments={"path": "/workspace/CONTEXT_LOG.md"}
                ),
                ToolCall(
                    id="c-test", name="read_file", arguments={"path": f"/workspace/{test_file}"}
                ),
                ToolCall(
                    id="c-src1",
                    name="read_file",
                    arguments={"path": f"/workspace/{src_file}"},
                ),
                ToolCall(
                    id="c-fail1", name="run_shell", arguments={"command": task.verify_command}
                ),
                *[
                    ToolCall(
                        id=f"c-inc-{idx}",
                        name="write_file",
                        arguments={"path": f"/workspace/{path}", "content": content},
                    )
                    for idx, (path, content) in enumerate(task.incomplete_files.items())
                ],
                ToolCall(
                    id="c-fail2", name="run_shell", arguments={"command": task.verify_command}
                ),
                ToolCall(
                    id="c-src2",
                    name="read_file",
                    arguments={"path": f"/workspace/{src_file}"},
                ),
                *[
                    ToolCall(
                        id=f"c-good-{idx}",
                        name="write_file",
                        arguments={"path": f"/workspace/{path}", "content": content},
                    )
                    for idx, (path, content) in enumerate(task.known_good_files.items())
                ],
                ToolCall(id="c-pass", name="run_shell", arguments={"command": task.verify_command}),
            ]
            self._responses = [
                LLMResponse(content="", tool_calls=[tc], finish_reason="tool_calls") for tc in steps
            ] + [LLMResponse(content="Verified fix complete.", tool_calls=[], finish_reason="stop")]
            self._cursor = 0

        async def complete(self, messages, tools=None, config=None):
            del tools, config
            template = self._responses[min(self._cursor, len(self._responses) - 1)]
            self._cursor += 1
            prompt_tokens = estimate_messages_tokens(messages)
            completion_tokens = 25
            return LLMResponse(
                content=template.content,
                tool_calls=template.tool_calls,
                usage=TokenUsage(
                    prompt_tokens,
                    completion_tokens,
                    prompt_tokens + completion_tokens,
                ),
                model="scripted-m2",
                finish_reason=template.finish_reason,
            )

        async def close(self) -> None:
            return None

    async def _run_suite(*, use_context: bool) -> tuple[int, int]:
        tasks = resolve_tasks(suite="smoke")
        tools = create_default_registry(load_plugins=False)
        sandbox_cfg = SandboxConfig(
            backend="subprocess-eval",
            network_enabled=False,
            writable_cache_mounts=False,
            timeout_seconds=20,
        )
        policy = ContextPolicy(
            context_window_tokens=1_200,
            compaction_trigger_ratio=0.70,
            compaction_target_ratio=0.45,
            keep_recent_turns=2,
            stale_tool_result_max_chars=240,
        )
        verified_count = 0
        prompt_token_total = 0

        for task in tasks:
            with tempfile.TemporaryDirectory() as td:
                repo = materialize_task_repo(task, Path(td) / "repo")
                (repo / "AGENTS.md").write_text(
                    "ROOT_CONVENTION: keep edits surgical and verified.", encoding="utf-8"
                )
                os.symlink("AGENTS.md", repo / "CLAUDE.md")
                (repo / "tests" / "AGENTS.md").write_text(
                    "TESTS_CONVENTION: preserve all test assertions.", encoding="utf-8"
                )
                (repo / "CONTEXT_LOG.md").write_text(
                    "Diagnostic context line with historical run trace details.\n" * 60,
                    encoding="utf-8",
                )
                subprocess.run(
                    ["git", "add", "-A"],  # noqa: S607
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "Add repository context docs"],  # noqa: S607
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    env={
                        **os.environ,
                        "GIT_AUTHOR_NAME": "Eval",
                        "GIT_AUTHOR_EMAIL": "eval@example.com",
                        "GIT_COMMITTER_NAME": "Eval",
                        "GIT_COMMITTER_EMAIL": "eval@example.com",
                    },
                )

                cfg = AgentConfig(
                    model="scripted-m2",
                    provider="deterministic",
                    max_iterations=16,
                    context_enabled=use_context,
                )
                run = AgentRun(task=task.prompt, repo_path=str(repo), config=cfg)
                sb = SubprocessEvalSandbox()
                with VerificationWorkspace(
                    str(repo), task.verify_command, extra_verifier_files=task.hidden_tests
                ) as ws:
                    await sb.start(repo_path=str(ws.workspace), config=sandbox_cfg)
                    engine = ContextEngine(ws.workspace, task=task.prompt, policy=policy)
                    try:
                        await react_loop(
                            run,
                            _TokenAccountingScriptedLLM(task),
                            tools,
                            sb,
                            verify=lambda ws=ws, sb=sb: ws.verify(
                                sandbox_cfg,
                                sb,
                                sandbox_factory=lambda _c: SubprocessEvalSandbox(),
                            ),
                            context_engine=engine if use_context else None,
                        )
                    finally:
                        await sb.stop()

                assert run.state == RunState.COMPLETED
                assert run.verification is not None and run.verification.passed is True
                verified_count += 1
                prompt_token_total += run.total_tokens.prompt_tokens
                if use_context:
                    loaded = run.context_stats["instruction_paths"]
                    assert "AGENTS.md" in loaded
                    assert "tests/AGENTS.md" in loaded
                    assert "CLAUDE.md" not in loaded
                    assert run.context_stats["compactions"] >= 1
                    assert "ROOT_CONVENTION" in run.messages[0].content
                    assert any("TESTS_CONVENTION" in m.content for m in run.messages)

        return verified_count, prompt_token_total

    raw_verified, raw_tokens = await _run_suite(use_context=False)
    managed_verified, managed_tokens = await _run_suite(use_context=True)

    assert raw_verified == 4
    assert managed_verified == 4
    assert managed_tokens < raw_tokens * 0.85


def _build_responses_mock_payload(task: BenchmarkTask, turn: int) -> dict[str, object]:
    src_file = next(iter(task.files))
    if turn == 1:
        output: list[dict[str, object]] = [
            {
                "type": "function_call",
                "call_id": f"call_{task.id}_read",
                "name": "read_file",
                "arguments": json.dumps({"path": f"/workspace/{src_file}"}),
            }
        ]
        cached = 0
    elif turn == 2:
        output = [
            {
                "type": "function_call",
                "call_id": f"call_{task.id}_fix_{idx}",
                "name": "write_file",
                "arguments": json.dumps({"path": f"/workspace/{path}", "content": content}),
            }
            for idx, (path, content) in enumerate(task.known_good_files.items())
        ]
        cached = 450
    else:
        output = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Fix verified."}],
            }
        ]
        cached = 520
    return {
        "id": f"resp_{task.id}_{turn}",
        "model": "gpt-5.4",
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": 560 if turn > 1 else 420,
            "output_tokens": 35,
            "total_tokens": 595 if turn > 1 else 455,
            "input_tokens_details": {"cached_tokens": cached},
        },
    }


def _build_chat_mock_payload(task: BenchmarkTask, turn: int, full_tokens: int) -> dict[str, object]:
    src_file = next(iter(task.files))
    if turn == 1:
        message: dict[str, object] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{task.id}_read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": f"/workspace/{src_file}"}),
                    },
                }
            ],
        }
        finish_reason = "tool_calls"
    elif turn == 2:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{task.id}_fix_{idx}",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": f"/workspace/{path}", "content": content}),
                    },
                }
                for idx, (path, content) in enumerate(task.known_good_files.items())
            ],
        }
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": "Fix verified."}
        finish_reason = "stop"
    return {
        "model": "gpt-5.4",
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": max(full_tokens, 560 if turn > 1 else 420),
            "completion_tokens": 35,
            "total_tokens": max(full_tokens, 560 if turn > 1 else 420) + 35,
        },
    }


async def _run_single_openai_task(
    task: BenchmarkTask,
    api_mode: str,
) -> tuple[int, float]:
    import tempfile

    import httpx
    import respx

    from agent_forge.agent.context import ContextEngine, ContextPolicy
    from agent_forge.agent.core import react_loop
    from agent_forge.agent.models import AgentConfig, AgentRun, RunState
    from agent_forge.agent.verification import VerificationWorkspace
    from agent_forge.eval.deterministic import SubprocessEvalSandbox
    from agent_forge.llm.openai import OpenAIProvider
    from agent_forge.observability.cost import _estimate_cost
    from agent_forge.sandbox.base import SandboxConfig
    from agent_forge.tools import create_default_registry

    tools = create_default_registry(load_plugins=False)
    sandbox_cfg = SandboxConfig(
        backend="subprocess-eval",
        network_enabled=False,
        writable_cache_mounts=False,
        timeout_seconds=20,
    )
    policy = ContextPolicy(discover_instructions=False, repo_map_enabled=False)
    state = {"turn": 0, "cont_bytes": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        state["turn"] += 1
        turn = state["turn"]
        body = json.loads(request.content)
        if turn > 1:
            state["cont_bytes"] += len(request.content)
        if api_mode == "responses":
            assert "prompt_cache_key" in body
            if turn > 1:
                assert body["previous_response_id"] == f"resp_{task.id}_{turn - 1}"
            return httpx.Response(200, json=_build_responses_mock_payload(task, turn))
        msg_tokens = len(json.dumps(body.get("messages", []))) // 4
        return httpx.Response(200, json=_build_chat_mock_payload(task, turn, msg_tokens))

    endpoint = (
        "https://api.openai.com/v1/responses"
        if api_mode == "responses"
        else "https://api.openai.com/v1/chat/completions"
    )
    with (
        tempfile.TemporaryDirectory() as td,
        respx.mock(assert_all_called=True) as mock_router,
    ):
        mock_router.post(endpoint).mock(side_effect=_handler)
        repo = materialize_task_repo(task, Path(td) / "repo")
        cfg = AgentConfig(model="gpt-5.4", provider="openai", max_iterations=8)
        run = AgentRun(task=task.prompt, repo_path=str(repo), config=cfg)
        sb = SubprocessEvalSandbox()
        provider = OpenAIProvider(api_key="test-key", api_mode=api_mode)  # type: ignore[arg-type]
        with VerificationWorkspace(
            str(repo), task.verify_command, extra_verifier_files=task.hidden_tests
        ) as ws:
            await sb.start(repo_path=str(ws.workspace), config=sandbox_cfg)
            engine = ContextEngine(ws.workspace, task=task.prompt, policy=policy)
            try:
                await react_loop(
                    run,
                    provider,
                    tools,
                    sb,
                    verify=lambda: ws.verify(
                        sandbox_cfg,
                        sb,
                        sandbox_factory=lambda _c: SubprocessEvalSandbox(),
                    ),
                    context_engine=engine,
                )
            finally:
                await provider.close()
                await sb.stop()
        assert run.state == RunState.COMPLETED
        assert run.verification is not None and run.verification.passed is True
        return state["cont_bytes"], _estimate_cost(run.total_tokens, "gpt-5.4")


@pytest.mark.asyncio
async def test_openai_responses_adapter_matches_or_beats_chat_completions_on_m0_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify OpenAI Responses API matches 4/4 M0 smoke fix rate at lower cost & payload."""
    from agent_forge.eval.corpus import resolve_tasks

    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.core.save_summary", lambda *_a: tmp_path / "s.json")

    tasks = resolve_tasks(suite="smoke")
    resp_runs = [await _run_single_openai_task(task, "responses") for task in tasks]
    chat_runs = [await _run_single_openai_task(task, "chat_completions") for task in tasks]

    assert len(resp_runs) == 4
    assert len(chat_runs) == 4
    assert sum(b for b, _ in resp_runs) < sum(b for b, _ in chat_runs)
    assert sum(c for _, c in resp_runs) < sum(c for _, c in chat_runs)


def _ollama_handler_for_task(task: BenchmarkTask, *, emulated_tools: bool) -> object:
    import httpx

    state = {"turn": 0}
    src_file = next(iter(task.files))

    def _handler(request: httpx.Request) -> httpx.Response:
        state["turn"] += 1
        turn = state["turn"]
        headers = {k.lower(): v for k, v in request.headers.items()}
        assert "authorization" not in headers
        body = json.loads(request.content)
        assert "parallel_tool_calls" not in body

        if emulated_tools:
            assert "tools" not in body
            assert "tool_choice" not in body
            assert "Tool Invocation Format" in body["messages"][0]["content"]
            return httpx.Response(200, json=_emulated_ollama_payload(task, src_file, turn))

        assert body.get("tools")
        return httpx.Response(200, json=_build_chat_mock_payload(task, turn, 200))

    return _handler


def _emulated_ollama_payload(task: BenchmarkTask, src_file: str, turn: int) -> dict[str, object]:
    if turn == 1:
        call = {"name": "read_file", "arguments": {"path": f"/workspace/{src_file}"}}
        text = f"Reading file.\n```tool_call\n{json.dumps(call)}\n```"
    elif turn == 2:
        blocks: list[str] = []
        for path, content in task.known_good_files.items():
            call = {
                "name": "write_file",
                "arguments": {"path": f"/workspace/{path}", "content": content},
            }
            blocks.append(f"```tool_call\n{json.dumps(call)}\n```")
        text = "Applying fix.\n" + "\n".join(blocks)
    else:
        text = "Verified fix complete."
    return {
        "model": "gemma3:12b",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230},
    }


@pytest.mark.asyncio
async def test_local_ollama_config_only_completes_m0_smoke_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify config-only localhost Ollama profile completes an M0 smoke task without API keys."""
    import respx

    from agent_forge.config import load_config
    from agent_forge.eval import run_benchmark
    from agent_forge.eval.corpus import resolve_tasks
    from agent_forge.eval.deterministic import SubprocessEvalSandbox

    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.core.save_summary", lambda *_a: tmp_path / "s.json")
    monkeypatch.setattr(
        "agent_forge.eval.benchmark.create_sandbox",
        lambda _cfg: SubprocessEvalSandbox(),
    )
    monkeypatch.setattr(
        "agent_forge.agent.verification.create_sandbox",
        lambda _cfg: SubprocessEvalSandbox(),
    )
    for env_var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env_var, raising=False)

    toml_path = tmp_path / "agent-forge.toml"
    toml_path.write_text(
        '[model_profiles.local-ollama]\nprovider = "openai-compatible"\n'
        'model = "gemma3:12b"\nbase_url = "http://localhost:11434/v1"\n\n'
        '[model_profiles.local-ollama-no-tools]\nprovider = "openai-compatible"\n'
        'model = "gemma3:12b"\nbase_url = "http://localhost:11434/v1"\n'
        "capabilities = { tool_calling = false }\n",
        encoding="utf-8",
    )
    cfg = load_config(project_path=toml_path, user_path=tmp_path / "none.toml")
    task = resolve_tasks(task_ids=["fix-health-status"])[0]

    with respx.mock(assert_all_called=True) as router:
        router.post("http://localhost:11434/v1/chat/completions").mock(
            side_effect=_ollama_handler_for_task(task, emulated_tools=False)
        )
        report = await run_benchmark(
            cfg,
            profile_names=["local-ollama"],
            task_ids=["fix-health-status"],
            sandbox_backend="docker",
        )
    assert report.profiles[0].status == "completed"
    assert report.profiles[0].verified_fixes == 1

    with respx.mock(assert_all_called=True) as router:
        router.post("http://localhost:11434/v1/chat/completions").mock(
            side_effect=_ollama_handler_for_task(task, emulated_tools=True)
        )
        report_emulated = await run_benchmark(
            cfg,
            profile_names=["local-ollama-no-tools"],
            task_ids=["fix-health-status"],
            sandbox_backend="docker",
        )
    assert report_emulated.profiles[0].status == "completed"
    assert report_emulated.profiles[0].verified_fixes == 1
