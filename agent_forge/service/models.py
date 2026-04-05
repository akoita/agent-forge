"""Pydantic models for the hosted agent-forge service contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RunLifecycleStatus = Literal["accepted", "queued", "running", "completed", "failed", "cancelled"]
SourceKind = Literal["archive_uri", "repository_uri", "git_repository", "local_path"]
SubmissionKind = Literal["deployed_address", "source_bundle", "repository_url"]
ConfidenceLevel = Literal["low", "medium", "high"]
SeverityLevel = Literal["critical", "high", "medium", "low"]
DeliveryMode = Literal["pull", "callback"]
ArtifactKind = Literal["report", "logs", "run_metadata"]
ErrorCode = Literal[
    "invalid_request",
    "unsupported_profile",
    "unsupported_source_kind",
    "source_fetch_failed",
    "sandbox_start_failed",
    "sandbox_execution_failed",
    "report_generation_failed",
    "report_invalid_json",
    "report_schema_invalid",
    "policy_denied",
    "unauthorized",
    "quota_exceeded",
]


class ClientRef(BaseModel):
    """Identifies the external caller that submitted the run."""

    model_config = ConfigDict(extra="forbid")

    name: str
    request_id: str
    service_id: str | None = None


class ProfileRef(BaseModel):
    """Describes the policy or template to apply for a run."""

    model_config = ConfigDict(extra="forbid")

    id: str
    report_schema: str
    max_iterations: int | None = Field(default=None, ge=1)


class SourceRef(BaseModel):
    """Reference to the source material the service should analyze."""

    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    uri: str
    archive_format: Literal["zip", "tar.gz"] | None = None
    entry_contract: str | None = None
    source_digest: str | None = None


class TargetRef(BaseModel):
    """Optional metadata describing the target deployment."""

    model_config = ConfigDict(extra="forbid")

    submission_kind: SubmissionKind | None = None
    network: str | None = None
    chain_id: int | None = None
    contract_address: str | None = None


class ArtifactPolicy(BaseModel):
    """Controls how result artifacts should be delivered."""

    model_config = ConfigDict(extra="forbid")

    result_delivery: DeliveryMode | None = None
    include_logs: bool | None = None


class RunRequest(BaseModel):
    """Versioned submission payload for hosted runs."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent-forge-run-request-v1"]
    client: ClientRef
    profile: ProfileRef
    source: SourceRef
    target: TargetRef | None = None
    artifacts: ArtifactPolicy | None = None


class RunArtifactRef(BaseModel):
    """Reference to a machine-consumable artifact emitted by the run."""

    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    url: str
    available: bool
    content_type: str


class RunError(BaseModel):
    """Structured error payload for rejected or failed runs."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool


class RunStatus(BaseModel):
    """Versioned status document returned by the service."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent-forge-run-v1"] = "agent-forge-run-v1"
    run_id: str
    status: RunLifecycleStatus
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    client: ClientRef | None = None
    profile: ProfileRef | None = None
    status_url: str | None = None
    artifacts: list[RunArtifactRef] = Field(default_factory=list)
    error: RunError | None = None


class ErrorResponse(BaseModel):
    """Envelope returned for machine-readable failures."""

    model_config = ConfigDict(extra="forbid")

    error: RunError


class ReportTarget(BaseModel):
    """Identifies the report target in a downstream-friendly shape."""

    model_config = ConfigDict(extra="forbid")

    submission_kind: str | None = None
    network: str | None = None
    chain_id: int | None = None
    contract_address: str | None = None
    entry_contract: str | None = None


class Finding(BaseModel):
    """A single machine-readable audit finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    title: str
    severity: SeverityLevel
    category: str
    description: str
    impact: str
    recommendation: str
    confidence: ConfidenceLevel
    detector: str | None = None
    affected_function: str | None = None
    source_path: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    evidence_uri: str | None = None


class SeverityBreakdown(BaseModel):
    """Counts findings by severity."""

    model_config = ConfigDict(extra="forbid")

    critical: int = Field(ge=0)
    high: int = Field(ge=0)
    medium: int = Field(ge=0)
    low: int = Field(ge=0)


class ReportStats(BaseModel):
    """Aggregate summary for a report."""

    model_config = ConfigDict(extra="forbid")

    finding_count: int = Field(ge=0)
    max_severity: SeverityLevel | None = None
    severity_breakdown: SeverityBreakdown


class ReportProvenance(BaseModel):
    """Captures which profile and source digest produced the report."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str | None = None
    source_digest: str | None = None


class ProofOfAuditReport(BaseModel):
    """Stable report schema for Proof-of-Audit-compatible runs."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proof-of-audit-report-v1"]
    run_id: str
    summary: str
    confidence: ConfidenceLevel
    benchmark_id: str | None = None
    target: ReportTarget | None = None
    findings: list[Finding]
    stats: ReportStats
    provenance: ReportProvenance | None = None


class LogsResponse(BaseModel):
    """References the persisted artifacts for a completed run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    inline: str | None = None
    logs_url: str | None = None
    artifacts: dict[str, str]


class HealthResponse(BaseModel):
    """Readiness information for a hosted deployment."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    service_root: str
    queue_backend: str
    sandbox_image: str
    # Multi-instance persona metadata
    instance_id: str | None = None
    persona: str | None = None
    capabilities: list[str] = []
    llm_provider: str | None = None
