"""Pydantic models for Proof-of-Audit reports and challenge evidence payloads."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from agent_forge.service.models import ReportProvenance, ReportTarget

if TYPE_CHECKING:
    from collections.abc import Sequence

ConfidenceLevel = Literal["low", "medium", "high"]
SeverityLevel = Literal["critical", "high", "medium", "low"]


class DivergenceType(StrEnum):
    """Classification of how the original report diverges from the challenger."""

    MISSED_VULNERABILITY = "missed_vulnerability"
    SEVERITY_DOWNGRADE = "severity_downgrade"
    FALSE_NEGATIVE = "false_negative"


class MissedFinding(BaseModel):
    """A single finding the original audit failed to report."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    title: str
    severity: SeverityLevel
    category: str
    confidence: ConfidenceLevel
    explanation: str


class ChallengeEvidence(BaseModel):
    """Structured challenge evidence payload for PoA dispute proposals.

    Output format compatible with ``POST /audits/{id}/challenge``.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_type: DivergenceType
    missed_findings: list[MissedFinding] = Field(default_factory=list)
    severity_summary: SeverityLevel
    challenge_confidence: ConfidenceLevel


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
    """Aggregate summary for a Proof-of-Audit report."""

    model_config = ConfigDict(extra="forbid")

    finding_count: int = Field(ge=0)
    max_severity: SeverityLevel | None = None
    severity_breakdown: SeverityBreakdown


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


def compute_report_stats(findings: Sequence[Finding | dict[str, Any]]) -> ReportStats:
    """Compute severity breakdown and max severity for Proof-of-Audit findings."""
    counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    severities: list[str] = []
    for item in findings:
        severity = item.severity if isinstance(item, Finding) else item.get("severity")
        if severity in counts:
            counts[severity] += 1
            severities.append(severity)
    highest: SeverityLevel | None = max_severity(severities) if severities else None
    return ReportStats(
        finding_count=len(findings),
        max_severity=highest,
        severity_breakdown=SeverityBreakdown(**counts),
    )


# ---------------------------------------------------------------------------
# Severity ordering (for comparison logic)
# ---------------------------------------------------------------------------

_SEVERITY_ORDER: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}


def severity_rank(severity: str) -> int:
    """Return a numeric rank for a severity level (higher = more severe)."""
    return _SEVERITY_ORDER.get(severity, 0)


def max_severity(severities: Sequence[str]) -> SeverityLevel:
    """Return the highest severity from a list."""
    if not severities:
        return "low"
    highest = max(severities, key=severity_rank)
    return cast("SeverityLevel", highest)
