"""Proof-of-Audit plugin for Agent Forge."""

from plugins.proof_of_audit.client import (
    ProofOfAuditHostedClient,
    build_proof_of_audit_request,
)
from plugins.proof_of_audit.models import (
    Finding,
    ProofOfAuditReport,
    ReportStats,
    SeverityBreakdown,
    SeverityLevel,
    compute_report_stats,
)
from plugins.proof_of_audit.prompts import build_hosted_poa_system_prompt

__all__ = [
    "Finding",
    "ProofOfAuditHostedClient",
    "ProofOfAuditReport",
    "ReportStats",
    "SeverityBreakdown",
    "SeverityLevel",
    "build_hosted_poa_system_prompt",
    "build_proof_of_audit_request",
    "compute_report_stats",
]
