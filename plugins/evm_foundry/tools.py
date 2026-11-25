"""Foundry build, test, fork simulation, and approval-gated broadcast tools."""

from __future__ import annotations

import re
import shlex
import time
from typing import TYPE_CHECKING, Any, Literal

from agent_forge.tools.base import Tool, ToolResult, validate_path
from plugins.evm_foundry.policy import (
    SimulationGateTracker,
    default_sim_tracker,
    validate_sandbox_signer_isolation,
)

if TYPE_CHECKING:
    from agent_forge.sandbox.base import Sandbox

SignerMode = Literal["host_broker", "hardware_ledger", "hardware_trezor", "safe_multisig"]

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")
_SIG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\([A-Za-z0-9_,\[\]\s]*\)$")
_GAS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Estimated total gas used for script:\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bGas:\s*(\d+)", re.IGNORECASE),
)
_ALLOWED_SIGNER_MODES: frozenset[str] = frozenset(
    {"host_broker", "hardware_ledger", "hardware_trezor", "safe_multisig"}
)


def _validate_token(value: object, label: str) -> str:
    if not isinstance(value, str):
        msg = f"Missing required string argument: {label}"
        raise ValueError(msg)
    cleaned = value.strip()
    if not _TOKEN_RE.fullmatch(cleaned):
        msg = (
            f"Invalid {label} '{value}': only letters, digits, and '._:/@-' are permitted "
            "with no whitespace or shell metacharacters."
        )
        raise ValueError(msg)
    return cleaned


def _validate_sig(value: object) -> str:
    if not isinstance(value, str):
        msg = "sig must be a string function signature"
        raise ValueError(msg)
    cleaned = value.strip()
    if not _SIG_RE.fullmatch(cleaned):
        msg = f"Invalid function signature '{value}'"
        raise ValueError(msg)
    return cleaned


def _validate_workspace_dir(raw: object, *, default: str = "/workspace") -> str:
    candidate = default if raw is None else str(raw).strip()
    return validate_path(candidate or default)


def _format_exec(stdout: str, stderr: str) -> str:
    parts = [part.strip() for part in (stdout, stderr) if part and part.strip()]
    return "\n".join(parts)


def _extract_estimated_gas(output: str) -> int | None:
    for pattern in _GAS_PATTERNS:
        match = pattern.search(output)
        if match is not None:
            return int(match.group(1))
    return None


def _format_script_target(script_path: str, target_contract: str) -> str:
    return f"{script_path}:{target_contract}" if target_contract else script_path


class ForgeBuildTool(Tool):
    """Run offline ``forge build --sizes`` inside ``/workspace``."""

    def __init__(self, tracker: SimulationGateTracker | None = None) -> None:
        self._tracker = tracker or default_sim_tracker()

    @property
    def name(self) -> str:
        return "forge_build"

    @property
    def description(self) -> str:
        return (
            "Compile smart contracts cleanly inside the Foundry sandbox via 'forge build --sizes'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "working_dir": {
                    "type": "string",
                    "description": "Project directory under /workspace.",
                    "default": "/workspace",
                },
                "profile": {
                    "type": "string",
                    "description": "Optional Foundry profile from foundry.toml.",
                },
            },
        }

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Execute ``forge build --sizes`` inside *working_dir*."""
        started = time.monotonic()
        try:
            work_dir = _validate_workspace_dir(arguments.get("working_dir"))
            raw_profile = arguments.get("profile")
            profile = _validate_token(raw_profile, "profile") if raw_profile else None
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        tokens = ["forge", "build", "--sizes"]
        if profile is not None:
            tokens.extend(["--profile", profile])
        cmd = f"cd {shlex.quote(work_dir)} && {' '.join(shlex.quote(t) for t in tokens)}"
        res = await sandbox.exec(cmd, timeout_seconds=180)
        combined = _format_exec(res.stdout, res.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=combined,
                error=f"forge build failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )
        return ToolResult(output=combined, exit_code=0, execution_time_ms=elapsed_ms)


class ForgeTestTool(Tool):
    """Run ``forge test`` inside the Foundry sandbox."""

    def __init__(self, tracker: SimulationGateTracker | None = None) -> None:
        self._tracker = tracker or default_sim_tracker()

    @property
    def name(self) -> str:
        return "forge_test"

    @property
    def description(self) -> str:
        return "Execute Foundry unit/fuzz tests inside the sandbox via 'forge test'."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "working_dir": {"type": "string", "default": "/workspace"},
                "match_test": {"type": "string"},
                "match_contract": {"type": "string"},
                "match_path": {"type": "string"},
                "verbosity": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
            },
        }

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Run ``forge test`` with validated filter flags inside *working_dir*."""
        started = time.monotonic()
        try:
            work_dir = _validate_workspace_dir(arguments.get("working_dir"))
            verbosity = max(1, min(4, int(arguments.get("verbosity", 2))))
            tokens = ["forge", "test", "-" + ("v" * verbosity)]
            for key, flag in (
                ("match_test", "--match-test"),
                ("match_contract", "--match-contract"),
                ("match_path", "--match-path"),
            ):
                if arguments.get(key):
                    tokens.extend([flag, _validate_token(arguments[key], key)])
        except (TypeError, ValueError) as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        cmd = f"cd {shlex.quote(work_dir)} && {' '.join(shlex.quote(t) for t in tokens)}"
        res = await sandbox.exec(cmd, timeout_seconds=300)
        combined = _format_exec(res.stdout, res.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=combined,
                error=f"forge test failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )
        return ToolResult(output=combined, exit_code=0, execution_time_ms=elapsed_ms)


class ForgeSimulateScriptTool(Tool):
    """Simulate a Foundry deployment script without broadcasting or handling private keys."""

    def __init__(self, tracker: SimulationGateTracker | None = None) -> None:
        self._tracker = tracker or default_sim_tracker()

    @property
    def name(self) -> str:
        return "forge_simulate_script"

    @property
    def description(self) -> str:
        return (
            "Run a dry-run 'forge script' simulation (never '--broadcast') against a configured "
            "rpc_alias and record a sim_id required before 'forge_broadcast_script'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "script_path": {"type": "string"},
                "rpc_alias": {"type": "string"},
                "working_dir": {"type": "string", "default": "/workspace"},
                "target_contract": {"type": "string"},
                "sig": {"type": "string"},
                "chain_id": {"type": "integer", "minimum": 1},
            },
            "required": ["script_path", "rpc_alias"],
        }

    @property
    def side_effecting(self) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Execute a dry-run ``forge script`` simulation and record a :class:`SimulationRecord`."""
        started = time.monotonic()
        try:
            work_dir = _validate_workspace_dir(arguments.get("working_dir"))
            script_path = validate_path(str(arguments.get("script_path") or ""))
            rpc_alias = _validate_token(arguments.get("rpc_alias"), "rpc_alias")
            target_contract = (
                _validate_token(arguments.get("target_contract"), "target_contract")
                if arguments.get("target_contract")
                else ""
            )
            sig = _validate_sig(arguments["sig"]) if arguments.get("sig") else None
            raw_cid = arguments.get("chain_id")
            chain_id = int(raw_cid) if raw_cid is not None else None
            if chain_id is not None and chain_id < 1:
                msg = "chain_id must be >= 1"
                raise ValueError(msg)
        except (TypeError, ValueError) as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        tokens = [
            "forge",
            "script",
            _format_script_target(script_path, target_contract),
            "--rpc-url",
            rpc_alias,
            "--non-interactive",
        ]
        if sig is not None:
            tokens.extend(["--sig", sig])
        validate_sandbox_signer_isolation({}, tokens)

        cmd = f"cd {shlex.quote(work_dir)} && {' '.join(shlex.quote(t) for t in tokens)}"
        res = await sandbox.exec(cmd, timeout_seconds=300)
        combined = _format_exec(res.stdout, res.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=combined,
                error=f"forge script simulation failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )

        estimated_gas = _extract_estimated_gas(combined)
        rec = self._tracker.record(
            script_path,
            rpc_alias,
            work_dir,
            combined[:2000],
            target_contract=target_contract,
            chain_id=chain_id,
            estimated_gas=estimated_gas,
        )
        return ToolResult(
            output=f"[sim_id={rec.sim_id} broadcast=false rpc_alias={rpc_alias}]\n{combined}",
            exit_code=0,
            execution_time_ms=elapsed_ms,
            metadata={
                "sim_id": rec.sim_id,
                "script_path": script_path,
                "rpc_alias": rpc_alias,
                "broadcast": False,
                "estimated_gas": estimated_gas,
            },
        )


def _build_broadcast_tokens(
    *,
    script_path: str,
    target_contract: str,
    rpc_alias: str,
    sig: str | None,
    signer_mode: str,
) -> list[str]:
    if signer_mode not in _ALLOWED_SIGNER_MODES:
        msg = (
            f"Unsupported signer_mode '{signer_mode}': use host_broker, hardware, or safe_multisig."
        )
        raise ValueError(msg)
    tokens = [
        "forge",
        "script",
        _format_script_target(script_path, target_contract),
        "--rpc-url",
        rpc_alias,
        "--broadcast",
        "--non-interactive",
    ]
    if sig is not None:
        tokens.extend(["--sig", sig])
    if signer_mode == "hardware_ledger":
        tokens.append("--ledger")
    elif signer_mode == "hardware_trezor":
        tokens.append("--trezor")
    validate_sandbox_signer_isolation({}, tokens)
    return tokens


class ForgeBroadcastScriptTool(Tool):
    """Broadcast a previously simulated Foundry script behind ``ApprovalGate``."""

    def __init__(self, tracker: SimulationGateTracker | None = None) -> None:
        self._tracker = tracker or default_sim_tracker()

    @property
    def name(self) -> str:
        return "forge_broadcast_script"

    @property
    def description(self) -> str:
        return (
            "Broadcast a simulated Foundry deployment script ('sim_id') under interactive "
            "ApprovalGate confirmation with host/hardware/multisig signer isolation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "sim_id": {"type": "string"},
                "script_path": {"type": "string"},
                "rpc_alias": {"type": "string"},
                "working_dir": {"type": "string", "default": "/workspace"},
                "target_contract": {"type": "string"},
                "sig": {"type": "string"},
                "signer_mode": {
                    "type": "string",
                    "enum": ["host_broker", "hardware_ledger", "hardware_trezor", "safe_multisig"],
                    "default": "host_broker",
                },
            },
            "required": ["sim_id", "script_path", "rpc_alias"],
        }

    @property
    def requires_approval(self) -> bool:
        return True

    @property
    def approval_category(self) -> str:
        return "evm_broadcast"

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        """Describe script, RPC alias, signer mode, and simulation ID awaiting approval."""
        return (
            f"Broadcast EVM script '{arguments.get('script_path', '<missing>')}' "
            f"via rpc_alias='{arguments.get('rpc_alias', '?')}' "
            f"(signer_mode={arguments.get('signer_mode', 'host_broker')}, "
            f"sim_id={arguments.get('sim_id', '<missing>')})."
        )

    async def execute(self, arguments: dict[str, Any], sandbox: Sandbox) -> ToolResult:
        """Verify matching simulation record & signer isolation, then broadcast."""
        started = time.monotonic()
        try:
            work_dir = _validate_workspace_dir(arguments.get("working_dir"))
            script_path = validate_path(str(arguments.get("script_path") or ""))
            rpc_alias = _validate_token(arguments.get("rpc_alias"), "rpc_alias")
            target_contract = (
                _validate_token(arguments.get("target_contract"), "target_contract")
                if arguments.get("target_contract")
                else ""
            )
            sig = _validate_sig(arguments["sig"]) if arguments.get("sig") else None
            signer_mode = str(arguments.get("signer_mode", "host_broker")).strip()
            rec = self._tracker.require(
                str(arguments.get("sim_id") or ""),
                script_path=script_path,
                rpc_alias=rpc_alias,
                working_dir=work_dir,
                target_contract=target_contract,
            )
            tokens = _build_broadcast_tokens(
                script_path=script_path,
                target_contract=target_contract,
                rpc_alias=rpc_alias,
                sig=sig,
                signer_mode=signer_mode,
            )
        except ValueError as exc:
            return ToolResult(output="", error=str(exc), exit_code=1)

        cmd = f"cd {shlex.quote(work_dir)} && {' '.join(shlex.quote(t) for t in tokens)}"
        res = await sandbox.exec(cmd, timeout_seconds=300)
        combined = _format_exec(res.stdout, res.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.exit_code != 0:
            return ToolResult(
                output=combined,
                error=f"forge script broadcast failed with exit code {res.exit_code}",
                exit_code=res.exit_code,
                execution_time_ms=elapsed_ms,
            )

        self._tracker.consume(rec.sim_id)
        return ToolResult(
            output=combined,
            exit_code=0,
            execution_time_ms=elapsed_ms,
            metadata={
                "sim_id": rec.sim_id,
                "broadcast": True,
                "rpc_alias": rpc_alias,
                "signer_mode": signer_mode,
            },
        )


def create_evm_foundry_tools(tracker: SimulationGateTracker | None = None) -> list[Tool]:
    """Return instances of all four Foundry build/test/simulate/broadcast tools."""
    shared = tracker or SimulationGateTracker()
    return [
        ForgeBuildTool(shared),
        ForgeTestTool(shared),
        ForgeSimulateScriptTool(shared),
        ForgeBroadcastScriptTool(shared),
    ]
