"""Unit tests for verification-aware completion contracts (#139)."""

from __future__ import annotations

import inspect
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from agent_forge.agent.contracts import (
    ChangesExistClause,
    ChecksExecutedClause,
    CompletionContract,
    ContractContext,
    JsonArtifactClause,
    NoUnacknowledgedFailuresClause,
    PatchAppliesCleanlyClause,
    VerificationChecksPassedClause,
    coding_fix_contract,
    qa_contract,
    resolve_completion_contract,
)
from agent_forge.agent.core import react_loop
from agent_forge.agent.journal import RunJournal, replay_journal
from agent_forge.agent.models import AgentConfig, AgentRun, RunState, ToolInvocation
from agent_forge.agent.persistence import load_run, save_run
from agent_forge.agent.verification import VerificationResult
from agent_forge.cli import main as cli_main
from agent_forge.extensions.discovery import CONTRACT_PLUGIN_GROUP, discover_extension_contracts
from agent_forge.llm.base import LLMResponse, Message, Role, TokenUsage, ToolCall
from agent_forge.profiles.profile import AgentProfile, CompletionContractConfig, load_profiles
from agent_forge.sandbox.base import ExecResult, Sandbox, SandboxConfig
from agent_forge.tools import ToolRegistry, WriteFileTool
from agent_forge.tools.base import ToolResult
from plugins.proof_of_audit.contracts import (
    POA_REPORT_SCHEMA_VERSION,
    build_poa_completion_contract,
)


class _WorkspaceSandbox(Sandbox):
    def __init__(self, root: Path) -> None:
        self._root = root
        self._alive = True

    async def start(self, repo_path: str, config: SandboxConfig | None = None) -> None:
        del config
        self._root = Path(repo_path)
        self._alive = True

    async def stop(self) -> None:
        self._alive = False

    async def is_alive(self) -> bool:
        return self._alive

    def _resolve(self, path: str) -> Path:
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


def _valid_poa_payload(run_id: str = "r1") -> dict[str, Any]:
    return {
        "schema_version": POA_REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "summary": "No issues identified.",
        "confidence": "high",
        "findings": [],
        "stats": {
            "finding_count": 0,
            "max_severity": None,
            "severity_breakdown": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        },
    }


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "add", "."],
        ["git", "commit", "-m", "base"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)  # noqa: S603
    return repo


class TestBuiltinClausesAndFailureTracking:
    """Verify individual contract clauses and tool-failure semantics."""

    @pytest.mark.asyncio
    async def test_no_unacknowledged_failures_handles_stderr_retry_and_ack(
        self, tmp_path: Path
    ) -> None:
        now = datetime.now(UTC)

        # exit_code == 0 with stderr output (e.g., unittest runner) is NOT a failure.
        run_ok = AgentRun(task="t", repo_path=str(tmp_path), config=AgentConfig())
        run_ok.tool_invocations.append(
            ToolInvocation(
                tool_name="run_shell",
                arguments={"command": "python3 -m unittest"},
                result=ToolResult(output="", error="Ran 2 tests\nOK\n", exit_code=0),
                iteration=1,
                timestamp=now,
                duration_ms=10,
            )
        )
        res_ok = await NoUnacknowledgedFailuresClause().evaluate(ContractContext(run=run_ok))
        assert res_ok.passed is True

        # Failed read_file followed by retry success clears failure.
        run_retry = AgentRun(task="t", repo_path=str(tmp_path), config=AgentConfig())
        run_retry.tool_invocations.extend(
            [
                ToolInvocation(
                    tool_name="read_file",
                    arguments={"path": "missing.py"},
                    result=ToolResult(output="", error="not found", exit_code=1),
                    iteration=1,
                    timestamp=now,
                    duration_ms=2,
                ),
                ToolInvocation(
                    tool_name="read_file",
                    arguments={"path": "app.py"},
                    result=ToolResult(output="ok", exit_code=0),
                    iteration=2,
                    timestamp=now,
                    duration_ms=2,
                ),
            ]
        )
        assert (
            await NoUnacknowledgedFailuresClause().evaluate(ContractContext(run=run_retry))
        ).passed is True

        # Unresolved failure fails strict clause, but passes when acknowledged via final response.
        run_failed = AgentRun(task="t", repo_path=str(tmp_path), config=AgentConfig())
        run_failed.tool_invocations.append(
            ToolInvocation(
                tool_name="search_codebase",
                arguments={"pattern": "foo"},
                result=ToolResult(output="", error="rg failed", exit_code=2),
                iteration=1,
                timestamp=now,
                duration_ms=3,
            )
        )
        run_failed.messages.append(
            Message(
                role=Role.ASSISTANT,
                content="Note: search_codebase returned an error because rg was unavailable.",
            )
        )
        strict_res = await NoUnacknowledgedFailuresClause(allow_acknowledged=False).evaluate(
            ContractContext(run=run_failed)
        )
        assert strict_res.passed is False
        assert strict_res.error_code == "unacknowledged_tool_failure"

        ack_res = await NoUnacknowledgedFailuresClause(allow_acknowledged=True).evaluate(
            ContractContext(run=run_failed)
        )
        assert ack_res.passed is True

    @pytest.mark.asyncio
    async def test_changes_patch_checks_and_passed_clauses(self, tmp_path: Path) -> None:
        run = AgentRun(task="fix", repo_path=str(tmp_path), config=AgentConfig())
        ctx_empty = ContractContext(
            run=run,
            workspace_path=tmp_path,
            verification=VerificationResult("abc", "", "empty_patch", "pytest", None),
        )
        assert (await ChangesExistClause().evaluate(ctx_empty)).passed is False
        assert (await PatchAppliesCleanlyClause().evaluate(ctx_empty)).passed is False
        assert (await ChecksExecutedClause().evaluate(ctx_empty)).passed is False
        assert (await VerificationChecksPassedClause().evaluate(ctx_empty)).passed is False

        ctx_check_fail = ContractContext(
            run=run,
            workspace_path=tmp_path,
            verification=VerificationResult("abc", "diff --git ...", "check_failed", "pytest", 1),
        )
        assert (await ChangesExistClause().evaluate(ctx_check_fail)).passed is True
        assert (await PatchAppliesCleanlyClause().evaluate(ctx_check_fail)).passed is True
        assert (await ChecksExecutedClause().evaluate(ctx_check_fail)).passed is True
        assert (await VerificationChecksPassedClause().evaluate(ctx_check_fail)).passed is False

        ctx_verified = ContractContext(
            run=run,
            workspace_path=tmp_path,
            verification=VerificationResult("abc", "diff --git ...", "verified", "pytest", 0),
        )
        assert (await VerificationChecksPassedClause().evaluate(ctx_verified)).passed is True


class TestProfileExtensionAndLoopEnforcement:
    """Verify Q&A vs coding contracts, remediation inside react_loop, and persistence."""

    @pytest.mark.asyncio
    async def test_qa_vs_coding_fix_contract_on_read_only_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs_dir = tmp_path / "runs"
        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary", lambda *_a: tmp_path / "s.json"
        )

        repo = _make_git_repo(tmp_path)
        sb = _WorkspaceSandbox(repo)

        llm_qa = AsyncMock()
        llm_qa.complete.return_value = LLMResponse(
            content="app.py sets VALUE = 1.",
            tool_calls=[],
            usage=TokenUsage(10, 5, 15),
            finish_reason="stop",
        )
        qa_run = AgentRun(task="What is VALUE?", repo_path=str(repo), config=AgentConfig())
        res_qa = await react_loop(
            qa_run, llm_qa, ToolRegistry(), sb, completion_contract=qa_contract()
        )
        assert res_qa.state == RunState.COMPLETED
        assert res_qa.contract_evaluation is not None
        assert res_qa.contract_evaluation.passed is True

        # Same read-only behavior with coding_fix_contract -> VERIFICATION_FAILED!
        llm_fix = AsyncMock()
        llm_fix.complete.return_value = LLMResponse(
            content="Looks fine.", tool_calls=[], usage=TokenUsage(10, 5, 15), finish_reason="stop"
        )
        fix_run = AgentRun(
            task="Change VALUE to 2 and test", repo_path=str(repo), config=AgentConfig()
        )
        journal = RunJournal(fix_run.id, base_dir=runs_dir)
        res_fix = await react_loop(
            fix_run,
            llm_fix,
            ToolRegistry(),
            sb,
            journal=journal,
            completion_contract=coding_fix_contract(has_verifier=False),
        )
        assert res_fix.state == RunState.VERIFICATION_FAILED
        assert res_fix.contract_evaluation is not None
        assert res_fix.contract_evaluation.passed is False
        unmet_ids = [c.clause_id for c in res_fix.contract_evaluation.unmet_clauses]
        assert "changes_exist" in unmet_ids
        assert "checks_executed" in unmet_ids

        # Persistence + journal replay preserve VERIFICATION_FAILED & contract_evaluation.
        loaded = load_run(fix_run.id, base_dir=runs_dir)
        assert loaded.state == RunState.VERIFICATION_FAILED
        assert loaded.contract_evaluation is not None
        assert loaded.contract_evaluation.contract_id == "coding-fix-v1"
        replay = replay_journal(fix_run.id, base_dir=runs_dir)
        assert replay.valid is True
        assert replay.state_sequence == ["pending", "running", "verification_failed"]
        assert replay.contract_evaluation is not None

    @pytest.mark.asyncio
    async def test_bounded_remediation_succeeds_then_exhausts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs_dir = tmp_path / "runs"
        monkeypatch.setattr(
            "agent_forge.agent.core.save_run", lambda r: save_run(r, base_dir=runs_dir)
        )
        monkeypatch.setattr(
            "agent_forge.observability.cost.save_summary", lambda *_a: tmp_path / "s.json"
        )

        repo = _make_git_repo(tmp_path)
        registry = ToolRegistry()
        registry.register(WriteFileTool())
        sb = _WorkspaceSandbox(repo)

        # Turn 1 stops without artifact -> remediates -> writes valid report -> COMPLETED.
        llm = AsyncMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="Done analyzing.",
                tool_calls=[],
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c-write",
                        name="write_file",
                        arguments={
                            "path": "/workspace/.agent-forge/report.json",
                            "content": json.dumps(_valid_poa_payload("r-ok")),
                        },
                    )
                ],
                usage=TokenUsage(10, 10, 20),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="Wrote report.",
                tool_calls=[],
                usage=TokenUsage(10, 5, 15),
                finish_reason="stop",
            ),
        ]
        contract = build_poa_completion_contract(max_remediation_attempts=1)
        run = AgentRun(task="Analyze", repo_path=str(repo), config=AgentConfig(max_iterations=5))
        res = await react_loop(run, llm, registry, sb, completion_contract=contract)
        assert res.state == RunState.COMPLETED
        assert res.contract_evaluation is not None
        assert res.contract_evaluation.attempt == 2
        assert res.contract_evaluation.passed is True

        # Exhausted remediation ends in VERIFICATION_FAILED.
        repo2 = tmp_path / "repo2"
        repo2.mkdir()
        llm_fail = AsyncMock()
        llm_fail.complete.side_effect = [
            LLMResponse(
                content="Stop 1", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
            ),
            LLMResponse(
                content="Stop 2", tool_calls=[], usage=TokenUsage(5, 5, 10), finish_reason="stop"
            ),
        ]
        run2 = AgentRun(task="Analyze", repo_path=str(repo2), config=AgentConfig(max_iterations=5))
        res2 = await react_loop(
            run2, llm_fail, registry, _WorkspaceSandbox(repo2), completion_contract=contract
        )
        assert res2.state == RunState.VERIFICATION_FAILED
        assert res2.contract_evaluation is not None
        assert res2.contract_evaluation.attempt == 2
        assert res2.contract_evaluation.unmet_clauses[0].error_code == "report_generation_failed"

    @pytest.mark.asyncio
    async def test_poa_extension_contract_and_entry_point_discovery(self, tmp_path: Path) -> None:
        @dataclass
        class _FakeEntryPoint:
            name: str
            value: object

            def load(self) -> object:
                return self.value

        custom_contract = CompletionContract(
            id="custom-ext-v1",
            clauses=(
                JsonArtifactClause(
                    relative_path=".agent-forge/custom.json",
                    required_fields=("schema_version", "status"),
                ),
            ),
        )

        def _fake_eps(group: str) -> list[Any]:
            if group == CONTRACT_PLUGIN_GROUP:
                return [_FakeEntryPoint("custom-ext", lambda: custom_contract)]
            return []

        discovered = discover_extension_contracts(entry_points_factory=_fake_eps)
        assert discovered["custom-ext"] == custom_contract
        assert discovered["custom-ext-v1"] == custom_contract

        profile = AgentProfile(
            id="ext-profile",
            name="Ext",
            completion_contract=CompletionContractConfig(
                kind="extension", contract_id="custom-ext-v1"
            ),
        )
        resolved = resolve_completion_contract(profile=profile, extension_contracts=discovered)
        assert resolved == custom_contract

        # Proof-of-Audit profiles load with entrypoint resolving to PoA report contract.
        poa_dir = Path(__file__).resolve().parents[2] / "plugins" / "proof_of_audit" / "profiles"
        profiles = load_profiles([poa_dir], include_builtins=False, discover_entry_points=False)
        full_profile = profiles["full-spectrum"]
        poa_contract = resolve_completion_contract(profile=full_profile, extension_contracts={})
        assert poa_contract is not None
        assert poa_contract.id == POA_REPORT_SCHEMA_VERSION

        # Partial report (missing PoA fields) fails PoA schema validation.
        ws = tmp_path / "ws"
        report_file = ws / ".agent-forge" / "report.json"
        report_file.parent.mkdir(parents=True)
        report_file.write_text(
            json.dumps(
                {"schema_version": POA_REPORT_SCHEMA_VERSION, "run_id": "r1", "summary": "hi"}
            ),
            encoding="utf-8",
        )
        eval_bad = await poa_contract.evaluate(
            ContractContext(
                run=AgentRun(task="t", repo_path=str(ws), config=AgentConfig()), workspace_path=ws
            )
        )
        assert eval_bad.passed is False
        assert eval_bad.unmet_clauses[0].error_code == "report_schema_invalid"

        report_file.write_text(json.dumps(_valid_poa_payload("r1")), encoding="utf-8")
        eval_good = await poa_contract.evaluate(
            ContractContext(
                run=AgentRun(task="t", repo_path=str(ws), config=AgentConfig()), workspace_path=ws
            )
        )
        assert eval_good.passed is True


def test_cli_contract_flag_and_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agent-forge run --contract coding-fix` exits 1 and reports unmet clauses on no-op."""
    monkeypatch.setattr("agent_forge.cli.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.agent.persistence.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr("agent_forge.config.USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "agent_forge.observability.cost.save_summary", lambda *_a: tmp_path / "s.json"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    repo = _make_git_repo(tmp_path)
    llm = AsyncMock()
    llm.complete.return_value = LLMResponse(
        content="Nothing to do.", tool_calls=[], usage=TokenUsage(10, 5, 15), finish_reason="stop"
    )
    llm.close = AsyncMock()
    sb = _WorkspaceSandbox(repo)
    runner_options: dict[str, Any] = (
        {"mix_stderr": False} if "mix_stderr" in inspect.signature(CliRunner).parameters else {}
    )
    runner = CliRunner(**runner_options)

    with (
        patch("agent_forge.sandbox.factory.create_sandbox", return_value=sb),
        patch("agent_forge.cli.create_provider", return_value=llm),
    ):
        res_fail = runner.invoke(
            cli_main,
            [
                "run",
                "--task",
                "Fix code",
                "--repo",
                str(repo),
                "--contract",
                "coding-fix",
                "--output-format",
                "json",
            ],
        )
        assert res_fail.exit_code == 1
        payload_fail = json.loads(res_fail.stdout)
        assert payload_fail["state"] == "verification_failed"
        assert payload_fail["completion_contract"]["passed"] is False
        assert [c["clause_id"] for c in payload_fail["completion_contract"]["unmet_clauses"]] == [
            "changes_exist",
            "checks_executed",
        ]

        res_qa = runner.invoke(
            cli_main,
            [
                "run",
                "--task",
                "Explain repo",
                "--repo",
                str(repo),
                "--contract",
                "qa",
                "--output-format",
                "json",
            ],
        )
        assert res_qa.exit_code == 0, res_qa.output
        assert json.loads(res_qa.stdout)["state"] == "completed"
