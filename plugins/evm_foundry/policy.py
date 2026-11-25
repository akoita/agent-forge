"""EVM/Foundry sandbox policies, signer isolation, and simulation-before-broadcast gates."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_forge.sandbox.base import SandboxConfig
from agent_forge.sandbox.egress_proxy import validate_egress_domain

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

FOUNDRY_SANDBOX_IMAGE = "ghcr.io/foundry-rs/foundry:latest"

FORBIDDEN_SIGNER_ENV_VARS: frozenset[str] = frozenset(
    {
        "PRIVATE_KEY",
        "DEPLOYER_PRIVATE_KEY",
        "ETH_PRIVATE_KEY",
        "RAW_PRIVATE_KEY",
        "MNEMONIC",
        "SEED_PHRASE",
        "ETH_KEYSTORE_PASSWORD",
        "FOUNDRY_ETH_KEYSTORE",
    }
)

FORBIDDEN_SIGNER_FLAGS: tuple[str, ...] = (
    "--private-key",
    "--private-keys",
    "--mnemonic",
    "--mnemonics",
    "--raw-private-key",
    "--unlocked",
)

_PEM_KEY_MARKERS: tuple[str, ...] = ("BEGIN PRIVATE KEY", "BEGIN EC PRIVATE KEY")
_HEX_KEY_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_SECRET_NAME_TOKENS: tuple[str, ...] = ("KEY", "SECRET", "SIGNER", "SEED")


def build_foundry_sandbox_config(
    *,
    rpc_domains: Sequence[str] = (),
    image: str = FOUNDRY_SANDBOX_IMAGE,
    timeout_seconds: int = 300,
) -> SandboxConfig:
    """Build a Foundry :class:`SandboxConfig` (offline unless *rpc_domains* are declared)."""
    seen: set[str] = set()
    validated: list[str] = []
    for raw in rpc_domains:
        domain = validate_egress_domain(raw)
        if domain not in seen:
            seen.add(domain)
            validated.append(domain)
    return SandboxConfig(
        image=image,
        network_enabled=bool(validated),
        allowed_egress_domains=validated,
        timeout_seconds=timeout_seconds,
    )


def _check_secret_env_entry(key: str, value: str) -> None:
    if key in FORBIDDEN_SIGNER_ENV_VARS:
        msg = f"Forbidden signer/key env var inside sandbox: '{key}'"
        raise ValueError(msg)
    for marker in _PEM_KEY_MARKERS:
        if marker in value:
            msg = f"Refusing embedded private-key material in sandbox env var '{key}'"
            raise ValueError(msg)
    upper_key = key.upper()
    if any(tok in upper_key for tok in _SECRET_NAME_TOKENS) and _HEX_KEY_RE.fullmatch(
        value.strip()
    ):
        msg = f"Refusing raw 32-byte hex key material in sandbox env var '{key}'"
        raise ValueError(msg)


def validate_sandbox_signer_isolation(
    env: Mapping[str, str],
    cli_args: Sequence[str] = (),
) -> None:
    """Ensure private keys, mnemonics, and unlocked-signer flags never enter the sandbox."""
    for key, value in env.items():
        _check_secret_env_entry(key, value)
    for raw_arg in cli_args:
        arg = raw_arg.strip()
        for flag in FORBIDDEN_SIGNER_FLAGS:
            if arg == flag or arg.startswith(f"{flag}="):
                msg = f"Forbidden signer CLI flag inside sandbox: '{flag}'"
                raise ValueError(msg)


@dataclass(frozen=True)
class SimulationRecord:
    """Recorded fork simulation result required before ``forge script --broadcast``."""

    sim_id: str
    script_path: str
    target_contract: str
    rpc_alias: str
    working_dir: str
    summary: str
    chain_id: int | None = None
    estimated_gas: int | None = None


class SimulationGateTracker:
    """In-memory tracker enforcing ``forge_simulate_script`` -> inspection -> broadcast."""

    def __init__(self) -> None:
        self._records: dict[str, SimulationRecord] = {}

    def record(
        self,
        script_path: str,
        rpc_alias: str,
        working_dir: str,
        summary: str,
        *,
        target_contract: str = "",
        chain_id: int | None = None,
        estimated_gas: int | None = None,
    ) -> SimulationRecord:
        """Record a successful fork simulation and return its unique ``sim_id``."""
        sim_id = f"sim-{uuid.uuid4().hex[:12]}"
        rec = SimulationRecord(
            sim_id=sim_id,
            script_path=script_path,
            target_contract=target_contract,
            rpc_alias=rpc_alias,
            working_dir=working_dir,
            summary=summary,
            chain_id=chain_id,
            estimated_gas=estimated_gas,
        )
        self._records[sim_id] = rec
        return rec

    def require(
        self,
        sim_id: str,
        *,
        script_path: str,
        rpc_alias: str,
        working_dir: str,
        target_contract: str = "",
    ) -> SimulationRecord:
        """Return matching :class:`SimulationRecord` or raise ``ValueError``."""
        cleaned = sim_id.strip()
        if not cleaned:
            msg = "Missing sim_id; run 'forge_simulate_script' before broadcasting."
            raise ValueError(msg)
        rec = self._records.get(cleaned)
        if rec is None:
            msg = (
                f"Unknown or already consumed sim_id '{cleaned}'; "
                "run 'forge_simulate_script' and inspect simulation output first."
            )
            raise ValueError(msg)
        if (
            rec.script_path != script_path
            or rec.rpc_alias != rpc_alias
            or rec.working_dir != working_dir
            or rec.target_contract != target_contract
        ):
            msg = f"Simulation '{cleaned}' parameters do not match requested broadcast target."
            raise ValueError(msg)
        return rec

    def consume(self, sim_id: str) -> None:
        """Mark *sim_id* consumed so one broadcast cannot replay blindly."""
        self._records.pop(sim_id.strip(), None)


_DEFAULT_SIM_TRACKER = SimulationGateTracker()


def default_sim_tracker() -> SimulationGateTracker:
    """Return the module-level shared :class:`SimulationGateTracker`."""
    return _DEFAULT_SIM_TRACKER
