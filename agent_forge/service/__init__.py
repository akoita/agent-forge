"""Hosted service package for agent-forge."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_forge.service.client import (
    BaseHostedServiceClient,
    HostedServiceClient,
    HostedServiceClientError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from agent_forge.config import ForgeConfig

__all__ = [
    "BaseHostedServiceClient",
    "HostedServiceClient",
    "HostedServiceClientError",
    "create_app",
]


def create_app(
    *,
    service_root: Path | None = None,
    runs_dir: Path | None = None,
    config: ForgeConfig | None = None,
    instance_id: str | None = None,
    persona: str | None = None,
) -> FastAPI:
    """Create the hosted service ASGI app (requires the ``service`` extra)."""
    try:
        from agent_forge.service.app import create_app as _create_app
    except ImportError as exc:
        msg = (
            "Hosted service requires optional dependencies. "
            "Install with: pip install 'agent-forge[service]'"
        )
        raise RuntimeError(msg) from exc
    return _create_app(
        service_root=service_root,
        runs_dir=runs_dir,
        config=config,
        instance_id=instance_id,
        persona=persona,
    )
