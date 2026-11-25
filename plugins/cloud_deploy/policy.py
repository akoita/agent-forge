"""Egress allowlist policies and mandatory plan-before-apply gates."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agent_forge.sandbox.egress_proxy import validate_egress_domain

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

CloudProvider = Literal["gcp", "aws", "terraform"]

GCP_EGRESS_DOMAINS: tuple[str, ...] = (
    "*.googleapis.com",
    "oauth2.googleapis.com",
    "sts.googleapis.com",
    "iamcredentials.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
)

AWS_EGRESS_DOMAINS: tuple[str, ...] = (
    "sts.amazonaws.com",
    "*.amazonaws.com",
    "*.ecr.amazonaws.com",
)

TERRAFORM_EGRESS_DOMAINS: tuple[str, ...] = (
    "registry.terraform.io",
    "releases.hashicorp.com",
    "checkpoint-api.hashicorp.com",
)

_PROVIDER_EGRESS_MAP: dict[str, tuple[str, ...]] = {
    "gcp": GCP_EGRESS_DOMAINS,
    "aws": AWS_EGRESS_DOMAINS,
    "terraform": TERRAFORM_EGRESS_DOMAINS,
}

FORBIDDEN_SANDBOX_CREDENTIAL_ENV_VARS: frozenset[str] = frozenset(
    {
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GCP_SA_KEY",
        "GOOGLE_CREDENTIALS",
        "ARM_CLIENT_SECRET",
        "AZURE_CLIENT_SECRET",
        "PRIVATE_KEY",
    }
)

_PRIVATE_KEY_MARKERS: tuple[str, ...] = (
    "BEGIN PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "private_key",
)


def build_sandbox_egress_policy(providers: Sequence[CloudProvider]) -> list[str]:
    """Return a deduplicated, validated ``allowed_egress_domains`` list for *providers*.

    Raises:
        ValueError: If an unknown provider name is passed or any rule fails validation.
    """
    seen: set[str] = set()
    domains: list[str] = []
    for raw_provider in providers:
        provider = raw_provider.strip().lower()
        entries = _PROVIDER_EGRESS_MAP.get(provider)
        if entries is None:
            msg = f"Unsupported cloud provider for egress policy: '{raw_provider}'"
            raise ValueError(msg)
        for rule in entries:
            validated = validate_egress_domain(rule)
            if validated not in seen:
                seen.add(validated)
                domains.append(validated)
    return domains


def validate_host_brokered_auth_env(env: Mapping[str, str]) -> None:
    """Reject static cloud credentials or private-key blobs inside sandbox env mappings.

    Raises:
        ValueError: If *env* contains forbidden long-lived credential variables or key material.
    """
    for key, value in env.items():
        if key in FORBIDDEN_SANDBOX_CREDENTIAL_ENV_VARS:
            msg = f"Forbidden long-lived cloud credential env var in sandbox: '{key}'"
            raise ValueError(msg)
        for marker in _PRIVATE_KEY_MARKERS:
            if marker in value:
                msg = f"Refusing embedded private-key material in sandbox env var '{key}'"
                raise ValueError(msg)


@dataclass(frozen=True)
class PlanRecord:
    """Recorded dry-run/plan inspection output required before an apply step."""

    plan_id: str
    provider: CloudProvider
    target: str
    summary: str
    plan_artifact: str | None = None
    fingerprint: str = ""


class PlanGateTracker:
    """In-memory registry enforcing plan/dry-run -> diff inspection -> gated apply."""

    def __init__(self) -> None:
        self._records: dict[str, PlanRecord] = {}

    def record(
        self,
        provider: CloudProvider,
        target: str,
        summary: str,
        *,
        plan_artifact: str | None = None,
        fingerprint: str = "",
    ) -> PlanRecord:
        """Store a newly inspected plan record and return it with a unique ``plan_id``."""
        plan_id = f"{provider}-{uuid.uuid4().hex[:12]}"
        rec = PlanRecord(
            plan_id=plan_id,
            provider=provider,
            target=target,
            summary=summary,
            plan_artifact=plan_artifact,
            fingerprint=fingerprint,
        )
        self._records[plan_id] = rec
        return rec

    def require(
        self,
        plan_id: str,
        *,
        provider: CloudProvider,
        target: str,
    ) -> PlanRecord:
        """Return the matching :class:`PlanRecord` or raise ``ValueError``."""
        cleaned = plan_id.strip()
        if not cleaned:
            msg = f"Missing plan_id for {provider} apply; run the matching plan/dry-run tool first."
            raise ValueError(msg)
        rec = self._records.get(cleaned)
        if rec is None:
            msg = (
                f"Unknown or already consumed plan_id '{cleaned}'; "
                "run plan/dry-run and inspect diff before applying."
            )
            raise ValueError(msg)
        if rec.provider != provider:
            msg = (
                f"Plan '{cleaned}' belongs to provider '{rec.provider}', "
                f"cannot apply via '{provider}'."
            )
            raise ValueError(msg)
        if rec.target != target:
            msg = f"Plan '{cleaned}' targets '{rec.target}', not '{target}'."
            raise ValueError(msg)
        return rec

    def consume(self, plan_id: str) -> None:
        """Mark *plan_id* consumed so an executed plan cannot be replayed blindly."""
        self._records.pop(plan_id.strip(), None)


_DEFAULT_TRACKER = PlanGateTracker()


def default_tracker() -> PlanGateTracker:
    """Return the shared default :class:`PlanGateTracker` instance."""
    return _DEFAULT_TRACKER
