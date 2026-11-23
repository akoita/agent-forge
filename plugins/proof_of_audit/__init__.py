"""Proof-of-Audit plugin for Agent Forge."""

from plugins.proof_of_audit.client import (
    ProofOfAuditHostedClient,
    build_proof_of_audit_request,
)
from plugins.proof_of_audit.contracts import (
    POA_REPORT_SCHEMA_VERSION,
    build_poa_completion_contract,
    validate_poa_report_payload,
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
    "POA_REPORT_SCHEMA_VERSION",
    "Finding",
    "ProofOfAuditHostedClient",
    "ProofOfAuditReport",
    "ReportStats",
    "SeverityBreakdown",
    "SeverityLevel",
    "build_hosted_poa_system_prompt",
    "build_poa_completion_contract",
    "build_proof_of_audit_request",
    "compute_report_stats",
    "validate_poa_report_payload",
]
