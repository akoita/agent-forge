"""Proof-of-Audit completion contract and report validation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agent_forge.agent.contracts import CompletionContract, artifact_report_contract
from plugins.proof_of_audit.models import ProofOfAuditReport

if TYPE_CHECKING:
    from collections.abc import Sequence

POA_REPORT_SCHEMA_VERSION = "proof-of-audit-report-v1"
POA_REPORT_RELATIVE_PATH = ".agent-forge/report.json"
POA_REPORT_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "summary",
    "confidence",
    "findings",
    "stats",
)


def validate_poa_report_payload(payload: dict[str, Any]) -> list[str]:
    """Validate *payload* against :class:`ProofOfAuditReport` and return error details."""
    try:
        ProofOfAuditReport.model_validate(payload)
    except ValidationError as exc:
        return [f"Proof-of-Audit report schema invalid: {exc.errors()[0]['msg']}"]
    return []


def build_poa_completion_contract(
    *,
    max_remediation_attempts: int = 1,
    required_fields: Sequence[str] = POA_REPORT_REQUIRED_FIELDS,
) -> CompletionContract:
    """Build the domain completion contract requiring a valid Proof-of-Audit report."""
    return artifact_report_contract(
        relative_path=POA_REPORT_RELATIVE_PATH,
        required_fields=required_fields,
        expected_schema_version=POA_REPORT_SCHEMA_VERSION,
        contract_id=POA_REPORT_SCHEMA_VERSION,
        validator=validate_poa_report_payload,
        max_remediation_attempts=max_remediation_attempts,
        remediation_prompt=(
            "The security audit task finished without a valid Proof-of-Audit report at "
            f"{POA_REPORT_RELATIVE_PATH}. Write a valid JSON file matching schema_version "
            f"'{POA_REPORT_SCHEMA_VERSION}' using write_file now."
        ),
    )
