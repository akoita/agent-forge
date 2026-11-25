"""Host-brokered short-lived OIDC/WIF credential helper for cloud deployments."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from plugins.cloud_deploy.policy import validate_host_brokered_auth_env

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_SHORT_LIVED_TTL_SECONDS = 3600
_MIN_SHORT_LIVED_TTL_SECONDS = 60
_MIN_SCRUB_SECRET_LEN = 8
_REDACTED_MARKER = "[REDACTED_TOKEN]"


@dataclass(frozen=True)
class ShortLivedAuthConfig:
    """Configuration for minting a host-brokered ephemeral cloud credential."""

    provider: Literal["gcp_wif", "aws_sts_oidc"]
    audience: str
    role_or_service_account: str
    ttl_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.audience.strip():
            msg = "OIDC/WIF audience must not be empty"
            raise ValueError(msg)
        if not self.role_or_service_account.strip():
            msg = "Target role or service account must not be empty"
            raise ValueError(msg)
        if not (_MIN_SHORT_LIVED_TTL_SECONDS <= self.ttl_seconds <= MAX_SHORT_LIVED_TTL_SECONDS):
            msg = (
                f"ttl_seconds must be between {_MIN_SHORT_LIVED_TTL_SECONDS} and "
                f"{MAX_SHORT_LIVED_TTL_SECONDS} (got {self.ttl_seconds})"
            )
            raise ValueError(msg)


@dataclass(frozen=True)
class BrokeredToken:
    """Ephemeral cloud token issued via host-side OIDC/WIF exchange."""

    provider: str
    token: str
    expires_in_seconds: int
    principal: str


TokenExchanger = Callable[[ShortLivedAuthConfig], BrokeredToken]


class HostAuthBroker:
    """Mint short-lived OIDC/WIF credentials on the host and scrub token leaks."""

    def __init__(self, exchanger: TokenExchanger | None = None) -> None:
        self._exchanger = exchanger

    def mint_ephemeral_token(
        self,
        config: ShortLivedAuthConfig,
        *,
        sandbox_env: Mapping[str, str] | None = None,
    ) -> BrokeredToken:
        """Validate sandbox env boundaries, enforce TTL caps, and run the OIDC/WIF exchanger."""
        validate_host_brokered_auth_env(sandbox_env or {})
        if config.ttl_seconds > MAX_SHORT_LIVED_TTL_SECONDS:
            msg = f"Requested token TTL exceeds limit ({MAX_SHORT_LIVED_TTL_SECONDS}s)"
            raise ValueError(msg)
        if self._exchanger is None:
            msg = (
                "Host-side OIDC/WIF token exchanger is not configured; "
                "configure GCP Workload Identity Federation or AWS STS OIDC on the host."
            )
            raise RuntimeError(msg)
        return self._exchanger(config)

    @staticmethod
    def scrub_secrets(text: str, tokens: Sequence[str]) -> str:
        """Redact issued ephemeral token values (>= 8 chars) from tool or log output."""
        cleaned = text
        for token in tokens:
            if len(token) >= _MIN_SCRUB_SECRET_LEN:
                cleaned = cleaned.replace(token, _REDACTED_MARKER)
        return cleaned
