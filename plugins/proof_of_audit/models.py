"""Pydantic models for Proof-of-Audit challenge evidence payloads."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


def max_severity(severities: list[str]) -> SeverityLevel:
    """Return the highest severity from a list."""
    if not severities:
        return "low"
    return max(severities, key=severity_rank)  # type: ignore[return-value]
