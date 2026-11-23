"""Unit tests for config-driven model profiles and the benchmark harness (#155)."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from click.testing import CliRunner

from agent_forge.agent.models import AgentConfig, AgentRun, RunState, ToolInvocation
from agent_forge.agent.verification import VerificationResult, VerificationWorkspace
from agent_forge.cli import main as cli_main
from agent_forge.config import ForgeConfig, ModelProfileSettings
from agent_forge.eval import (
    BUILTIN_TASKS,
    BenchmarkReport,
    TaskAttemptResult,
    render_markdown_report,
    resolve_tasks,
    run_benchmark,
    save_attempt_trajectory,
    summarize_profile,
)
from agent_forge.eval.deterministic import SubprocessEvalSandbox
from agent_forge.llm.base import LLMConfig, LLMResponse, Message, Role, TokenUsage
from agent_forge.llm.qualification import probe_model, resolve_model_profile
from agent_forge.sandbox.base import SandboxConfig
from agent_forge.tools.base import ToolResult

if TYPE_CHECKING:
    from pathlib import Path


def _runner() -> CliRunner:
    kwargs: dict[str, Any] = {}
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        kwargs["mix_stderr"] = False
    return CliRunner(**kwargs)


class TestConfigDrivenModelProfiles:
    """Verify base_url, api_key_env, and temperature overrides on model profiles."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_probe_openai_compatible_profile_uses_custom_base_url_and_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOGETHER_API_KEY", "together-secret")
        route = respx.post("https://api.together.xyz/v1/chat/completions")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_meta_1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_probe_value",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "model": "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "forge-probe-7391"}}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ),
        ]
        profile = ModelProfileSettings(
            provider="openai",
            model="meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
            base_url="https://api.together.xyz/v1",
            api_key_env="TOGETHER_API_KEY",
            temperature=0.2,
        )
        cfg = ForgeConfig(model_profiles={"meta-llama": profile})
        resolved = resolve_model_profile(cfg, "meta-llama")
        assert resolved.api_key_env == "TOGETHER_API_KEY"

        res = await probe_model(cfg, resolved)
        assert res.status == "passed"
        assert res.reported_models == ["meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"]
        body = json.loads(route.calls.last.request.content)
        assert body["temperature"] == pytest.approx(0.2)

    def test_run_applies_profile_api_key_env_base_url_and_temperature(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "agent-forge.toml").write_text(
            "[model_profiles.custom]\n"
            'provider = "openai"\n'
            'model = "custom-llama"\n'
            'base_url = "https://example.invalid/v1"\n'
            'api_key_env = "CUSTOM_MODEL_KEY"\n'
            "temperature = 0.15\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CUSTOM_MODEL_KEY", "custom-key-value")
        with (
            patch("agent_forge.cli._run_agent") as run_mock,
            patch("agent_forge.cli._emit_run_output"),
        ):
            result = _runner().invoke(
                cli_main,
                ["run", "--task", "Fix bug", "--repo", ".", "--model-profile", "custom"],
            )
        assert result.exit_code == 0, result.output
        cfg_passed = run_mock.call_args.args[2]
        assert cfg_passed.agent.default_model == "custom-llama"
        assert cfg_passed.agent.temperature == pytest.approx(0.15)
        assert cfg_passed.providers["openai"].base_url == "https://example.invalid/v1"
        assert run_mock.call_args.args[4] == "custom-key-value"


class TestBenchmarkAggregationAndRunner:
    """Verify summary math (including failed runs), skips, and CLI reporting."""

    def test_summarize_profile_includes_failed_run_costs_and_latency(self) -> None:
        profile = ModelProfileSettings(provider="gemini", model="gemini-3.1-flash-lite-preview")
        attempts = [
            TaskAttemptResult(
                task_id="fix-health-status",
                attempt=1,
                status="passed",
                verified=True,
                latency_seconds=4.0,
                iterations=2,
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                estimated_cost_usd=0.002,
                tool_calls=2,
                tool_errors=0,
                reported_model="gemini-3.1-flash-lite",
            ),
            TaskAttemptResult(
                task_id="fix-pagination-offset",
                attempt=1,
                status="failed",
                verified=False,
                latency_seconds=10.0,
                iterations=5,
                prompt_tokens=300,
                completion_tokens=100,
                total_tokens=400,
                estimated_cost_usd=0.004,
                tool_calls=6,
                tool_errors=2,
                reported_model="gemini-3.1-flash-lite",
                reason="check_failed",
            ),
            TaskAttemptResult(
                task_id="fix-retry-backoff",
                attempt=1,
                status="error",
                verified=False,
                latency_seconds=1.0,
                iterations=1,
                prompt_tokens=50,
                completion_tokens=0,
                total_tokens=50,
                estimated_cost_usd=0.0005,
                tool_calls=0,
                tool_errors=0,
                reason="TimeoutError",
            ),
        ]
        summary = summarize_profile(
            "gemini-flash-lite",
            profile,
            attempts,
            temperature=0.0,
            max_iterations=8,
            max_tokens_per_run=40_000,
        )
        assert summary.status == "completed"
        assert summary.attempts == 3
        assert summary.verified_fixes == 1
        assert summary.verified_rate == pytest.approx(0.3333)
        assert summary.mean_latency_seconds == pytest.approx(5.0)
        assert summary.median_latency_seconds == pytest.approx(4.0)
        assert summary.total_cost_usd == pytest.approx(0.0065)
        # Includes costs from failed and errored runs in cost per verified fix
        assert summary.cost_per_verified_fix_usd == pytest.approx(0.0065)
        assert summary.tool_error_rate == pytest.approx(2 / 8)

    @pytest.mark.asyncio
    async def test_run_benchmark_skips_missing_credentials_and_records_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = ForgeConfig(
            model_profiles={
                "gemini-candidate": ModelProfileSettings(
                    provider="gemini",
                    model="gemini-3.1-flash-lite-preview",
                ),
                "openai-candidate": ModelProfileSettings(provider="openai", model="gpt-5.4"),
            }
        )

        class _FakeProvider:
            async def complete(
                self,
                messages: object,
                tools: object = None,
                config: LLMConfig | None = None,
            ) -> LLMResponse:
                del messages, tools, config
                return LLMResponse(
                    content="done",
                    usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
                    finish_reason="stop",
                    reported_model="gemini-3.1-flash-lite",
                )

            def stream(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                return iter(())

            async def close(self) -> None:
                return None

        fake_sandbox = MagicMock()
        fake_sandbox.start = AsyncMock()
        fake_sandbox.stop = AsyncMock()

        async def _fake_react_loop(
            run: AgentRun,
            llm: Any,
            tools: object,
            sandbox: object,
            verify: Any = None,
        ) -> AgentRun:
            del tools, sandbox, verify
            await llm.complete([], None, None)
            run.iterations = 2
            run.total_tokens = TokenUsage(prompt_tokens=100, completion_tokens=40, total_tokens=140)
            run.tool_invocations.append(
                ToolInvocation(
                    tool_name="read_file",
                    arguments={"path": "app.py"},
                    result=ToolResult(output="ok", exit_code=0),
                    iteration=1,
                    timestamp=datetime.now(UTC),
                    duration_ms=5,
                )
            )
            if "health endpoint handler" in run.task:
                run.state = RunState.COMPLETED
                run.verification = VerificationResult("base", "diff", "verified", "unittest", 0)
            else:
                run.state = RunState.FAILED
                run.verification = VerificationResult("base", "", "empty_patch", "unittest", None)
            return run

        with (
            patch("agent_forge.eval.benchmark.create_provider", return_value=_FakeProvider()),
            patch("agent_forge.eval.benchmark.create_sandbox", return_value=fake_sandbox),
            patch("agent_forge.eval.benchmark.react_loop", side_effect=_fake_react_loop),
        ):
            report = await run_benchmark(
                cfg,
                task_ids=["fix-health-status", "fix-retry-backoff"],
                attempts=1,
                sandbox_backend="bwrap",
            )

        assert [p.profile for p in report.profiles] == ["gemini-candidate", "openai-candidate"]
        gemini_summary, openai_summary = report.profiles
        assert openai_summary.status == "skipped_missing_credentials"
        assert "OPENAI_API_KEY" in (openai_summary.skip_reason or "")
        assert gemini_summary.status == "completed"
        assert gemini_summary.verified_fixes == 1
        assert gemini_summary.attempts == 2
        assert gemini_summary.total_cost_usd > 0
        assert gemini_summary.cost_per_verified_fix_usd == pytest.approx(
            gemini_summary.total_cost_usd
        )
        assert gemini_summary.reported_models == ["gemini-3.1-flash-lite"]

        markdown = render_markdown_report(report)
        assert "gemini-3.1-flash-lite-preview" in markdown
        assert "skipped (Missing credential variable OPENAI_API_KEY.)" in markdown

    def test_cli_models_benchmark_writes_json_and_md_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "agent-forge.toml").write_text(
            '[model_profiles.gemini-candidate]\nprovider = "gemini"\nmodel = "gemini-2.5-flash"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        fake_report = BenchmarkReport(
            generated_at="2026-11-22T16:00:00+00:00",
            harness_revision="abc1234",
            sandbox_backend="bwrap",
            tasks=["fix-health-status"],
            profiles=[
                summarize_profile(
                    "gemini-candidate",
                    ModelProfileSettings(provider="gemini", model="gemini-2.5-flash"),
                    [],
                    temperature=0.0,
                    max_iterations=8,
                    max_tokens_per_run=40_000,
                    skip_reason="Missing credential variable GEMINI_API_KEY.",
                )
            ],
        )
        json_out = tmp_path / "out" / "report.json"
        md_out = tmp_path / "out" / "report.md"
        with patch("agent_forge.eval.run_benchmark", AsyncMock(return_value=fake_report)):
            res = _runner().invoke(
                cli_main,
                [
                    "models",
                    "benchmark",
                    "--profile",
                    "gemini-candidate",
                    "--task",
                    "fix-health-status",
                    "--output-json",
                    str(json_out),
                    "--output-md",
                    str(md_out),
                ],
            )
        assert res.exit_code == 0, res.output
        payload = json.loads(res.stdout)
        assert payload["schema_version"] == "benchmark-report-v1"
        assert json.loads(json_out.read_text(encoding="utf-8")) == payload
        assert "# Model Benchmark Report" in md_out.read_text(encoding="utf-8")

    def test_corpus_tiers_and_cli_suite_and_trajectory_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        all_tasks = resolve_tasks()
        smoke_tasks = resolve_tasks(suite="smoke")
        assert len(BUILTIN_TASKS) == 12
        assert len(all_tasks) == 12
        assert [t.id for t in smoke_tasks] == [
            "fix-health-status",
            "fix-pagination-offset",
            "fix-multi-file-summary",
            "fix-retry-backoff",
        ]

        (tmp_path / "agent-forge.toml").write_text(
            '[model_profiles.gemini-candidate]\nprovider = "gemini"\nmodel = "gemini-2.5-flash"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        traj_dir = tmp_path / "trajectories"
        fake_report = BenchmarkReport(
            generated_at="2026-11-23T00:00:00+00:00",
            harness_revision="abc1234",
            sandbox_backend="bwrap",
            tasks=[t.id for t in smoke_tasks],
            profiles=[],
        )
        mock_bench = AsyncMock(return_value=fake_report)
        with patch("agent_forge.eval.run_benchmark", mock_bench):
            res = _runner().invoke(
                cli_main,
                ["models", "benchmark", "--suite", "smoke", "--trajectory-dir", str(traj_dir)],
            )
        assert res.exit_code == 0, res.output
        kwargs = mock_bench.await_args.kwargs
        assert kwargs["suite"] == "smoke"
        assert kwargs["trajectory_dir"] == traj_dir

    @pytest.mark.asyncio
    async def test_verification_workspace_extra_verifier_files_and_path_guard(
        self, tmp_path: Path
    ) -> None:
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return 0\n")
        for cmd in (
            ["git", "init"],
            ["git", "config", "user.name", "Test"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "add", "."],
            ["git", "commit", "-m", "init"],
        ):
            subprocess.run(cmd, cwd=repo, check=True, capture_output=True)  # noqa: S603

        cfg = SandboxConfig(backend="subprocess-eval", timeout_seconds=10)
        with VerificationWorkspace(
            str(repo),
            "python3 -c 'import calc; assert calc.add(2, 3) == 5'",
            extra_verifier_files={"tests/hidden.txt": "present"},
        ) as ws:
            sb = SubprocessEvalSandbox()
            await sb.start(str(ws.workspace), cfg)
            await sb.write_file(
                "/workspace/calc.py", "def add(a: int, b: int) -> int:\n    return a + b\n"
            )
            res = await ws.verify(cfg, sb, sandbox_factory=lambda _c: SubprocessEvalSandbox())
        assert res.passed
        assert res.reason == "verified"

        with VerificationWorkspace(
            str(repo),
            "python3 -c 'import sys; sys.exit(0)'",
            extra_verifier_files={"../escape.txt": "bad"},
        ) as bad_ws:
            sb2 = SubprocessEvalSandbox()
            await sb2.start(str(bad_ws.workspace), cfg)
            await sb2.write_file(
                "/workspace/calc.py", "def add(a: int, b: int) -> int:\n    return a + b\n"
            )
            bad_res = await bad_ws.verify(
                cfg, sb2, sandbox_factory=lambda _c: SubprocessEvalSandbox()
            )
        assert not bad_res.passed
        assert bad_res.reason == "verification_error"
        assert "escapes verifier checkout" in bad_res.stderr

    def test_save_attempt_trajectory_writes_jsonl(self, tmp_path: Path) -> None:
        run = AgentRun(
            task="Fix bug",
            repo_path=str(tmp_path),
            config=AgentConfig(model="test-model"),
            iterations=1,
            messages=[
                Message(role=Role.USER, content="Fix bug"),
                Message(role=Role.ASSISTANT, content="Done"),
            ],
            tool_invocations=[
                ToolInvocation(
                    tool_name="read_file",
                    arguments={"path": "/workspace/app.py"},
                    result=ToolResult(output="ok", exit_code=0, execution_time_ms=3),
                    iteration=1,
                    timestamp=datetime.now(UTC),
                    duration_ms=3,
                )
            ],
            verification=VerificationResult(
                base_revision="deadbeef",
                patch="diff --git a/app.py b/app.py\n",
                reason="verified",
                command="pytest",
                exit_code=0,
            ),
        )
        attempt = TaskAttemptResult(
            task_id="fix-health-status",
            attempt=1,
            status="passed",
            verified=True,
            latency_seconds=0.5,
            iterations=1,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            estimated_cost_usd=0.0,
            tool_calls=1,
            tool_errors=0,
            patch_applied=True,
            run_id=run.id,
            reason="verified",
        )
        out = save_attempt_trajectory(
            tmp_path / "traj" / "sample.jsonl",
            profile="p1",
            task_id="fix-health-status",
            attempt=1,
            run=run,
            result=attempt,
        )
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        assert [row["kind"] for row in rows] == [
            "run_header",
            "message",
            "message",
            "tool_invocation",
            "verification",
            "attempt_result",
        ]
        assert rows[-1]["patch_applied"] is True
