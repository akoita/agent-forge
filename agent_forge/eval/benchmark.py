"""Pinned coding benchmark suite and runner for comparing model profiles."""

from __future__ import annotations

import asyncio
import os
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun, RunState
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.verification import VerificationWorkspace
from agent_forge.llm.base import LLMProvider, TokenUsage
from agent_forge.llm.factory import create_provider
from agent_forge.llm.qualification import (
    resolve_model_profile,
    resolve_profile_api_key_env,
    resolve_profile_base_url,
)
from agent_forge.observability.cost import COST_TABLE, _estimate_cost
from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.factory import create_sandbox
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_forge.config import ForgeConfig, ModelProfileSettings
    from agent_forge.llm.base import LLMConfig, LLMResponse, Message, ToolDefinition

VERIFY_CMD = "python3 -m unittest discover -s tests -v"


@dataclass(frozen=True)
class BenchmarkTask:
    """Self-contained coding benchmark task with independent verifier tests."""

    id: str
    title: str
    prompt: str
    verify_command: str
    files: dict[str, str]
    tests: dict[str, str]
    known_good_files: dict[str, str]


BUILTIN_TASKS: tuple[BenchmarkTask, ...] = (
    BenchmarkTask(
        id="fix-health-status",
        title="Return healthy HTTP status from /health handler",
        prompt=(
            "The health endpoint handler in app.py returns an error payload with status 500. "
            "Inspect app.py and tests/test_app.py, fix health() so it returns "
            '({"status": "ok"}, 200), and verify tests pass.'
        ),
        verify_command=VERIFY_CMD,
        files={
            "app.py": (
                '"""Application handlers."""\n\n'
                "def index() -> tuple[str, int]:\n"
                '    return "Hello, World!", 200\n\n\n'
                "def health() -> tuple[dict[str, str], int]:\n"
                '    return {"status": "error"}, 500\n'
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_app.py": (
                "import unittest\n"
                "from app import health, index\n\n\n"
                "class TestApp(unittest.TestCase):\n"
                "    def test_index(self) -> None:\n"
                "        body, code = index()\n"
                "        self.assertEqual(code, 200)\n"
                '        self.assertIn("Hello", body)\n\n'
                "    def test_health_ok(self) -> None:\n"
                "        body, code = health()\n"
                "        self.assertEqual(code, 200)\n"
                '        self.assertEqual(body, {"status": "ok"})\n'
            ),
        },
        known_good_files={
            "app.py": (
                '"""Application handlers."""\n\n'
                "def index() -> tuple[str, int]:\n"
                '    return "Hello, World!", 200\n\n\n'
                "def health() -> tuple[dict[str, str], int]:\n"
                '    return {"status": "ok"}, 200\n'
            ),
        },
    ),
    BenchmarkTask(
        id="fix-pagination-offset",
        title="Fix 1-based page slicing and ceiling total_pages",
        prompt=(
            "Fix paginate() in pagination.py so 1-based page slicing and total_pages ceiling "
            "calculation satisfy tests/test_pagination.py, and verify tests pass."
        ),
        verify_command=VERIFY_CMD,
        files={
            "pagination.py": (
                '"""List pagination helper."""\n\n'
                "def paginate(\n"
                "    items: list[int], page: int = 1, page_size: int = 10\n"
                ") -> dict[str, object]:\n"
                "    if page < 1:\n"
                '        raise ValueError("page must be >= 1")\n'
                "    if page_size < 1:\n"
                '        raise ValueError("page_size must be >= 1")\n'
                "    start = page * page_size\n"
                "    end = start + page_size\n"
                "    total_pages = len(items) // page_size\n"
                "    return {\n"
                '        "items": items[start:end],\n'
                '        "page": page,\n'
                '        "page_size": page_size,\n'
                '        "total_items": len(items),\n'
                '        "total_pages": total_pages,\n'
                "    }\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_pagination.py": (
                "import unittest\n"
                "from pagination import paginate\n\n\n"
                "class TestPagination(unittest.TestCase):\n"
                "    def test_first_page(self) -> None:\n"
                "        res = paginate([1, 2, 3, 4, 5], page=1, page_size=2)\n"
                '        self.assertEqual(res["items"], [1, 2])\n'
                '        self.assertEqual(res["total_pages"], 3)\n\n'
                "    def test_last_partial_page(self) -> None:\n"
                "        res = paginate([1, 2, 3, 4, 5], page=3, page_size=2)\n"
                '        self.assertEqual(res["items"], [5])\n'
                '        self.assertEqual(res["total_pages"], 3)\n\n'
                "    def test_empty_items(self) -> None:\n"
                "        res = paginate([], page=1, page_size=5)\n"
                '        self.assertEqual(res["items"], [])\n'
                '        self.assertEqual(res["total_pages"], 0)\n'
            ),
        },
        known_good_files={
            "pagination.py": (
                '"""List pagination helper."""\n\n'
                "def paginate(\n"
                "    items: list[int], page: int = 1, page_size: int = 10\n"
                ") -> dict[str, object]:\n"
                "    if page < 1:\n"
                '        raise ValueError("page must be >= 1")\n'
                "    if page_size < 1:\n"
                '        raise ValueError("page_size must be >= 1")\n'
                "    start = (page - 1) * page_size\n"
                "    end = start + page_size\n"
                "    total_pages = (len(items) + page_size - 1) // page_size if items else 0\n"
                "    return {\n"
                '        "items": items[start:end],\n'
                '        "page": page,\n'
                '        "page_size": page_size,\n'
                '        "total_items": len(items),\n'
                '        "total_pages": total_pages,\n'
                "    }\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-multi-file-summary",
        title="Fix line-item field mismatch and currency formatting across modules",
        prompt=(
            "Inspect models.py, formatter.py, and tests/test_formatter.py. "
            "Fix summarize_order() in formatter.py so line totals multiply quantity by "
            "unit_price_cents and format totals to two decimal places, and verify tests pass."
        ),
        verify_command=VERIFY_CMD,
        files={
            "models.py": (
                '"""Domain data models for orders."""\n\n'
                "from dataclasses import dataclass\n\n\n"
                "@dataclass(frozen=True)\n"
                "class OrderLine:\n"
                "    sku: str\n"
                "    quantity: int\n"
                "    unit_price_cents: int\n"
            ),
            "formatter.py": (
                '"""Order summary formatter."""\n\n'
                "from models import OrderLine\n\n\n"
                "def summarize_order(lines: list[OrderLine]) -> dict[str, object]:\n"
                "    total_units = sum(line.quantity for line in lines)\n"
                "    total_cents = sum(line.price_cents for line in lines)\n"
                '    formatted = f"${round(total_cents / 100, 1)}"\n'
                "    return {\n"
                '        "total_units": total_units,\n'
                '        "total_cents": total_cents,\n'
                '        "display": formatted,\n'
                "    }\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_formatter.py": (
                "import unittest\n"
                "from formatter import summarize_order\n"
                "from models import OrderLine\n\n\n"
                "class TestFormatter(unittest.TestCase):\n"
                "    def test_summarize_order(self) -> None:\n"
                "        lines = [\n"
                '            OrderLine(sku="A1", quantity=2, unit_price_cents=1250),\n'
                '            OrderLine(sku="B2", quantity=3, unit_price_cents=199),\n'
                "        ]\n"
                "        summary = summarize_order(lines)\n"
                '        self.assertEqual(summary["total_units"], 5)\n'
                '        self.assertEqual(summary["total_cents"], 3097)\n'
                '        self.assertEqual(summary["display"], "$30.97")\n\n'
                "    def test_empty_order(self) -> None:\n"
                "        summary = summarize_order([])\n"
                '        self.assertEqual(summary["total_units"], 0)\n'
                '        self.assertEqual(summary["total_cents"], 0)\n'
                '        self.assertEqual(summary["display"], "$0.00")\n'
            ),
        },
        known_good_files={
            "formatter.py": (
                '"""Order summary formatter."""\n\n'
                "from models import OrderLine\n\n\n"
                "def summarize_order(lines: list[OrderLine]) -> dict[str, object]:\n"
                "    total_units = sum(line.quantity for line in lines)\n"
                "    total_cents = sum(line.quantity * line.unit_price_cents for line in lines)\n"
                '    formatted = f"${total_cents / 100:.2f}"\n'
                "    return {\n"
                '        "total_units": total_units,\n'
                '        "total_cents": total_cents,\n'
                '        "display": formatted,\n'
                "    }\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-retry-backoff",
        title="Fix exponential backoff attempt indexing and max_delay cap",
        prompt=(
            "Inspect retry.py and tests/test_retry.py. Fix compute_backoff() so 1-indexed "
            "attempts return min(base * (2 ** (attempt - 1)), max_delay) and raise ValueError "
            "for attempt < 1."
        ),
        verify_command=VERIFY_CMD,
        files={
            "retry.py": (
                '"""Exponential backoff calculation."""\n\n'
                "def compute_backoff(\n"
                "    attempt: int, *, base: float = 1.0, max_delay: float = 30.0\n"
                ") -> float:\n"
                "    return base * (2**attempt)\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_retry.py": (
                "import unittest\n"
                "from retry import compute_backoff\n\n\n"
                "class TestRetry(unittest.TestCase):\n"
                "    def test_progression_and_cap(self) -> None:\n"
                "        self.assertEqual(compute_backoff(1, base=2.0, max_delay=10.0), 2.0)\n"
                "        self.assertEqual(compute_backoff(2, base=2.0, max_delay=10.0), 4.0)\n"
                "        self.assertEqual(compute_backoff(3, base=2.0, max_delay=10.0), 8.0)\n"
                "        self.assertEqual(compute_backoff(4, base=2.0, max_delay=10.0), 10.0)\n\n"
                "    def test_invalid_attempt_raises(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            compute_backoff(0)\n"
            ),
        },
        known_good_files={
            "retry.py": (
                '"""Exponential backoff calculation."""\n\n'
                "def compute_backoff(\n"
                "    attempt: int, *, base: float = 1.0, max_delay: float = 30.0\n"
                ") -> float:\n"
                "    if attempt < 1:\n"
                '        raise ValueError("attempt must be >= 1")\n'
                "    return min(base * (2 ** (attempt - 1)), max_delay)\n"
            ),
        },
    ),
)


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


def _resolve_tasks(task_ids: list[str] | None) -> list[BenchmarkTask]:
    by_id = {task.id: task for task in BUILTIN_TASKS}
    if not task_ids:
        return list(BUILTIN_TASKS)
    selected: list[BenchmarkTask] = []
    for task_id in task_ids:
        if task_id not in by_id:
            available = ", ".join(sorted(by_id))
            msg = f"Unknown benchmark task '{task_id}'. Available: {available}"
            raise ValueError(msg)
        selected.append(by_id[task_id])
    return selected


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
        results=results,
    )


async def _run_single_attempt(
    config: ForgeConfig,
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
) -> TaskAttemptResult:
    temperature = (
        profile.temperature if profile.temperature is not None else config.agent.temperature
    )
    tools = create_default_registry(load_plugins=False)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"bench-{task.id}-") as tmp_dir:
        repo_path = materialize_task_repo(task, Path(tmp_dir) / "repo")
        agent_cfg = AgentConfig(
            model=profile.model,
            provider=profile.provider,
            max_iterations=max_iterations,
            max_tokens_per_run=max_tokens_per_run,
            temperature=temperature,
            system_prompt=build_system_prompt(
                task.prompt,
                tools.list_definitions(),
                sandbox_backend=sandbox_config.backend,
                sandbox_image=sandbox_config.image,
                network_enabled=False,
                command_timeout_seconds=sandbox_config.timeout_seconds,
            ),
        )
        run = AgentRun(task=task.prompt, repo_path=str(repo_path), config=agent_cfg)
        raw_llm = (
            create_provider(profile.provider, api_key, base_url=base_url)
            if base_url
            else create_provider(profile.provider, api_key)
        )
        llm = _RecordingLLM(raw_llm)
        sandbox = create_sandbox(sandbox_config)
        try:
            with VerificationWorkspace(str(repo_path), task.verify_command) as workspace:
                await sandbox.start(repo_path=str(workspace.workspace), config=sandbox_config)

                async def _verify() -> Any:
                    return await workspace.verify(sandbox_config, sandbox)

                async with asyncio.timeout(timeout_seconds):
                    await react_loop(run, llm, tools, sandbox, verify=_verify)
        except TimeoutError:
            return _error_attempt_result(
                task.id,
                attempt,
                run,
                profile.model,
                base_url,
                llm.reported_models,
                time.monotonic() - started,
                reason=f"Timed out after {timeout_seconds}s",
            )
        except Exception as exc:  # noqa: BLE001 — isolate failed candidate runs
            return _error_attempt_result(
                task.id,
                attempt,
                run,
                profile.model,
                base_url,
                llm.reported_models,
                time.monotonic() - started,
                reason=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                await sandbox.stop()
            finally:
                await llm.close()

    return _build_completed_result(
        task.id,
        attempt,
        run,
        profile.model,
        base_url,
        llm.reported_models,
        time.monotonic() - started,
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
        run_id=run.id,
        reported_model=reported_models[0] if reported_models else None,
        reason=reason,
    )


def _build_completed_result(
    task_id: str,
    attempt: int,
    run: AgentRun,
    model: str,
    base_url: str | None,
    reported_models: list[str],
    latency_seconds: float,
) -> TaskAttemptResult:
    verification = run.verification
    verified = verification is not None and verification.passed and run.state == RunState.COMPLETED
    tool_calls = len(run.tool_invocations)
    tool_errors = sum(
        1
        for inv in run.tool_invocations
        if inv.result.exit_code != 0 or inv.result.error is not None
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
        run_id=run.id,
        reported_model=reported_models[0] if reported_models else None,
        reason=reason,
    )


async def run_benchmark(
    config: ForgeConfig,
    *,
    profile_names: list[str] | None = None,
    task_ids: list[str] | None = None,
    attempts: int = 1,
    sandbox_backend: str | None = None,
    max_iterations: int = 8,
    max_tokens_per_run: int = 40_000,
    timeout_seconds: int = 180,
) -> BenchmarkReport:
    """Run pinned coding benchmark tasks across selected model profiles."""
    if attempts < 1:
        msg = "attempts must be >= 1"
        raise ValueError(msg)

    tasks = _resolve_tasks(task_ids)
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
        key_env = resolve_profile_api_key_env(config, profile)
        api_key = os.environ.get(key_env, "")
        if not api_key:
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
        results: list[TaskAttemptResult] = []
        for task in tasks:
            for attempt in range(1, attempts + 1):
                results.append(
                    await _run_single_attempt(
                        config,
                        profile,
                        api_key,
                        base_url,
                        task,
                        attempt,
                        sandbox_config=sandbox_cfg,
                        max_iterations=max_iterations,
                        max_tokens_per_run=max_tokens_per_run,
                        timeout_seconds=timeout_seconds,
                    )
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
