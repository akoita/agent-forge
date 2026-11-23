"""Composable per-run completion contracts and harness-evaluated verification clauses."""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from agent_forge.agent.models import AgentRun
    from agent_forge.agent.verification import VerificationResult
    from agent_forge.profiles.profile import AgentProfile, CompletionContractConfig
    from agent_forge.sandbox.base import Sandbox

CONTRACT_SCHEMA_VERSION: Literal["completion-contract-v1"] = "completion-contract-v1"
_MUTATING_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "apply_patch"})
_SHELL_CHECK_TOOLS: frozenset[str] = frozenset({"run_shell", "shell_start"})
_ENTRYPOINT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


class ClauseResult(BaseModel):
    """Evaluation outcome for a single completion contract clause."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    description: str
    passed: bool
    detail: str = ""
    error_code: str | None = None


class ContractEvaluation(BaseModel):
    """Aggregate evaluation of a :class:`CompletionContract` for an agent run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["completion-contract-v1"] = CONTRACT_SCHEMA_VERSION
    contract_id: str
    passed: bool
    clauses: list[ClauseResult] = Field(default_factory=list)
    unmet_clauses: list[ClauseResult] = Field(default_factory=list)
    attempt: int = Field(default=1, ge=1)

    @property
    def summary_reason(self) -> str:
        """Return a concise summary of unmet clauses (or ``verified`` if satisfied)."""
        if self.passed or not self.unmet_clauses:
            return "verified"
        first = self.unmet_clauses[0]
        names = ", ".join(clause.clause_id for clause in self.unmet_clauses)
        return f"Contract '{self.contract_id}' unmet ({names}): {first.detail}"


@dataclass
class ContractContext:
    """Harness-controlled evidence inputs passed to each contract clause."""

    run: AgentRun
    sandbox: Sandbox | None = None
    workspace_path: Path | None = None
    verification: VerificationResult | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ContractClause(ABC):
    """Abstract base class for composable completion verification clauses."""

    @property
    @abstractmethod
    def clause_id(self) -> str:
        """Stable machine identifier for this clause."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable statement of the requirement."""
        ...

    @abstractmethod
    async def evaluate(self, context: ContractContext) -> ClauseResult:
        """Evaluate this clause against harness-captured run state."""
        ...


def _workspace_has_uncommitted_diff(workspace: Path | None) -> bool:
    if workspace is None or not (workspace / ".git").exists():
        return False
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    res = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],  # noqa: S607
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return res.returncode == 0 and bool(res.stdout.strip())


def _has_successful_mutation_tool(run: AgentRun) -> bool:
    for inv in run.tool_invocations:
        if inv.tool_name not in _MUTATING_TOOLS or inv.result.exit_code != 0:
            continue
        if inv.tool_name == "apply_patch" and bool(inv.result.metadata.get("dry_run", False)):
            continue
        return True
    return False


class ChangesExistClause(ContractClause):
    """Require at least one non-empty file modification produced by the run."""

    @property
    def clause_id(self) -> str:
        return "changes_exist"

    @property
    def description(self) -> str:
        return "Workspace contains requested code or file changes"

    async def evaluate(self, context: ContractContext) -> ClauseResult:
        if context.verification is not None:
            has_patch = bool(context.verification.patch.strip())
            return ClauseResult(
                clause_id=self.clause_id,
                description=self.description,
                passed=has_patch,
                detail="Patch contains workspace changes."
                if has_patch
                else "No workspace changes produced.",
                error_code=None if has_patch else "empty_patch",
            )

        changed = _has_successful_mutation_tool(context.run) or _workspace_has_uncommitted_diff(
            context.workspace_path or Path(context.run.repo_path)
        )
        return ClauseResult(
            clause_id=self.clause_id,
            description=self.description,
            passed=changed,
            detail="Workspace modifications recorded."
            if changed
            else "No workspace changes produced.",
            error_code=None if changed else "empty_patch",
        )


class PatchAppliesCleanlyClause(ContractClause):
    """Require the candidate patch to apply cleanly onto an isolated base checkout."""

    @property
    def clause_id(self) -> str:
        return "patch_applies_cleanly"

    @property
    def description(self) -> str:
        return "Candidate patch applies cleanly to the base checkout"

    async def evaluate(self, context: ContractContext) -> ClauseResult:
        ver = context.verification
        if ver is not None and ver.reason in ("verified", "check_failed"):
            return ClauseResult(
                clause_id=self.clause_id,
                description=self.description,
                passed=True,
                detail="Patch applied cleanly in isolated checkout.",
            )
        reason = ver.reason if ver is not None else "empty_patch"
        detail = (
            "No patch to apply."
            if reason == "empty_patch"
            else f"Patch application or verification setup failed ({reason})."
        )
        return ClauseResult(
            clause_id=self.clause_id,
            description=self.description,
            passed=False,
            detail=detail,
            error_code=reason or "patch_apply_failed",
        )


class ChecksExecutedClause(ContractClause):
    """Require verification checks or test commands to have executed."""

    @property
    def clause_id(self) -> str:
        return "checks_executed"

    @property
    def description(self) -> str:
        return "Required verification checks executed and recorded output"

    async def evaluate(self, context: ContractContext) -> ClauseResult:
        if context.verification is not None:
            ran = context.verification.exit_code is not None
            return ClauseResult(
                clause_id=self.clause_id,
                description=self.description,
                passed=ran,
                detail=f"Independent check exited with code {context.verification.exit_code}."
                if ran
                else f"Verification checks did not run ({context.verification.reason}).",
                error_code=None if ran else "checks_not_run",
            )

        ran_shell = any(inv.tool_name in _SHELL_CHECK_TOOLS for inv in context.run.tool_invocations)
        return ClauseResult(
            clause_id=self.clause_id,
            description=self.description,
            passed=ran_shell,
            detail="Shell check command executed."
            if ran_shell
            else "No check or test command was executed during the run.",
            error_code=None if ran_shell else "checks_not_run",
        )


class VerificationChecksPassedClause(ContractClause):
    """Require independent verification checks to pass with exit code 0."""

    @property
    def clause_id(self) -> str:
        return "verification_checks_passed"

    @property
    def description(self) -> str:
        return "Independent verification checks passed"

    async def evaluate(self, context: ContractContext) -> ClauseResult:
        ver = context.verification
        if ver is not None and ver.passed:
            return ClauseResult(
                clause_id=self.clause_id,
                description=self.description,
                passed=True,
                detail="Independent verification command succeeded.",
            )
        reason = ver.reason if ver is not None else "checks_not_run"
        detail = (
            ver.stderr.strip() or ver.stdout.strip() or f"Verification failed ({reason})."
            if ver is not None
            else "Independent verification result missing."
        )
        return ClauseResult(
            clause_id=self.clause_id,
            description=self.description,
            passed=False,
            detail=detail,
            error_code=reason or "check_failed",
        )


def _final_assistant_text(run: AgentRun) -> str:
    for msg in reversed(run.messages):
        if getattr(msg.role, "value", msg.role) == "assistant" and not msg.tool_calls:
            return msg.content.lower()
    return ""


class NoUnacknowledgedFailuresClause(ContractClause):
    """Ensure tool or shell failures were recovered or (optionally) acknowledged."""

    def __init__(self, *, allow_acknowledged: bool = False) -> None:
        self._allow_acknowledged = allow_acknowledged

    @property
    def clause_id(self) -> str:
        return "no_unacknowledged_failures"

    @property
    def description(self) -> str:
        return "No unresolved or unacknowledged tool execution failures"

    async def evaluate(self, context: ContractContext) -> ClauseResult:
        unresolved: dict[str, int] = {}
        for inv in context.run.tool_invocations:
            if inv.result.metadata.get("approval_denied"):
                continue
            if inv.result.exit_code != 0:
                unresolved[inv.tool_name] = inv.result.exit_code
            else:
                unresolved.pop(inv.tool_name, None)

        if context.verification is not None and context.verification.passed:
            for shell_tool in _SHELL_CHECK_TOOLS:
                unresolved.pop(shell_tool, None)

        if self._allow_acknowledged and unresolved:
            final_text = _final_assistant_text(context.run)
            if final_text and ("error" in final_text or "fail" in final_text):
                unresolved = {
                    name: code
                    for name, code in unresolved.items()
                    if name.lower() not in final_text
                }

        if not unresolved:
            return ClauseResult(
                clause_id=self.clause_id,
                description=self.description,
                passed=True,
                detail="All tool executions succeeded or recovered.",
            )

        summary = ", ".join(f"{name} (exit {code})" for name, code in unresolved.items())
        return ClauseResult(
            clause_id=self.clause_id,
            description=self.description,
            passed=False,
            detail=f"Unresolved tool failure(s): {summary}",
            error_code="unacknowledged_tool_failure",
        )


ArtifactValidator = Callable[[dict[str, Any]], list[str] | None]


def evaluate_json_artifact_raw(
    raw: str | None,
    *,
    relative_path: str,
    required_fields: Sequence[str] = (),
    expected_schema_version: str | None = None,
    validator: ArtifactValidator | None = None,
    missing_error_code: str = "artifact_missing",
    invalid_json_error_code: str = "artifact_invalid_json",
    schema_error_code: str = "artifact_schema_invalid",
    clause_id: str = "artifact_json_valid",
    description: str = "Required JSON artifact exists and validates against schema",
) -> ClauseResult:
    """Validate raw text from a required JSON artifact synchronously."""
    filename = Path(relative_path).name
    if raw is None:
        return ClauseResult(
            clause_id=clause_id,
            description=description,
            passed=False,
            detail=f"run completed without writing {relative_path}",
            error_code=missing_error_code,
        )

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return ClauseResult(
            clause_id=clause_id,
            description=description,
            passed=False,
            detail=f"{filename} exists but is not valid JSON: {exc}",
            error_code=invalid_json_error_code,
        )

    if not isinstance(payload, dict):
        return ClauseResult(
            clause_id=clause_id,
            description=description,
            passed=False,
            detail=f"{filename} must contain a JSON object",
            error_code=schema_error_code,
        )

    missing = set(required_fields) - set(payload.keys())
    if missing:
        return ClauseResult(
            clause_id=clause_id,
            description=description,
            passed=False,
            detail=f"{filename} missing required fields: {sorted(missing)}",
            error_code=schema_error_code,
        )

    if (
        expected_schema_version is not None
        and payload.get("schema_version") != expected_schema_version
    ):
        return ClauseResult(
            clause_id=clause_id,
            description=description,
            passed=False,
            detail=(
                f"{filename} schema_version expected '{expected_schema_version}', "
                f"got {payload.get('schema_version')!r}"
            ),
            error_code=schema_error_code,
        )

    if validator is not None:
        errors = validator(payload) or []
        if errors:
            return ClauseResult(
                clause_id=clause_id,
                description=description,
                passed=False,
                detail="; ".join(errors),
                error_code=schema_error_code,
            )

    return ClauseResult(
        clause_id=clause_id,
        description=description,
        passed=True,
        detail=f"Validated {relative_path}.",
    )


class JsonArtifactClause(ContractClause):
    """Require a JSON artifact file to exist and satisfy structural/schema checks."""

    def __init__(
        self,
        *,
        relative_path: str = ".agent-forge/report.json",
        required_fields: Sequence[str] = (),
        expected_schema_version: str | None = None,
        validator: ArtifactValidator | None = None,
        missing_error_code: str = "artifact_missing",
        invalid_json_error_code: str = "artifact_invalid_json",
        schema_error_code: str = "artifact_schema_invalid",
    ) -> None:
        self.relative_path = relative_path
        self.required_fields = tuple(required_fields)
        self.expected_schema_version = expected_schema_version
        self.validator = validator
        self.missing_error_code = missing_error_code
        self.invalid_json_error_code = invalid_json_error_code
        self.schema_error_code = schema_error_code

    @property
    def clause_id(self) -> str:
        return "artifact_json_valid"

    @property
    def description(self) -> str:
        return f"Artifact '{self.relative_path}' exists and validates"

    async def _read_raw_artifact(self, context: ContractContext) -> str | None:
        base_dir = context.workspace_path or (
            Path(context.run.repo_path) if context.run.repo_path else None
        )
        if base_dir is not None:
            candidate = base_dir / self.relative_path
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")

        if context.sandbox is not None and await context.sandbox.is_alive():
            sandbox_path = f"/workspace/{self.relative_path.lstrip('/')}"
            try:
                return await context.sandbox.read_file(sandbox_path)
            except (FileNotFoundError, OSError, RuntimeError, PermissionError):
                return None
        return None

    async def evaluate(self, context: ContractContext) -> ClauseResult:
        raw = await self._read_raw_artifact(context)
        return evaluate_json_artifact_raw(
            raw,
            relative_path=self.relative_path,
            required_fields=self.required_fields,
            expected_schema_version=self.expected_schema_version,
            validator=self.validator,
            missing_error_code=self.missing_error_code,
            invalid_json_error_code=self.invalid_json_error_code,
            schema_error_code=self.schema_error_code,
            clause_id=self.clause_id,
            description=self.description,
        )


@dataclass(frozen=True)
class CompletionContract:
    """Evaluated gate determining whether an agent run achieved verified completion."""

    id: str
    clauses: tuple[ContractClause, ...] = ()
    max_remediation_attempts: int = 0
    remediation_prompt: str | None = None

    async def evaluate(
        self,
        context: ContractContext,
        *,
        attempt: int = 1,
    ) -> ContractEvaluation:
        """Evaluate every clause in order and return a structured evaluation."""
        results = [await clause.evaluate(context) for clause in self.clauses]
        unmet = [res for res in results if not res.passed]
        return ContractEvaluation(
            contract_id=self.id,
            passed=len(unmet) == 0,
            clauses=results,
            unmet_clauses=unmet,
            attempt=attempt,
        )

    def build_remediation_message(self, evaluation: ContractEvaluation) -> str:
        """Build a concise user-role prompt describing unmet clauses for bounded retry."""
        bullets = "\n".join(
            f"- {clause.clause_id}: {clause.detail or clause.description}"
            for clause in evaluation.unmet_clauses
        )
        header = self.remediation_prompt or (
            "Completion verification failed for contract "
            f"'{self.id}'. Resolve the unmet requirements below before finishing:"
        )
        return f"{header}\n{bullets}" if bullets else header


def qa_contract(*, require_no_unacknowledged_failures: bool = False) -> CompletionContract:
    """Return a contract appropriate for read-only Q&A / exploration tasks."""
    clauses: tuple[ContractClause, ...] = (
        (NoUnacknowledgedFailuresClause(allow_acknowledged=True),)
        if require_no_unacknowledged_failures
        else ()
    )
    return CompletionContract(id="qa-v1", clauses=clauses)


def coding_fix_contract(
    *,
    has_verifier: bool = True,
    max_remediation_attempts: int = 0,
) -> CompletionContract:
    """Return a strict verification-aware contract for code-editing tasks."""
    clauses: list[ContractClause] = [ChangesExistClause()]
    if has_verifier:
        clauses.append(PatchAppliesCleanlyClause())
    clauses.append(ChecksExecutedClause())
    if has_verifier:
        clauses.append(VerificationChecksPassedClause())
    clauses.append(NoUnacknowledgedFailuresClause(allow_acknowledged=False))
    return CompletionContract(
        id="coding-fix-v1",
        clauses=tuple(clauses),
        max_remediation_attempts=max_remediation_attempts,
    )


def artifact_report_contract(
    *,
    relative_path: str = ".agent-forge/report.json",
    required_fields: Sequence[str] = ("schema_version", "run_id", "summary"),
    expected_schema_version: str | None = None,
    contract_id: str = "artifact-report-v1",
    validator: ArtifactValidator | None = None,
    max_remediation_attempts: int = 1,
    remediation_prompt: str | None = None,
) -> CompletionContract:
    """Return a contract requiring a valid structured JSON artifact on completion."""
    clause = JsonArtifactClause(
        relative_path=relative_path,
        required_fields=required_fields,
        expected_schema_version=expected_schema_version,
        validator=validator,
        missing_error_code="report_generation_failed",
        invalid_json_error_code="report_invalid_json",
        schema_error_code="report_schema_invalid",
    )
    return CompletionContract(
        id=contract_id,
        clauses=(clause,),
        max_remediation_attempts=max_remediation_attempts,
        remediation_prompt=remediation_prompt
        or (
            f"The run cannot finish until '{relative_path}' exists and validates against the "
            "required JSON schema. Write the valid JSON file now using write_file."
        ),
    )


def _instantiate_contract_entry(entry: Any) -> CompletionContract | None:
    if isinstance(entry, CompletionContract):
        return entry
    if callable(entry):
        built = entry()
        if isinstance(built, CompletionContract):
            return built
    return None


def _load_entrypoint_contract(entrypoint: str) -> CompletionContract | None:
    if not _ENTRYPOINT_RE.match(entrypoint):
        return None
    module_name, attr_name = entrypoint.split(":", 1)
    try:
        mod = importlib.import_module(module_name)
        target = getattr(mod, attr_name)
    except (ImportError, AttributeError):
        return None
    return _instantiate_contract_entry(target)


def _resolve_from_profile_config(
    cfg: CompletionContractConfig,
    extension_contracts: Mapping[str, Any],
    *,
    has_verifier: bool,
    report_schema: str | None,
) -> CompletionContract:
    if cfg.contract_id and cfg.contract_id in extension_contracts:
        loaded = _instantiate_contract_entry(extension_contracts[cfg.contract_id])
        if loaded is not None:
            return loaded

    if cfg.entrypoint:
        loaded = _load_entrypoint_contract(cfg.entrypoint)
        if loaded is not None:
            return loaded

    if cfg.kind == "coding_fix":
        return coding_fix_contract(
            has_verifier=has_verifier,
            max_remediation_attempts=cfg.max_remediation_attempts,
        )
    if cfg.kind == "artifact_report":
        return artifact_report_contract(
            relative_path=cfg.artifact_path or ".agent-forge/report.json",
            required_fields=cfg.required_fields or ("schema_version", "run_id", "summary"),
            expected_schema_version=cfg.expected_schema_version or report_schema,
            contract_id=cfg.contract_id or "artifact-report-v1",
            max_remediation_attempts=cfg.max_remediation_attempts,
            remediation_prompt=cfg.remediation_prompt,
        )
    return qa_contract(
        require_no_unacknowledged_failures=cfg.require_no_unacknowledged_failures,
    )


def resolve_completion_contract(
    *,
    explicit_contract: CompletionContract | None = None,
    profile: AgentProfile | None = None,
    contract_mode: str = "auto",
    has_verifier: bool = False,
    report_schema: str | None = None,
    extension_contracts: Mapping[str, Any] | None = None,
) -> CompletionContract | None:
    """Resolve the active :class:`CompletionContract` for a run."""
    if explicit_contract is not None:
        return explicit_contract

    normalized = contract_mode.strip().lower()
    if normalized == "qa":
        return qa_contract(require_no_unacknowledged_failures=True)
    if normalized == "coding-fix":
        return coding_fix_contract(has_verifier=has_verifier)

    registry = extension_contracts
    if registry is None:
        from agent_forge.extensions.discovery import discover_extension_contracts

        registry = discover_extension_contracts()

    if report_schema and report_schema in registry:
        loaded = _instantiate_contract_entry(registry[report_schema])
        if loaded is not None:
            return loaded

    if profile is not None and profile.completion_contract is not None:
        return _resolve_from_profile_config(
            profile.completion_contract,
            registry,
            has_verifier=has_verifier,
            report_schema=report_schema,
        )

    if has_verifier:
        return coding_fix_contract(has_verifier=True)
    if report_schema is not None:
        return artifact_report_contract(expected_schema_version=report_schema)
    return None
