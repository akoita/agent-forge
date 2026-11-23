"""Pinned coding benchmark suite and runner for comparing model profiles."""

from __future__ import annotations

import asyncio
import inspect
import os
import statistics
import subprocess
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_forge.agent.context import ContextEngine, ContextPolicy
from agent_forge.agent.core import react_loop
from agent_forge.agent.journal import (
    RunJournal,
    compute_tool_idempotency_key,
    is_side_effecting_tool,
)
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.verification import VerificationWorkspace
from agent_forge.eval.corpus import (
    BUILTIN_TASKS,
    VERIFY_CMD,
    BenchmarkTask,
    resolve_tasks,
)
from agent_forge.llm.base import LLMProvider, TokenUsage
from agent_forge.llm.capabilities import ProviderCapabilities, query_provider_capabilities
from agent_forge.llm.factory import create_provider
from agent_forge.llm.qualification import (
    resolve_model_profile,
    resolve_profile_base_url,
    resolve_profile_capabilities,
    resolve_profile_credentials,
)
from agent_forge.observability.cost import COST_TABLE, _estimate_cost
from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.factory import create_sandbox
from agent_forge.tools import create_default_registry

__all__ = [
    "BUILTIN_TASKS",
    "VERIFY_CMD",
    "BenchmarkReport",
    "BenchmarkTask",
    "ProfileBenchmarkSummary",
    "TaskAttemptResult",
    "materialize_task_repo",
    "render_markdown_report",
    "resolve_tasks",
    "run_benchmark",
    "save_attempt_trajectory",
    "summarize_profile",
]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_forge.config import ForgeConfig, ModelProfileSettings
    from agent_forge.llm.base import LLMConfig, LLMResponse, Message, ToolDefinition


class TaskAttemptResult(BaseModel):
    """Measured outcome from one benchmark task attempt."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    attempt: int = Field(ge=1)
    status: Literal["passed", "failed", "error"]
    verified: bool
    latency_seconds: float = Field(ge=0.0)
    iterations: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0.0)
    tool_calls: int = Field(ge=0)
    tool_errors: int = Field(ge=0)
    patch_applied: bool = False
    run_state: str = ""
    unmet_clauses: list[str] = Field(default_factory=list)
    trajectory_path: str | None = None
    run_id: str | None = None
    reported_model: str | None = None
    reason: str = ""


class ProfileBenchmarkSummary(BaseModel):
    """Aggregate benchmark metrics across all task attempts for one profile."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    provider: str
    requested_model: str
    reported_models: list[str]
    status: Literal["completed", "skipped_missing_credentials"]
    skip_reason: str | None = None
    temperature: float
    max_iterations: int
    max_tokens_per_run: int
    attempts: int = Field(ge=0)
    verified_fixes: int = Field(ge=0)
    verified_rate: float = Field(ge=0.0)
    mean_latency_seconds: float = Field(ge=0.0)
    median_latency_seconds: float = Field(ge=0.0)
    total_tokens: int = Field(ge=0)
    total_cost_usd: float = Field(ge=0.0)
    cost_per_verified_fix_usd: float | None = None
    tool_error_rate: float = Field(ge=0.0)
    false_positive_rate: float = Field(default=0.0, ge=0.0)
    results: list[TaskAttemptResult]


class BenchmarkReport(BaseModel):
    """Complete multi-profile benchmark run report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["benchmark-report-v1"] = "benchmark-report-v1"
    generated_at: str
    harness_revision: str
    sandbox_backend: str
    tasks: list[str]
    profiles: list[ProfileBenchmarkSummary]


class _RecordingLLM(LLMProvider):
    """Thin LLMProvider wrapper that records API-reported model versions."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.reported_models: list[str] = []

    def capabilities(self, model: str | None = None) -> ProviderCapabilities:
        """Forward runtime capability queries to the wrapped provider."""
        return query_provider_capabilities(self._inner, model)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        resp = await self._inner.complete(messages, tools=tools, config=config)
        if resp.reported_model and resp.reported_model not in self.reported_models:
            self.reported_models.append(resp.reported_model)
        return resp

    def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        return self._inner.stream(messages, tools=tools, config=config)

    async def close(self) -> None:
        await self._inner.close()


def _git_init(directory: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    commands = [
        ["git", "init"],
        ["git", "config", "user.name", "Benchmark Fixture"],
        ["git", "config", "user.email", "benchmark@example.com"],
        ["git", "add", "."],
        ["git", "commit", "-m", "base"],
    ]
    for cmd in commands:
        subprocess.run(cmd, cwd=directory, env=env, check=True, capture_output=True, timeout=15)  # noqa: S603


def materialize_task_repo(task: BenchmarkTask, destination: Path) -> Path:
    """Write *task* source and test files into a clean Git repo at *destination*."""
    destination.mkdir(parents=True, exist_ok=True)
    for rel_path, content in {**task.files, **task.tests}.items():
        target = destination / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git_init(destination)
    return destination


def _harness_revision() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except OSError:
        pass
    return "unknown"


def _resolve_tasks(
    task_ids: list[str] | None,
    *,
    suite: Literal["smoke", "full"] | None = None,
) -> list[BenchmarkTask]:
    return resolve_tasks(task_ids, suite=suite)


def _estimate_attempt_cost(usage: TokenUsage, model: str, base_url: str | None) -> float:
    cost = _estimate_cost(usage, model)
    if cost > 0.0:
        return cost
    if base_url and "together" in base_url:
        together_key = f"together_ai/{model}"
        if together_key in COST_TABLE:
            return _estimate_cost(usage, together_key)
    return 0.0


def summarize_profile(
    profile_name: str,
    profile: ModelProfileSettings,
    results: list[TaskAttemptResult],
    *,
    temperature: float,
    max_iterations: int,
    max_tokens_per_run: int,
    skip_reason: str | None = None,
) -> ProfileBenchmarkSummary:
    """Compute summary metrics including failed and errored runs."""
    attempts = len(results)
    verified_fixes = sum(1 for item in results if item.verified)
    total_tokens = sum(item.total_tokens for item in results)
    total_cost = round(sum(item.estimated_cost_usd for item in results), 6)
    latencies = [item.latency_seconds for item in results]
    total_calls = sum(item.tool_calls for item in results)
    total_errors = sum(item.tool_errors for item in results)
    false_positives = sum(
        1
        for item in results
        if (item.run_state == "completed" and not item.verified)
        or (item.status == "failed" and item.reason == "unverified_completion")
    )
    reported_models = list(
        dict.fromkeys(item.reported_model for item in results if item.reported_model)
    )
    return ProfileBenchmarkSummary(
        profile=profile_name,
        provider=profile.provider,
        requested_model=profile.model,
        reported_models=reported_models,
        status="skipped_missing_credentials" if skip_reason else "completed",
        skip_reason=skip_reason,
        temperature=temperature,
        max_iterations=max_iterations,
        max_tokens_per_run=max_tokens_per_run,
        attempts=attempts,
        verified_fixes=verified_fixes,
        verified_rate=round(verified_fixes / attempts, 4) if attempts else 0.0,
        mean_latency_seconds=round(statistics.mean(latencies), 3) if latencies else 0.0,
        median_latency_seconds=round(statistics.median(latencies), 3) if latencies else 0.0,
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        cost_per_verified_fix_usd=(
            round(total_cost / verified_fixes, 6) if verified_fixes > 0 else None
        ),
        tool_error_rate=round(total_errors / total_calls, 4) if total_calls else 0.0,
        false_positive_rate=round(false_positives / attempts, 4) if attempts else 0.0,
        results=results,
    )


def _append_synthesized_trajectory(
    journal: RunJournal,
    *,
    profile: str,
    task_id: str,
    attempt: int,
    run: AgentRun,
    result: TaskAttemptResult,
) -> None:
    journal.append(
        "run_started",
        {
            "profile": profile,
            "task_id": task_id,
            "attempt": attempt,
            "model": run.config.model,
            "task": run.task,
            "initial_state": RunState.PENDING.value,
            "resumed": False,
        },
        iteration=0,
    )
    if run.state != RunState.PENDING:
        journal.append(
            "state_transition",
            {
                "from_state": RunState.PENDING.value,
                "to_state": RunState.RUNNING.value,
                "reason": "run_started",
            },
            iteration=0,
        )
    for index, msg in enumerate(run.messages):
        msg_rec: dict[str, Any] = {
            "index": index,
            "role": msg.role.value,
            "content": msg.content,
        }
        if msg.tool_call_id:
            msg_rec["tool_call_id"] = msg.tool_call_id
        if msg.tool_calls:
            msg_rec["tool_calls"] = [asdict(tc) for tc in msg.tool_calls]
        journal.append("message_recorded", msg_rec, iteration=run.iterations)
    if run.total_tokens.total_tokens > 0:
        journal.append(
            "llm_response",
            {
                "model": run.config.model,
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": run.total_tokens.prompt_tokens,
                    "completion_tokens": run.total_tokens.completion_tokens,
                    "total_tokens": run.total_tokens.total_tokens,
                },
            },
            iteration=run.iterations,
        )
    for idx, inv in enumerate(run.tool_invocations):
        side_effecting = is_side_effecting_tool(inv.tool_name)
        call_id = f"syn-{idx}"
        idem_key = compute_tool_idempotency_key(
            run.id,
            inv.iteration,
            call_id,
            inv.tool_name,
            dict(inv.arguments),
            call_index=idx,
        )
        journal.append(
            "action_requested",
            {
                "tool_call_id": call_id,
                "idempotency_key": idem_key,
                "tool_name": inv.tool_name,
                "arguments": dict(inv.arguments),
                "side_effecting": side_effecting,
            },
            iteration=inv.iteration,
        )
        journal.append(
            "action_result",
            {
                "tool_call_id": call_id,
                "idempotency_key": idem_key,
                "tool_name": inv.tool_name,
                "arguments": dict(inv.arguments),
                "side_effecting": side_effecting,
                "reused": False,
                "duration_ms": inv.duration_ms,
                "result": {
                    "output": inv.result.output,
                    "error": inv.result.error,
                    "exit_code": inv.result.exit_code,
                    "execution_time_ms": inv.result.execution_time_ms,
                    "metadata": inv.result.metadata,
                },
            },
            iteration=inv.iteration,
        )
    if run.verification is not None:
        journal.append(
            "verification_recorded",
            {
                "base_revision": run.verification.base_revision,
                "reason": run.verification.reason,
                "passed": run.verification.passed,
                "command": run.verification.command,
                "exit_code": run.verification.exit_code,
                "patch": run.verification.patch,
                "stdout": run.verification.stdout,
                "stderr": run.verification.stderr,
            },
            iteration=run.iterations,
        )
    if run.state not in (RunState.PENDING, RunState.RUNNING):
        journal.append(
            "state_transition",
            {
                "from_state": RunState.RUNNING.value,
                "to_state": run.state.value,
                "reason": result.reason or run.state.value,
            },
            iteration=run.iterations,
        )
    journal.append(
        "attempt_result",
        {
            "profile": profile,
            "task_id": task_id,
            "attempt": attempt,
            **result.model_dump(mode="json"),
        },
        iteration=run.iterations,
    )


def save_attempt_trajectory(
    trajectory_path: Path,
    *,
    profile: str,
    task_id: str,
    attempt: int,
    run: AgentRun,
    result: TaskAttemptResult,
) -> Path:
    """Persist a synthesized replayable ``RunJournal`` trajectory as JSONL."""
    trajectory_path.parent.mkdir(parents=True, exist_ok=True)
    if trajectory_path.exists():
        trajectory_path.unlink()
    journal = RunJournal(run.id, path=trajectory_path, fsync=False)
    _append_synthesized_trajectory(
        journal,
        profile=profile,
        task_id=task_id,
        attempt=attempt,
        run=run,
        result=result,
    )
    return trajectory_path


def maybe_save_trajectory(
    trajectory_dir: Path | None,
    *,
    profile_name: str,
    task_id: str,
    attempt: int,
    run: AgentRun,
    result: TaskAttemptResult,
    journal: RunJournal | None = None,
) -> TaskAttemptResult:
    """Persist attempt trajectory JSONL when ``trajectory_dir`` is provided."""
    if trajectory_dir is None:
        return result
    path = trajectory_dir / f"{profile_name}__{task_id}__attempt-{attempt}.jsonl"
    updated = result.model_copy(update={"trajectory_path": str(path)})
    if journal is not None and journal.entry_count > 0:
        journal.append(
            "attempt_result",
            {
                "profile": profile_name,
                "task_id": task_id,
                "attempt": attempt,
                **updated.model_dump(mode="json"),
            },
            iteration=run.iterations,
        )
        return updated
    save_attempt_trajectory(
        path,
        profile=profile_name,
        task_id=task_id,
        attempt=attempt,
        run=run,
        result=updated,
    )
    return updated


def _accepts_kwarg(fn: Any, name: str) -> bool:
    target = getattr(fn, "side_effect", None)
    target_fn = target if callable(target) else fn
    try:
        params = inspect.signature(target_fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _create_benchmark_provider(
    config: ForgeConfig,
    profile: ModelProfileSettings,
    api_key: str,
    base_url: str | None,
) -> Any:
    api_mode = profile.api_mode
    if api_mode == "auto":
        provider_cfg = config.providers.get(profile.provider)
        if provider_cfg is not None:
            api_mode = provider_cfg.api_mode
    provider_kwargs: dict[str, Any] = {}
    if base_url:
        provider_kwargs["base_url"] = base_url
    if profile.provider in ("openai", "openai-compatible") and api_mode != "auto":
        provider_kwargs["api_mode"] = api_mode
    merged_caps = resolve_profile_capabilities(config, profile)
    if merged_caps and profile.provider in ("openai", "openai-compatible"):
        provider_kwargs["capability_overrides"] = merged_caps
    return create_provider(profile.provider, api_key, **provider_kwargs)


async def _run_single_attempt(
    config: ForgeConfig,
    profile_name: str,
    profile: ModelProfileSettings,
    api_key: str,
    base_url: str | None,
    task: BenchmarkTask,
    attempt: int,
    *,
    sandbox_config: SandboxConfig,
    max_iterations: int,
    max_tokens_per_run: int,
    timeout_seconds: int,
    trajectory_dir: Path | None = None,
) -> TaskAttemptResult:
    temperature = (
        profile.temperature if profile.temperature is not None else config.agent.temperature
    )
    tools = create_default_registry(load_plugins=False)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"bench-{task.id}-") as tmp_dir:
        repo_path = materialize_task_repo(task, Path(tmp_dir) / "repo")
        policy = ContextPolicy.from_settings(config.context)
        context_engine = (
            ContextEngine(repo_path=repo_path, task=task.prompt, policy=policy)
            if policy.enabled
            else None
        )
        if context_engine is not None:
            instructions, repo_map = context_engine.prepare_initial_context()
            repo_instructions = instructions.rendered or None
            repo_map_text = repo_map.rendered or None
            scoped_instructions = context_engine.task_scoped_instructions_rendered or None
        else:
            repo_instructions = None
            repo_map_text = None
            scoped_instructions = None
        agent_cfg = AgentConfig(
            model=profile.model,
            provider=profile.provider,
            max_iterations=max_iterations,
            max_tokens_per_run=max_tokens_per_run,
            temperature=temperature,
            context_enabled=policy.enabled,
            system_prompt=build_system_prompt(
                task.prompt,
                tools.list_definitions(),
                sandbox_backend=sandbox_config.backend,
                sandbox_image=sandbox_config.image,
                network_enabled=False,
                command_timeout_seconds=sandbox_config.timeout_seconds,
                repo_instructions=repo_instructions,
                repo_map=repo_map_text,
                scoped_instructions=scoped_instructions,
            ),
        )
        run = AgentRun(task=task.prompt, repo_path=str(repo_path), config=agent_cfg)
        journal: RunJournal | None = None
        if trajectory_dir is not None:
            traj_path = trajectory_dir / f"{profile_name}__{task.id}__attempt-{attempt}.jsonl"
            if traj_path.exists():
                traj_path.unlink()
            journal = RunJournal(run.id, path=traj_path, fsync=False)
        raw_llm = _create_benchmark_provider(config, profile, api_key, base_url)
        llm = _RecordingLLM(raw_llm)
        sandbox = create_sandbox(sandbox_config)
        try:
            with VerificationWorkspace(
                str(repo_path),
                task.verify_command,
                extra_verifier_files=task.hidden_tests,
            ) as workspace:
                await sandbox.start(repo_path=str(workspace.workspace), config=sandbox_config)

                async def _verify() -> Any:
                    return await workspace.verify(sandbox_config, sandbox)

                async with asyncio.timeout(timeout_seconds):
                    extra: dict[str, Any] = (
                        {"context_engine": context_engine}
                        if _accepts_kwarg(react_loop, "context_engine")
                        else {}
                    )
                    if journal is not None:
                        await react_loop(
                            run,
                            llm,
                            tools,
                            sandbox,
                            verify=_verify,
                            journal=journal,
                            base_revision=workspace.base_revision,
                            verify_command=task.verify_command,
                            **extra,
                        )
                    else:
                        await react_loop(run, llm, tools, sandbox, verify=_verify, **extra)
        except TimeoutError:
            res = _error_attempt_result(
                task.id,
                attempt,
                run,
                profile.model,
                base_url,
                llm.reported_models,
                time.monotonic() - started,
                reason=f"Timed out after {timeout_seconds}s",
            )
            return maybe_save_trajectory(
                trajectory_dir,
                profile_name=profile_name,
                task_id=task.id,
                attempt=attempt,
                run=run,
                result=res,
                journal=journal,
            )
        except Exception as exc:  # noqa: BLE001 — isolate failed candidate runs
            res = _error_attempt_result(
                task.id,
                attempt,
                run,
                profile.model,
                base_url,
                llm.reported_models,
                time.monotonic() - started,
                reason=f"{type(exc).__name__}: {exc}",
            )
            return maybe_save_trajectory(
                trajectory_dir,
                profile_name=profile_name,
                task_id=task.id,
                attempt=attempt,
                run=run,
                result=res,
                journal=journal,
            )
        finally:
            try:
                await sandbox.stop()
            finally:
                await llm.close()

    res = build_completed_result(
        task.id,
        attempt,
        run,
        profile.model,
        base_url,
        llm.reported_models,
        time.monotonic() - started,
    )
    return maybe_save_trajectory(
        trajectory_dir,
        profile_name=profile_name,
        task_id=task.id,
        attempt=attempt,
        run=run,
        result=res,
        journal=journal,
    )


def _error_attempt_result(
    task_id: str,
    attempt: int,
    run: AgentRun,
    model: str,
    base_url: str | None,
    reported_models: list[str],
    latency_seconds: float,
    *,
    reason: str,
) -> TaskAttemptResult:
    tool_calls = len(run.tool_invocations)
    tool_errors = sum(
        1
        for inv in run.tool_invocations
        if inv.result.exit_code != 0 or inv.result.error is not None
    )
    return TaskAttemptResult(
        task_id=task_id,
        attempt=attempt,
        status="error",
        verified=False,
        latency_seconds=round(latency_seconds, 3),
        iterations=run.iterations,
        prompt_tokens=run.total_tokens.prompt_tokens,
        completion_tokens=run.total_tokens.completion_tokens,
        total_tokens=run.total_tokens.total_tokens,
        estimated_cost_usd=round(_estimate_attempt_cost(run.total_tokens, model, base_url), 6),
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        run_state=run.state.value,
        run_id=run.id,
        reported_model=reported_models[0] if reported_models else None,
        reason=reason,
    )


def build_completed_result(
    task_id: str,
    attempt: int,
    run: AgentRun,
    model: str,
    base_url: str | None,
    reported_models: list[str],
    latency_seconds: float,
) -> TaskAttemptResult:
    """Build a scored :class:`TaskAttemptResult` from a completed agent run."""
    verification = run.verification
    verified = verification is not None and verification.passed and run.state == RunState.COMPLETED
    patch_applied = verification is not None and verification.reason in (
        "verified",
        "check_failed",
    )
    tool_calls = len(run.tool_invocations)
    tool_errors = sum(
        1
        for inv in run.tool_invocations
        if inv.result.exit_code != 0 or inv.result.error is not None
    )
    contract_eval = run.contract_evaluation
    unmet_clauses = (
        [clause.clause_id for clause in contract_eval.unmet_clauses]
        if contract_eval is not None
        else []
    )
    reason = verification.reason if verification is not None else (run.error or run.state.value)
    return TaskAttemptResult(
        task_id=task_id,
        attempt=attempt,
        status="passed" if verified else "failed",
        verified=verified,
        latency_seconds=round(latency_seconds, 3),
        iterations=run.iterations,
        prompt_tokens=run.total_tokens.prompt_tokens,
        completion_tokens=run.total_tokens.completion_tokens,
        total_tokens=run.total_tokens.total_tokens,
        estimated_cost_usd=round(_estimate_attempt_cost(run.total_tokens, model, base_url), 6),
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        patch_applied=patch_applied,
        run_state=run.state.value,
        unmet_clauses=unmet_clauses,
        run_id=run.id,
        reported_model=reported_models[0] if reported_models else None,
        reason=reason,
    )


async def _run_profile_attempts(
    config: ForgeConfig,
    profile_name: str,
    profile: ModelProfileSettings,
    api_key: str,
    base_url: str | None,
    tasks: list[BenchmarkTask],
    attempts: int,
    *,
    sandbox_config: SandboxConfig,
    max_iterations: int,
    max_tokens_per_run: int,
    timeout_seconds: int,
    trajectory_dir: Path | None,
    max_concurrency: int,
) -> list[TaskAttemptResult]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _bounded(task: BenchmarkTask, attempt: int) -> TaskAttemptResult:
        async with semaphore:
            return await _run_single_attempt(
                config,
                profile_name,
                profile,
                api_key,
                base_url,
                task,
                attempt,
                sandbox_config=sandbox_config,
                max_iterations=max_iterations,
                max_tokens_per_run=max_tokens_per_run,
                timeout_seconds=timeout_seconds,
                trajectory_dir=trajectory_dir,
            )

    if max_concurrency == 1:
        results: list[TaskAttemptResult] = []
        for task in tasks:
            for attempt in range(1, attempts + 1):
                results.append(await _bounded(task, attempt))
        return results

    coros = [_bounded(task, attempt) for task in tasks for attempt in range(1, attempts + 1)]
    return list(await asyncio.gather(*coros))


async def run_benchmark(
    config: ForgeConfig,
    *,
    profile_names: list[str] | None = None,
    task_ids: list[str] | None = None,
    suite: Literal["smoke", "full"] | None = None,
    attempts: int = 1,
    sandbox_backend: str | None = None,
    max_iterations: int = 8,
    max_tokens_per_run: int = 40_000,
    timeout_seconds: int = 180,
    trajectory_dir: Path | None = None,
    max_concurrency: int = 1,
) -> BenchmarkReport:
    """Run pinned coding benchmark tasks across selected model profiles."""
    if attempts < 1:
        msg = "attempts must be >= 1"
        raise ValueError(msg)
    if max_concurrency < 1:
        msg = "max_concurrency must be >= 1"
        raise ValueError(msg)

    tasks = _resolve_tasks(task_ids, suite=suite)
    names = list(profile_names) if profile_names else list(config.model_profiles)
    if not names:
        msg = "No model profiles configured to benchmark."
        raise ValueError(msg)

    backend = sandbox_backend or config.sandbox.backend
    sandbox_cfg = SandboxConfig(
        backend=backend,
        image=config.sandbox.image,
        cpu_limit=config.sandbox.cpu_limit,
        memory_limit=config.sandbox.memory_limit,
        timeout_seconds=config.sandbox.timeout_seconds,
        network_enabled=False,
        writable_cache_mounts=False,
    )

    summaries: list[ProfileBenchmarkSummary] = []
    for name in names:
        profile = resolve_model_profile(config, name)
        temperature = (
            profile.temperature if profile.temperature is not None else config.agent.temperature
        )
        key_env, api_key, key_required = resolve_profile_credentials(config, profile)
        if key_required and not api_key:
            summaries.append(
                summarize_profile(
                    name,
                    profile,
                    [],
                    temperature=temperature,
                    max_iterations=max_iterations,
                    max_tokens_per_run=max_tokens_per_run,
                    skip_reason=f"Missing credential variable {key_env}.",
                )
            )
            continue

        base_url = resolve_profile_base_url(config, profile)
        results = await _run_profile_attempts(
            config,
            name,
            profile,
            api_key,
            base_url,
            tasks,
            attempts,
            sandbox_config=sandbox_cfg,
            max_iterations=max_iterations,
            max_tokens_per_run=max_tokens_per_run,
            timeout_seconds=timeout_seconds,
            trajectory_dir=trajectory_dir,
            max_concurrency=max_concurrency,
        )
        summaries.append(
            summarize_profile(
                name,
                profile,
                results,
                temperature=temperature,
                max_iterations=max_iterations,
                max_tokens_per_run=max_tokens_per_run,
            )
        )

    return BenchmarkReport(
        generated_at=datetime.now(UTC).isoformat(),
        harness_revision=_harness_revision(),
        sandbox_backend=backend,
        tasks=[task.id for task in tasks],
        profiles=summaries,
    )


def render_markdown_report(report: BenchmarkReport) -> str:
    """Render a concise Markdown summary table for a :class:`BenchmarkReport`."""
    lines = [
        "# Model Benchmark Report",
        "",
        f"- **Generated at:** `{report.generated_at}`",
        f"- **Harness revision:** `{report.harness_revision}`",
        f"- **Sandbox backend:** `{report.sandbox_backend}`",
        f"- **Tasks:** {', '.join(f'`{t}`' for t in report.tasks)}",
        "",
        "## Profile Summary",
        "",
        "| Profile | Provider | Model | Status | Verified | Median Latency | "
        "Total Cost (incl. failures) | Cost / Verified Fix | Tool Error Rate |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.profiles:
        if item.status != "completed":
            lines.append(
                f"| `{item.profile}` | {item.provider} | `{item.requested_model}` | "
                f"skipped ({item.skip_reason}) | - | - | - | - | - |"
            )
            continue
        cost_fix = (
            f"${item.cost_per_verified_fix_usd:.6f}"
            if item.cost_per_verified_fix_usd is not None
            else "N/A (0 verified)"
        )
        lines.append(
            f"| `{item.profile}` | {item.provider} | `{item.requested_model}` | "
            f"{item.status} | {item.verified_fixes}/{item.attempts} "
            f"({item.verified_rate:.0%}) | {item.median_latency_seconds:.2f}s | "
            f"${item.total_cost_usd:.6f} | {cost_fix} | {item.tool_error_rate:.1%} |"
        )
    lines.extend(["", "## Per-Task Attempts", ""])
    lines.append(
        "| Profile | Task | Attempt | Status | Verified | Latency | Tokens | Est. Cost | Reason |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for item in report.profiles:
        for res in item.results:
            lines.append(
                f"| `{item.profile}` | `{res.task_id}` | {res.attempt} | {res.status} | "
                f"{'yes' if res.verified else 'no'} | {res.latency_seconds:.2f}s | "
                f"{res.total_tokens} | ${res.estimated_cost_usd:.6f} | {res.reason} |"
            )
    return "\n".join(lines) + "\n"
