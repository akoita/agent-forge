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


def _scope_benchmark_task(task: BenchmarkTask) -> BenchmarkTask:
    prefix = f"{task.id}/"
    return BenchmarkTask(
        id=task.id,
        title=task.title,
        prompt=task.prompt,
        verify_command=f"python3 -m unittest discover -s {task.id}/tests -t {task.id} -v",
        files={f"{prefix}{path}": body for path, body in task.files.items()},
        tests={f"{prefix}{path}": body for path, body in task.tests.items()},
        known_good_files={f"{prefix}{path}": body for path, body in task.known_good_files.items()},
        hidden_tests={f"{prefix}{path}": body for path, body in task.hidden_tests.items()},
        incomplete_files={f"{prefix}{path}": body for path, body in task.incomplete_files.items()},
        suite=task.suite,
    )


def _init_monorepo_from_tasks(repo: Path, scoped_tasks: list[BenchmarkTask]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for scoped in scoped_tasks:
        _write_files(repo, scoped.files)
        _write_files(repo, scoped.tests)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.name", "Eval Harness"],
        ["git", "config", "user.email", "eval@example.com"],
        ["git", "add", "."],
        ["git", "commit", "-m", "base monorepo"],
    ):
        subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True, timeout=15)  # noqa: S603


async def _verify_parallel_worktree_outcomes(
    monorepo: Path,
    wt_root: Path,
    scoped_tasks: list[BenchmarkTask],
    queue: object,
    worker: object,
) -> None:
    import re

    from agent_forge.orchestration import TaskStatus, WorktreeState

    for t in scoped_tasks:
        assert await queue.get_status(t.id) == TaskStatus.COMPLETED  # type: ignore[attr-defined]
    assert await queue.get_status("noop-task") == TaskStatus.FAILED  # type: ignore[attr-defined]

    manager = worker.get_worktree_manager(monorepo)  # type: ignore[attr-defined]
    assert manager is not None
    noop_lease = manager.get_lease("noop-task")
    assert noop_lease is not None
    assert noop_lease.state == WorktreeState.REMOVED

    for t in scoped_tasks:
        lease = manager.get_lease(t.id)
        assert lease is not None
        assert lease.state == WorktreeState.COMMITTED
        touched = re.findall(r"^diff --git a/(\S+) b/", lease.patch, flags=re.MULTILINE)
        assert touched
        assert all(path.startswith(f"{t.id}/") for path in touched), (
            f"Cross-contamination in {t.id}: {touched}"
        )

    integrations = await manager.integrate_all()
    assert [res.status for res in integrations] == ["merged"] * len(scoped_tasks)
    for t in scoped_tasks:
        _write_files(monorepo, t.hidden_tests)
        check = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "unittest", "discover", "-s", f"{t.id}/tests", "-t", t.id, "-v"],
            cwd=monorepo,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert check.returncode == 0, check.stdout + check.stderr

    assert [info.path for info in await manager.list_worktrees()] == [monorepo.resolve()]
    assert list(wt_root.iterdir()) == []
    refs = subprocess.run(
        ["git", "for-each-ref", "refs/heads/agent-forge/run/"],  # noqa: S607
        cwd=monorepo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert refs.stdout.strip() == ""


@pytest.mark.asyncio
async def test_parallel_worktree_benchmark_tasks_zero_cross_contamination(
    tmp_path: Path,
) -> None:
    """N M0 benchmark tasks run concurrently in isolated worktrees without cross-contamination."""
    import asyncio

    from agent_forge.agent.core import react_loop
    from agent_forge.agent.models import AgentConfig, AgentRun
    from agent_forge.agent.verification import VerificationWorkspace
    from agent_forge.eval.corpus import resolve_tasks
    from agent_forge.eval.deterministic import SubprocessEvalSandbox, _ScriptedEvalLLM
    from agent_forge.orchestration import EventBus, InMemoryQueue, Task, TaskStatus, Worker
    from agent_forge.sandbox.base import SandboxConfig
    from agent_forge.tools import create_default_registry

    scoped_tasks = [_scope_benchmark_task(task) for task in resolve_tasks(suite="smoke")]
    monorepo = tmp_path / "monorepo"
    wt_root = tmp_path / "worktrees"
    _init_monorepo_from_tasks(monorepo, scoped_tasks)

    by_id = {task.id: task for task in scoped_tasks}
    tools = create_default_registry(load_plugins=False)
    sandbox_cfg = SandboxConfig(
        backend="subprocess-eval",
        network_enabled=False,
        writable_cache_mounts=False,
        timeout_seconds=20,
    )
    active = 0
    peak_active = 0

    async def _run_isolated(qtask: Task) -> AgentRun:
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        try:
            await asyncio.sleep(0.05)
            is_noop = qtask.id == "noop-task"
            bench_task = scoped_tasks[0] if is_noop else by_id[qtask.id]
            mode = "noop" if is_noop else "oracle"
            run = AgentRun(
                id=qtask.id,
                task=bench_task.prompt,
                repo_path=qtask.repo_path,
                config=AgentConfig(
                    model=f"scripted-{mode}",
                    provider="deterministic",
                    max_iterations=10,
                    max_tokens_per_run=40_000,
                ),
            )
            llm = _ScriptedEvalLLM(bench_task, mode)
            sandbox = SubprocessEvalSandbox()
            with VerificationWorkspace(
                qtask.repo_path,
                bench_task.verify_command,
                extra_verifier_files=bench_task.hidden_tests,
            ) as workspace:
                await sandbox.start(repo_path=str(workspace.workspace), config=sandbox_cfg)
                try:

                    async def _verify() -> object:
                        return await workspace.verify(
                            sandbox_cfg,
                            sandbox,
                            sandbox_factory=lambda _c: SubprocessEvalSandbox(),
                        )

                    await react_loop(run, llm, tools, sandbox, verify=_verify)
                finally:
                    await sandbox.stop()
                if run.verification and run.verification.patch and not is_noop:
                    subprocess.run(
                        ["git", "apply", "-"],  # noqa: S607
                        cwd=qtask.repo_path,
                        input=run.verification.patch,
                        text=True,
                        check=True,
                    )
            return run
        finally:
            active -= 1

    queue = InMemoryQueue()
    worker = Worker(
        queue,
        EventBus(),
        _run_isolated,
        poll_interval=0.01,
        max_concurrent_tasks=5,
        worktree_isolation=True,
        worktree_root=wt_root,
    )
    all_ids = [t.id for t in scoped_tasks] + ["noop-task"]
    for tid in all_ids:
        await queue.enqueue(
            Task(id=tid, task_description=tid, repo_path=str(monorepo), config=None)
        )

    await worker.start()
    for _ in range(300):
        statuses = [await queue.get_status(tid) for tid in all_ids]
        if all(s not in (TaskStatus.QUEUED, TaskStatus.PROCESSING) for s in statuses):
            break
        await asyncio.sleep(0.05)
    await worker.stop()

    assert peak_active >= 2
    await _verify_parallel_worktree_outcomes(monorepo, wt_root, scoped_tasks, queue, worker)


@pytest.mark.asyncio
async def test_aborted_parallel_runs_leave_no_orphan_worktrees(tmp_path: Path) -> None:
    """Cancelled, crashed, and cancelled-state parallel runs leave zero orphan worktrees."""
    import asyncio

    from agent_forge.agent.models import AgentConfig, AgentRun, RunState
    from agent_forge.eval.corpus import resolve_tasks
    from agent_forge.orchestration import EventBus, InMemoryQueue, Task, Worker

    tasks = resolve_tasks(suite="smoke")
    repo = materialize_task_repo(tasks[0], tmp_path / "repo")
    wt_root = tmp_path / "worktrees"
    slow_started = 0
    both_slow = asyncio.Event()

    async def _runner(qtask: Task) -> AgentRun:
        nonlocal slow_started
        wt = Path(qtask.repo_path)
        (wt / f"scratch_{qtask.id}.txt").write_text("partial change\n", encoding="utf-8")
        if qtask.id.startswith("slow-"):
            slow_started += 1
            if slow_started >= 2:
                both_slow.set()
            await asyncio.sleep(10)
        if qtask.id == "raise-1":
            msg = "unexpected crash"
            raise RuntimeError(msg)
        return AgentRun(
            id=qtask.id,
            task=qtask.task_description,
            repo_path=qtask.repo_path,
            config=AgentConfig(),
            state=RunState.CANCELLED,
        )

    queue = InMemoryQueue()
    bus = EventBus()
    worker = Worker(
        queue,
        bus,
        _runner,
        poll_interval=0.01,
        max_concurrent_tasks=4,
        worktree_isolation=True,
        worktree_root=wt_root,
    )
    for tid in ("slow-1", "slow-2", "raise-1", "cancelled-state-1"):
        await queue.enqueue(Task(id=tid, task_description=tid, repo_path=str(repo), config=None))

    await worker.start()
    await asyncio.wait_for(both_slow.wait(), timeout=3.0)
    await asyncio.sleep(0.1)
    await worker.stop()

    manager = worker.get_worktree_manager(repo)
    assert manager is not None
    assert [info.path for info in await manager.list_worktrees()] == [repo.resolve()]
    assert list(wt_root.iterdir()) == []
    refs = subprocess.run(
        ["git", "for-each-ref", "refs/heads/agent-forge/run/"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert refs.stdout.strip() == ""


@pytest.mark.asyncio
async def test_deterministic_eval_parallel_concurrency_matches_gates() -> None:
    """Running deterministic eval with max_concurrency > 1 preserves order and passes gates."""
    report = await run_deterministic_eval(suite="smoke", max_concurrency=4)
    assert check_deterministic_gates(report) == []
    oracle = next(p for p in report.profiles if p.profile == "deterministic-oracle")
    assert [res.task_id for res in oracle.results] == report.tasks


def _build_mcp_eval_server_script(tmp_path: Path) -> Path:
    code = (
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1]).resolve()\n"
        "mode = sys.argv[2]\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    req = json.loads(line)\n"
        "    rid = req.get('id')\n"
        "    if rid is None:\n"
        "        continue\n"
        "    m = req.get('method')\n"
        "    p = req.get('params') or {}\n"
        "    if m == 'initialize':\n"
        "        res = {'protocolVersion': '2025-03-26', 'capabilities': {}}\n"
        "    elif m == 'tools/list':\n"
        "        if mode == 'slow':\n"
        "            res = {'tools': [{'name': 'ping', 'description': 'Slow ping', "
        "'inputSchema': {'type': 'object', 'properties': {}}}]}\n"
        "        else:\n"
        "            res = {'tools': [\n"
        "                {'name': 'read_file_ext', 'description': 'Read', "
        "'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}}}, "
        "'annotations': {'readOnlyHint': True}},\n"
        "                {'name': 'write_file_ext', 'description': 'Write', "
        "'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, "
        "'content': {'type': 'string'}}}, 'annotations': {'destructiveHint': True}}\n"
        "            ]}\n"
        "    elif m == 'tools/call':\n"
        "        if mode == 'slow':\n"
        "            time.sleep(3.0)\n"
        "            res = {'content': [{'type': 'text', 'text': 'ok'}]}\n"
        "        elif p.get('name') == 'read_file_ext':\n"
        "            txt = (root / p['arguments']['path']).read_text(encoding='utf-8')\n"
        "            res = {'content': [{'type': 'text', 'text': txt}]}\n"
        "        else:\n"
        "            (root / p['arguments']['path']).write_text("
        "p['arguments']['content'], encoding='utf-8')\n"
        "            res = {'content': [{'type': 'text', 'text': 'updated'}]}\n"
        "    else:\n"
        "        res = {}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': res}), flush=True)\n"
    )
    script = tmp_path / "eval_mcp_server.py"
    script.write_text(code, encoding="utf-8")
    return script


@pytest.mark.asyncio
async def test_mcp_filesystem_server_usable_in_run_with_policy_gated_approvals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit criterion: stdio MCP server works in run_agent_task with approvals + isolation."""
    from agent_forge.agent.approvals import ApprovalGate, ApprovalRequest
    from agent_forge.agent.models import AgentConfig, RunState
    from agent_forge.agent.runner import run_agent_task
    from agent_forge.config import load_config
    from agent_forge.eval.deterministic import SubprocessEvalSandbox
    from agent_forge.llm.base import (
        LLMConfig,
        LLMProvider,
        LLMResponse,
        Message,
        TokenUsage,
        ToolCall,
    )
    from agent_forge.orchestration.events import Event, EventBus, EventType

    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr("agent_forge.agent.core.save_summary", lambda *_a: tmp_path / "s.json")

    script = _build_mcp_eval_server_script(tmp_path)
    fs_root = tmp_path / "mcp_fs"
    fs_root.mkdir()
    (fs_root / "status.txt").write_text("initial", encoding="utf-8")

    toml_path = tmp_path / "agent-forge.toml"
    toml_path.write_text(
        f'[mcp.servers.fs]\ntransport = "stdio"\ncommand = "{sys.executable}"\n'
        f'args = ["{script}", "{fs_root}", "fs"]\n'
        'approval_mode = "prompt"\nallow_read_only_without_approval = true\n\n'
        f'[mcp.servers.slowsrv]\ntransport = "stdio"\ncommand = "{sys.executable}"\n'
        f'args = ["{script}", "{fs_root}", "slow"]\n'
        "timeout_seconds = 0.2\nfailure_threshold = 1\n",
        encoding="utf-8",
    )
    cfg = load_config(project_path=toml_path, user_path=tmp_path / "none.toml")

    seen_prompts: list[str] = []
    seen_tools_in_call: list[str] = []

    class _RunEvalLLM(LLMProvider):
        def __init__(self) -> None:
            self._turn = 0
            self._responses = [
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="m1",
                            name="mcp__fs__read_file_ext",
                            arguments={"path": "status.txt"},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="m2",
                            name="mcp__fs__write_file_ext",
                            arguments={"path": "status.txt", "content": "blocked"},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="m3",
                            name="mcp__fs__write_file_ext",
                            arguments={"path": "status.txt", "content": "approved-update"},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="m4", name="mcp__slowsrv__ping", arguments={}),
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="m5", name="mcp__slowsrv__ping", arguments={}),
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content="Completed MCP tasks.",
                    tool_calls=[],
                    finish_reason="stop",
                    usage=TokenUsage(20, 10, 30),
                ),
            ]

        async def complete(
            self,
            messages: list[Message],
            tools: list[object] | None = None,
            config: LLMConfig | None = None,
        ) -> LLMResponse:
            del config
            if messages and messages[0].role.value == "system":
                seen_prompts.append(messages[0].content or "")
            if tools and not seen_tools_in_call:
                seen_tools_in_call.extend(getattr(t, "name", "") for t in tools)
            resp = self._responses[min(self._turn, len(self._responses) - 1)]
            self._turn += 1
            return resp

        async def stream(self, messages: list[Message], **kwargs: object) -> object:
            del kwargs
            yield await self.complete(messages)

    async def _policy_callback(req: ApprovalRequest) -> bool:
        if req.tool_name == "mcp__fs__write_file_ext":
            return req.arguments.get("content") == "approved-update"
        return True

    decisions: list[Event] = []
    bus = EventBus()

    async def _on_decision(event: Event) -> None:
        decisions.append(event)

    await bus.subscribe(EventType.APPROVAL_DECIDED, _on_decision)

    run = await run_agent_task(
        "Use external MCP tools safely",
        str(fs_root),
        cfg,
        AgentConfig(model="eval-mock", provider="deterministic", max_iterations=8),
        "deterministic",
        "",
        discover_extensions=False,
        event_bus=bus,
        approval_gate=ApprovalGate(mode="prompt", callback=_policy_callback),
        sandbox_factory=lambda _c: SubprocessEvalSandbox(),
        provider_factory=lambda _p, _k: _RunEvalLLM(),
    )

    assert run.state == RunState.COMPLETED
    assert "mcp__fs__read_file_ext" in seen_tools_in_call
    assert "mcp__fs__write_file_ext" in seen_tools_in_call
    assert (fs_root / "status.txt").read_text(encoding="utf-8") == "approved-update"
    assert [d.data.get("approved") for d in decisions].count(False) == 1
    assert any(
        inv.tool_name == "mcp__slowsrv__ping" and inv.result.metadata.get("mcp_circuit_open")
        for inv in run.tool_invocations
    )


def _build_acp_eval_llm(task: BenchmarkTask) -> object:
    from agent_forge.llm.base import (
        LLMConfig,
        LLMProvider,
        LLMResponse,
        Message,
        TokenUsage,
        ToolCall,
    )

    class _ACPEditorEvalLLM(LLMProvider):
        def __init__(self) -> None:
            self._turn = 0
            self._turns = [
                LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="a1", name="read_file", arguments={"path": "app.py"})],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="a2", name="run_shell", arguments={"command": "rm -f scratch.tmp"}
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="a3", name="run_shell", arguments={"command": "rm -f scratch.tmp"}
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="a4",
                            name="write_file",
                            arguments={
                                "path": "app.py",
                                "content": task.known_good_files["app.py"],
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content="Fixed app.py health handler.",
                    tool_calls=[],
                    finish_reason="stop",
                    usage=TokenUsage(20, 10, 30),
                ),
            ]

        async def complete(
            self,
            messages: list[Message],
            tools: list[object] | None = None,
            config: LLMConfig | None = None,
        ) -> LLMResponse:
            del messages, tools, config
            resp = self._turns[min(self._turn, len(self._turns) - 1)]
            self._turn += 1
            return resp

        async def stream(self, messages: list[Message], **kwargs: object) -> object:
            del kwargs
            yield await self.complete(messages)

    return _ACPEditorEvalLLM()


async def _drive_acp_eval_turn(
    reader: object,
    writer: object,
    sid: str,
    prompt: str,
) -> tuple[list[dict[str, object]], int, dict[str, object]]:
    import asyncio

    await reader.send(  # type: ignore[attr-defined]
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
        }
    )
    updates: list[dict[str, object]] = []
    perm_count = 0
    while True:
        msg = await asyncio.wait_for(writer.msgs.get(), timeout=10.0)  # type: ignore[attr-defined]
        if msg.get("method") == "session/update":
            updates.append(msg["params"]["update"])
        elif msg.get("method") == "session/request_permission":
            perm_count += 1
            opt = "reject_once" if perm_count == 1 else "allow_once"
            await reader.send(  # type: ignore[attr-defined]
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": opt}},
                }
            )
        elif msg.get("id") == 3:
            return updates, perm_count, msg


class _EvalPipeReader:
    def __init__(self) -> None:
        import asyncio

        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    async def send(self, obj: dict[str, object]) -> None:
        await self._q.put((json.dumps(obj) + "\n").encode("utf-8"))

    async def close(self) -> None:
        await self._q.put(b"")

    async def readline(self) -> bytes:
        return await self._q.get()


class _EvalPipeWriter:
    def __init__(self) -> None:
        import asyncio

        self.msgs: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        while b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            text = line.decode("utf-8").strip()
            if text:
                self.msgs.put_nowait(json.loads(text))

    async def drain(self) -> None:
        import asyncio

        await asyncio.sleep(0)

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_acp_editor_session_streams_progress_and_approves_gated_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit criterion: ACP editor starts run, watches stream updates, and approves gated action."""
    import asyncio
    from unittest.mock import patch

    from agent_forge.acp import ACP_PROTOCOL_VERSION, ACPServer
    from agent_forge.eval.corpus import resolve_tasks
    from agent_forge.eval.deterministic import SubprocessEvalSandbox

    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr("agent_forge.agent.core.save_summary", lambda *_a: tmp_path / "s.json")
    monkeypatch.setenv("GEMINI_API_KEY", "test-acp-key")

    task = resolve_tasks(task_ids=["fix-health-status"])[0]
    repo = materialize_task_repo(task, tmp_path / "repo")
    scratch = repo / "scratch.tmp"
    scratch.write_text("to-remove", encoding="utf-8")

    reader = _EvalPipeReader()
    writer = _EvalPipeWriter()
    server = ACPServer(
        approval_mode="prompt",
        sandbox_factory=lambda _c: SubprocessEvalSandbox(),
        provider_factory=lambda _p, _k: _build_acp_eval_llm(task),
    )

    with patch("agent_forge.agent.verification.create_sandbox", lambda _c: SubprocessEvalSandbox()):
        srv_task = asyncio.create_task(server.serve(reader, writer))
        await reader.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        init_msg = await writer.msgs.get()
        assert init_msg["result"]["protocolVersion"] == ACP_PROTOCOL_VERSION  # type: ignore[index]

        await reader.send(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(repo)}}
        )
        sid = (await writer.msgs.get())["result"]["sessionId"]  # type: ignore[index]
        updates, perm_count, prompt_result = await _drive_acp_eval_turn(
            reader, writer, sid, task.prompt
        )
        await reader.close()
        await srv_task

    assert perm_count == 2
    assert prompt_result["result"]["stopReason"] == "end_turn"  # type: ignore[index]
    assert not scratch.exists()
    assert any(u.get("sessionUpdate") == "tool_call" for u in updates)
    assert any(u.get("sessionUpdate") == "tool_call_update" for u in updates)
    assert any(u.get("sessionUpdate") == "agent_message_chunk" for u in updates)

    _write_files(repo, task.hidden_tests)
    check = _run_unittest(repo)
    assert check.returncode == 0, check.stdout + check.stderr


def _setup_offline_push_eval_repo(tmp_path: Path, task: BenchmarkTask) -> tuple[Path, Path]:
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=bare, check=True)  # noqa: S607
    repo = materialize_task_repo(task, tmp_path / "repo")
    for cmd in (
        ["git", "branch", "-M", "main"],
        ["git", "remote", "add", "origin", str(bare)],
        ["git", "push", "-u", "origin", "main"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)  # noqa: S603
    return repo, bare


@pytest.mark.asyncio
async def test_host_brokered_git_push_from_offline_sandbox_with_approval_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit criterion: agent pushes feature branch from offline sandbox via ApprovalGate."""
    from agent_forge.agent.approvals import ApprovalGate, ApprovalRequest
    from agent_forge.agent.models import AgentConfig, RunState
    from agent_forge.agent.runner import run_agent_task
    from agent_forge.auth import CredentialStore
    from agent_forge.config import load_config
    from agent_forge.eval.corpus import resolve_tasks
    from agent_forge.eval.deterministic import SubprocessEvalSandbox
    from agent_forge.llm.base import (
        LLMConfig,
        LLMProvider,
        LLMResponse,
        Message,
        TokenUsage,
        ToolCall,
    )

    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr("agent_forge.agent.core.save_summary", lambda *_a: tmp_path / "s.json")

    CredentialStore().set("github", "ghp_eval_host_only_9999", host="github.com")
    task = resolve_tasks(task_ids=["fix-health-status"])[0]
    repo, bare = _setup_offline_push_eval_repo(tmp_path, task)

    class _PushFlowLLM(LLMProvider):
        def __init__(self) -> None:
            self._turn = 0
            self._turns = [
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="p1",
                            name="git_create_branch",
                            arguments={"branch_name": "feat/m7-auth-fix"},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="p2",
                            name="write_file",
                            arguments={
                                "path": "app.py",
                                "content": task.known_good_files["app.py"],
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="p3", name="run_shell", arguments={"command": "git add app.py"})
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="p4",
                            name="git_commit",
                            arguments={"message": "fix: healthy endpoint"},
                        )
                    ],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="p5", name="git_push", arguments={})],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="p6", name="git_push", arguments={})],
                    finish_reason="tool_calls",
                    usage=TokenUsage(20, 10, 30),
                ),
                LLMResponse(
                    content="Committed and pushed feat/m7-auth-fix.",
                    tool_calls=[],
                    finish_reason="stop",
                    usage=TokenUsage(20, 10, 30),
                ),
            ]

        async def complete(
            self,
            messages: list[Message],
            tools: list[object] | None = None,
            config: LLMConfig | None = None,
        ) -> LLMResponse:
            del messages, tools, config
            resp = self._turns[min(self._turn, len(self._turns) - 1)]
            self._turn += 1
            return resp

        async def stream(self, messages: list[Message], **kwargs: object) -> object:
            del kwargs
            yield await self.complete(messages)

    seen_pushes = 0

    async def _gate_cb(req: ApprovalRequest) -> bool:
        nonlocal seen_pushes
        if req.tool_name == "git_push":
            seen_pushes += 1
            return seen_pushes >= 2
        return True

    cfg = load_config(project_path=tmp_path / "none.toml", user_path=tmp_path / "none.toml")
    run = await run_agent_task(
        "Branch, fix, commit, and push",
        str(repo),
        cfg,
        AgentConfig(model="eval-mock", provider="deterministic", max_iterations=10),
        "deterministic",
        "",
        discover_extensions=False,
        approval_gate=ApprovalGate(mode="prompt", callback=_gate_cb),
        sandbox_factory=lambda _c: SubprocessEvalSandbox(),
        provider_factory=lambda _p, _k: _PushFlowLLM(),
    )

    assert run.state == RunState.COMPLETED
    assert seen_pushes == 2
    pushed_sha = subprocess.run(
        ["git", "rev-parse", "refs/heads/feat/m7-auth-fix"],  # noqa: S607
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    local_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert pushed_sha == local_sha
