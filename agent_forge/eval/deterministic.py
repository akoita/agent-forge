"""Offline deterministic harness evaluation runner and quality gate checker."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from agent_forge.agent.core import react_loop
from agent_forge.agent.models import AgentConfig, AgentRun
from agent_forge.agent.prompts import build_system_prompt
from agent_forge.agent.verification import VerificationWorkspace
from agent_forge.config import ModelProfileSettings
from agent_forge.eval.benchmark import (
    BenchmarkReport,
    ProfileBenchmarkSummary,
    TaskAttemptResult,
    _harness_revision,
    build_completed_result,
    materialize_task_repo,
    maybe_save_trajectory,
    render_markdown_report,
    summarize_profile,
)
from agent_forge.eval.corpus import BenchmarkTask, resolve_tasks
from agent_forge.llm.base import (
    LLMConfig,
    LLMProvider,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig, SandboxState
from agent_forge.sandbox.sessions import LocalPtySessionManager, SessionLimits, SessionSnapshot
from agent_forge.tools import create_default_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ScriptedMode = Literal["oracle", "noop", "incomplete"]
_SCRIPTED_PROFILES: tuple[tuple[str, ScriptedMode], ...] = (
    ("deterministic-oracle", "oracle"),
    ("deterministic-noop", "noop"),
    ("deterministic-incomplete", "incomplete"),
)


class SubprocessEvalSandbox(Sandbox):
    """Workspace-confined subprocess sandbox for offline deterministic eval."""

    def __init__(self) -> None:
        self._config = SandboxConfig(backend="subprocess-eval")
        self._state = SandboxState.IDLE
        self._repo_root: Path | None = None
        self._active_processes: set[asyncio.subprocess.Process] = set()
        self._sessions: LocalPtySessionManager | None = None

    @property
    def state(self) -> SandboxState:
        return self._state

    @property
    def timeout_cap_seconds(self) -> int:
        return self._config.timeout_seconds

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        if self._state == SandboxState.RUNNING:
            msg = "Sandbox is already running"
            raise RuntimeError(msg)
        self._config = config or SandboxConfig(backend="subprocess-eval")
        self._repo_root = Path(repo_path).resolve()
        self._sessions = LocalPtySessionManager(
            limits=SessionLimits(default_max_duration_seconds=self._config.timeout_seconds)
        )
        self._state = SandboxState.RUNNING

    async def exec(self, command: str, *, timeout_seconds: int = 30) -> ExecResult:
        if self._state != SandboxState.RUNNING or self._repo_root is None:
            msg = "Sandbox is not running"
            raise RuntimeError(msg)
        rewritten = self._rewrite_workspace_command(command)
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            rewritten,
            cwd=str(self._repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        self._active_processes.add(process)
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds}s",
            )
        except asyncio.CancelledError:
            process.kill()
            with contextlib.suppress(BaseException):
                await process.communicate()
            raise
        finally:
            self._active_processes.discard(process)
        return ExecResult(
            exit_code=process.returncode or 0,
            stdout=(stdout_raw or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_raw or b"").decode("utf-8", errors="replace"),
        )

    async def read_file(self, path: str) -> str:
        target = self._resolve_workspace_path(path)
        if not target.is_file():
            msg = f"File not found in workspace: {path}"
            raise FileNotFoundError(msg)
        return target.read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve_workspace_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def stop(self) -> None:
        await self.close_all_sessions()
        for process in list(self._active_processes):
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(BaseException):
                    await process.communicate()
        self._active_processes.clear()
        self._repo_root = None
        self._state = SandboxState.STOPPED

    async def is_alive(self) -> bool:
        return self._state == SandboxState.RUNNING and self._repo_root is not None

    async def start_session(
        self,
        command: str,
        *,
        pty: bool = True,
        max_duration_seconds: int | None = None,
        wait_seconds: float = 0.5,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        if self._state != SandboxState.RUNNING or self._repo_root is None or self._sessions is None:
            msg = "Sandbox is not running"
            raise RuntimeError(msg)
        rewritten = self._rewrite_workspace_command(command)
        return await self._sessions.start(
            ["/bin/sh", "-c", rewritten],
            command_label=command,
            cwd=str(self._repo_root),
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            pty_enabled=pty,
            max_duration_seconds=max_duration_seconds,
            initial_wait_seconds=wait_seconds,
            max_bytes=max_bytes,
        )

    async def poll_session(
        self,
        session_id: str,
        *,
        wait_seconds: float = 0.5,
        offset: int | None = None,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        if self._state != SandboxState.RUNNING or self._sessions is None:
            msg = "Sandbox is not running"
            raise RuntimeError(msg)
        return await self._sessions.poll(
            session_id,
            wait_seconds=wait_seconds,
            offset=offset,
            max_bytes=max_bytes,
        )

    async def write_session(
        self,
        session_id: str,
        data: str,
        *,
        wait_seconds: float = 0.5,
        max_bytes: int = 32 * 1024,
    ) -> SessionSnapshot:
        if self._state != SandboxState.RUNNING or self._sessions is None:
            msg = "Sandbox is not running"
            raise RuntimeError(msg)
        return await self._sessions.write(
            session_id,
            data,
            wait_seconds=wait_seconds,
            max_bytes=max_bytes,
        )

    async def cancel_session(self, session_id: str) -> SessionSnapshot:
        if self._state != SandboxState.RUNNING or self._sessions is None:
            msg = "Sandbox is not running"
            raise RuntimeError(msg)
        return await self._sessions.cancel(session_id)

    async def list_sessions(self) -> list[SessionSnapshot]:
        if self._sessions is None:
            return []
        return await self._sessions.list_sessions()

    async def close_all_sessions(self) -> None:
        if self._sessions is not None:
            try:
                await self._sessions.close_all()
            finally:
                self._sessions = None

    def _rewrite_workspace_command(self, command: str) -> str:
        ws = self._config.workspace_path.rstrip("/")
        rewritten = re.sub(rf"{re.escape(ws)}/", "", command)
        return re.sub(
            rf"(?<![A-Za-z0-9_/.-]){re.escape(ws)}(?![A-Za-z0-9_/-])",
            ".",
            rewritten,
        )

    def _resolve_workspace_path(self, path: str) -> Path:
        if self._state != SandboxState.RUNNING or self._repo_root is None:
            msg = "Sandbox is not running"
            raise RuntimeError(msg)
        ws_prefix = self._config.workspace_path.rstrip("/")
        if path == ws_prefix:
            rel = ""
        elif path.startswith(ws_prefix + "/"):
            rel = path[len(ws_prefix) + 1 :]
        elif not path.startswith("/"):
            rel = path
        else:
            msg = f"Path outside workspace denied: {path}"
            raise PermissionError(msg)
        target = (self._repo_root / rel).resolve() if rel else self._repo_root
        if not target.is_relative_to(self._repo_root):
            msg = f"Path escapes workspace root: {path}"
            raise PermissionError(msg)
        return target


class _ScriptedEvalLLM(LLMProvider):
    """Deterministic scripted provider exercising tools and verification."""

    def __init__(self, task: BenchmarkTask, mode: ScriptedMode) -> None:
        self._mode = mode
        self._turns = self._build_turns(task, mode)
        self._cursor = 0

    @staticmethod
    def _build_turns(task: BenchmarkTask, mode: ScriptedMode) -> list[LLMResponse]:
        first_file = next(iter(task.files))
        model_id = f"scripted-{mode}"
        turns = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_read_1",
                        name="read_file",
                        arguments={"path": f"/workspace/{first_file}"},
                    )
                ],
                usage=TokenUsage(prompt_tokens=120, completion_tokens=20, total_tokens=140),
                model=model_id,
                finish_reason="tool_calls",
                reported_model=model_id,
            )
        ]
        target_files: dict[str, str] = {}
        if mode == "oracle":
            target_files = task.known_good_files
        elif mode == "incomplete":
            target_files = task.incomplete_files

        for idx, (rel_path, content) in enumerate(target_files.items(), start=1):
            turns.append(
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"call_write_{idx}",
                            name="write_file",
                            arguments={"path": f"/workspace/{rel_path}", "content": content},
                        )
                    ],
                    usage=TokenUsage(prompt_tokens=140, completion_tokens=30, total_tokens=170),
                    model=model_id,
                    finish_reason="tool_calls",
                    reported_model=model_id,
                )
            )
        if mode == "oracle":
            turns.append(
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call_verify_1",
                            name="run_shell",
                            arguments={"command": task.verify_command},
                        )
                    ],
                    usage=TokenUsage(prompt_tokens=150, completion_tokens=20, total_tokens=170),
                    model=model_id,
                    finish_reason="tool_calls",
                    reported_model=model_id,
                )
            )
        turns.append(
            LLMResponse(
                content=f"Completed scripted {mode} trajectory.",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=160, completion_tokens=15, total_tokens=175),
                model=model_id,
                finish_reason="stop",
                reported_model=model_id,
            )
        )
        return turns

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        del messages, tools, config
        idx = min(self._cursor, len(self._turns) - 1)
        self._cursor += 1
        return self._turns[idx]

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        yield await self.complete(messages, tools=tools, config=config)


async def _run_deterministic_attempt(
    profile_name: str,
    mode: ScriptedMode,
    task: BenchmarkTask,
    *,
    trajectory_dir: Path | None,
) -> TaskAttemptResult:
    tools = create_default_registry(load_plugins=False)
    sandbox_cfg = SandboxConfig(
        backend="subprocess-eval",
        network_enabled=False,
        writable_cache_mounts=False,
        timeout_seconds=30,
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"det-eval-{task.id}-") as tmp_dir:
        repo_path = materialize_task_repo(task, Path(tmp_dir) / "repo")
        model_id = f"scripted-{mode}"
        agent_cfg = AgentConfig(
            model=model_id,
            provider="deterministic",
            max_iterations=10,
            max_tokens_per_run=40_000,
            temperature=0.0,
            system_prompt=build_system_prompt(
                task.prompt,
                tools.list_definitions(),
                sandbox_backend=sandbox_cfg.backend,
                sandbox_image=sandbox_cfg.image,
                network_enabled=False,
                command_timeout_seconds=sandbox_cfg.timeout_seconds,
            ),
        )
        run = AgentRun(task=task.prompt, repo_path=str(repo_path), config=agent_cfg)
        llm = _ScriptedEvalLLM(task, mode)
        sandbox = SubprocessEvalSandbox()
        try:
            with VerificationWorkspace(
                str(repo_path),
                task.verify_command,
                extra_verifier_files=task.hidden_tests,
            ) as workspace:
                await sandbox.start(repo_path=str(workspace.workspace), config=sandbox_cfg)

                async def _verify() -> Any:
                    return await workspace.verify(
                        sandbox_cfg,
                        sandbox,
                        sandbox_factory=lambda _cfg: SubprocessEvalSandbox(),
                    )

                await react_loop(run, llm, tools, sandbox, verify=_verify)
        finally:
            try:
                await sandbox.stop()
            finally:
                await llm.close()

    result = build_completed_result(
        task.id,
        1,
        run,
        model_id,
        None,
        [model_id],
        time.monotonic() - started,
    )
    return maybe_save_trajectory(
        trajectory_dir,
        profile_name=profile_name,
        task_id=task.id,
        attempt=1,
        run=run,
        result=result,
    )


async def run_deterministic_eval(
    *,
    suite: Literal["smoke", "full"] = "smoke",
    task_ids: list[str] | None = None,
    output_json: Path | None = None,
    output_md: Path | None = None,
    trajectory_dir: Path | None = None,
) -> BenchmarkReport:
    """Run offline scripted oracle/noop/incomplete evaluation and emit scored reports."""
    tasks = resolve_tasks(task_ids, suite=suite)
    summaries: list[ProfileBenchmarkSummary] = []
    for profile_name, mode in _SCRIPTED_PROFILES:
        results: list[TaskAttemptResult] = []
        for task in tasks:
            results.append(
                await _run_deterministic_attempt(
                    profile_name,
                    mode,
                    task,
                    trajectory_dir=trajectory_dir,
                )
            )
        profile_settings = ModelProfileSettings(
            provider="deterministic",
            model=f"scripted-{mode}",
            temperature=0.0,
        )
        summaries.append(
            summarize_profile(
                profile_name,
                profile_settings,
                results,
                temperature=0.0,
                max_iterations=10,
                max_tokens_per_run=40_000,
            )
        )

    report = BenchmarkReport(
        generated_at=datetime.now(UTC).isoformat(),
        harness_revision=_harness_revision(),
        sandbox_backend="subprocess-eval",
        tasks=[task.id for task in tasks],
        profiles=summaries,
    )
    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if output_md is not None:
        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(render_markdown_report(report), encoding="utf-8")
    return report


def _check_profile_gate(
    summary: ProfileBenchmarkSummary,
    *,
    expected_rate: float,
    expected_reason: str,
    expected_patch: bool,
) -> list[str]:
    errors: list[str] = []
    if summary.attempts == 0:
        return [f"{summary.profile}: expected at least 1 attempt, got 0"]
    if summary.verified_rate != expected_rate:
        errors.append(
            f"{summary.profile}: expected verified_rate={expected_rate}, "
            f"got {summary.verified_rate}"
        )
    for item in summary.results:
        if item.reason != expected_reason:
            errors.append(
                f"{summary.profile}/{item.task_id}: expected reason='{expected_reason}', "
                f"got '{item.reason}'"
            )
        if item.patch_applied is not expected_patch:
            errors.append(
                f"{summary.profile}/{item.task_id}: expected patch_applied={expected_patch}, "
                f"got {item.patch_applied}"
            )
    return errors


def check_deterministic_gates(report: BenchmarkReport) -> list[str]:
    """Assert that oracle passes all tasks while noop and incomplete fail verification."""
    by_profile = {item.profile: item for item in report.profiles}
    expectations: tuple[tuple[str, float, str, bool], ...] = (
        ("deterministic-oracle", 1.0, "verified", True),
        ("deterministic-noop", 0.0, "empty_patch", False),
        ("deterministic-incomplete", 0.0, "check_failed", True),
    )
    failures: list[str] = []
    for name, expected_rate, expected_reason, expected_patch in expectations:
        summary = by_profile.get(name)
        if summary is None:
            failures.append(f"Missing expected profile in report: {name}")
            continue
        failures.extend(
            _check_profile_gate(
                summary,
                expected_rate=expected_rate,
                expected_reason=expected_reason,
                expected_patch=expected_patch,
            )
        )
    return failures
