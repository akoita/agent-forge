"""Proof-of-Audit hosted service client compatibility adapter."""

from __future__ import annotations

from typing import Any

from agent_forge.service.client import BaseHostedServiceClient
from agent_forge.service.models import (
    ArtifactPolicy,
    ClientRef,
    ProfileRef,
    RunRequest,
    SourceKind,
    SourceRef,
    TargetRef,
)
from plugins.proof_of_audit.models import ProofOfAuditReport, compute_report_stats


def build_proof_of_audit_request(
    *,
    request_id: str,
    service_id: str,
    source_uri: str,
    source_kind: SourceKind,
    entry_contract: str,
    contract_address: str,
    network: str,
    chain_id: int,
    source_digest: str | None = None,
) -> RunRequest:
    """Build the hosted run payload expected by Proof-of-Audit-style clients."""
    return RunRequest(
        schema_version="agent-forge-run-request-v1",
        client=ClientRef(
            name="proof-of-audit",
            request_id=request_id,
            service_id=service_id,
        ),
        profile=ProfileRef(
            id="proof-of-audit-solidity-v1",
            report_schema="proof-of-audit-report-v1",
        ),
        source=SourceRef(
            kind=source_kind,
            uri=source_uri,
            entry_contract=entry_contract,
            source_digest=source_digest,
        ),
        target=TargetRef(
            submission_kind="deployed_address",
            network=network,
            chain_id=chain_id,
            contract_address=contract_address,
        ),
        artifacts=ArtifactPolicy(result_delivery="pull", include_logs=True),
    )


class ProofOfAuditHostedClient(BaseHostedServiceClient[ProofOfAuditReport]):
    """Async compatibility client for Proof-of-Audit-style hosted runs."""

    def _validate_report(self, payload: dict[str, Any]) -> ProofOfAuditReport:
        enriched = dict(payload)
        if "stats" not in enriched:
            findings = enriched.get("findings", [])
            enriched["stats"] = compute_report_stats(findings).model_dump()
        return ProofOfAuditReport.model_validate(enriched)
