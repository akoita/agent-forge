"""Internal authentication and policy models for hosted service mode."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

AllowedSourceKind = Literal["archive_uri", "repository_uri", "git_repository", "local_path"]

if TYPE_CHECKING:
    from pathlib import Path


def _default_allowed_source_kinds() -> list[AllowedSourceKind]:
    return ["archive_uri", "repository_uri", "git_repository"]


class ServiceClientPolicy(BaseModel):
    """Configuration for a single hosted-service client."""

    model_config = ConfigDict(extra="forbid")

    api_key_env: str
    allowed_profiles: list[str] = Field(default_factory=list)
    allowed_report_schemas: list[str]
    allowed_source_kinds: list[AllowedSourceKind] = Field(
        default_factory=_default_allowed_source_kinds
    )
    max_active_runs: int = Field(default=1, ge=1)
    max_runs_per_day: int = Field(default=25, ge=1)
    allow_local_path: bool = False


class ServiceClientRegistry(BaseModel):
    """Registry of externally authenticated hosted-service clients."""

    model_config = ConfigDict(extra="forbid")

    clients: dict[str, ServiceClientPolicy] = Field(default_factory=dict)


def load_client_registry(path: Path) -> dict[str, ServiceClientPolicy]:
    """Load client auth and policy configuration from a TOML file."""
    if not path.is_file():
        return {}

    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return ServiceClientRegistry.model_validate(raw).clients
