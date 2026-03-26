"""Client helpers for externally consuming the hosted agent-forge service."""

from __future__ import annotations

import asyncio
from typing import NoReturn, TypeVar

import httpx

from agent_forge.service.models import (
    ArtifactPolicy,
    ClientRef,
    ErrorResponse,
    ProfileRef,
    ProofOfAuditReport,
    RunRequest,
    RunStatus,
    SourceKind,
    SourceRef,
    TargetRef,
)

ModelT = TypeVar("ModelT", RunStatus, ProofOfAuditReport)


class HostedServiceClientError(RuntimeError):
    """Raised when the hosted service rejects or fails a client request."""

    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


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


class ProofOfAuditHostedClient:
    """Async compatibility client for Proof-of-Audit-style hosted runs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(base_url=base_url)

    async def aclose(self) -> None:
        """Close the underlying HTTP client if owned by this instance."""
        if self._owns_client:
            await self._client.aclose()

    async def submit_run(self, request: RunRequest) -> RunStatus:
        """Submit a hosted Proof-of-Audit run request."""
        response = await self._client.post(
            "/v1/runs",
            json=request.model_dump(mode="json"),
            headers=self._headers(),
        )
        return self._parse_response(response, RunStatus)

    async def get_status(self, run_id: str) -> RunStatus:
        """Fetch the current lifecycle state for a hosted run."""
        response = await self._client.get(
            f"/v1/runs/{run_id}",
            headers=self._headers(),
        )
        return self._parse_response(response, RunStatus)

    async def get_report(self, run_id: str) -> ProofOfAuditReport:
        """Fetch the machine-readable report for a completed run."""
        response = await self._client.get(
            f"/v1/runs/{run_id}/report",
            headers=self._headers(),
        )
        return self._parse_response(response, ProofOfAuditReport)

    async def wait_for_report(
        self,
        run_id: str,
        *,
        poll_interval_seconds: float = 0.05,
        max_polls: int = 100,
    ) -> ProofOfAuditReport:
        """Poll run status until a final report is available or the run fails."""
        for _ in range(max_polls):
            status = await self.get_status(run_id)
            if status.status == "completed":
                return await self.get_report(run_id)
            if status.status == "failed":
                error = status.error
                raise HostedServiceClientError(
                    status_code=409,
                    code=error.code if error is not None else None,
                    message=(
                        error.message
                        if error is not None
                        else "hosted service run failed without an error payload"
                    ),
                )
            await asyncio.sleep(poll_interval_seconds)

        msg = f"timed out waiting for hosted run: {run_id}"
        raise TimeoutError(msg)

    def _headers(self) -> dict[str, str]:
        return {"X-Agent-Forge-API-Key": self._api_key}

    def _parse_response(self, response: httpx.Response, model: type[ModelT]) -> ModelT:
        if response.is_success:
            return model.model_validate(response.json())
        self._raise_service_error(response)
        raise AssertionError("unreachable")

    def _raise_service_error(self, response: httpx.Response) -> NoReturn:
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text}

        detail = payload.get("detail", payload)
        if isinstance(detail, dict) and "error" in detail:
            error = ErrorResponse.model_validate(detail).error
            raise HostedServiceClientError(
                status_code=response.status_code,
                code=error.code,
                message=error.message,
            )

        raise HostedServiceClientError(
            status_code=response.status_code,
            code=None,
            message=str(detail),
        )
