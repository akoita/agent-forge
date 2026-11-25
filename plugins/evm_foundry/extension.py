"""Extension registration helpers for the EVM/Foundry workflow pack."""

from __future__ import annotations

from pathlib import Path

from agent_forge.extensions.discovery import ExtensionInfo


def get_profiles_dir() -> Path:
    """Return the directory containing EVM/Foundry YAML profiles."""
    return Path(__file__).resolve().parent / "profiles"


def get_prompt_fragment() -> str:
    """Return the shared system prompt safety rules for EVM/Foundry workflows."""
    return (
        "## EVM/Foundry Workflow Safety Rules\n"
        "- Target stock 'ghcr.io/foundry-rs/foundry:latest' offline for build and test.\n"
        "- Always execute 'forge_simulate_script' (dry-run without --broadcast) before any "
        "on-chain broadcast.\n"
        "- Require explicit ApprovalGate confirmation before 'forge_broadcast_script'; never pass "
        "private keys, mnemonics, or unlocked signers into the sandbox."
    )


def get_extension_info() -> ExtensionInfo:
    """Return :class:`ExtensionInfo` metadata for ``plugins.evm_foundry``."""
    return ExtensionInfo(
        name="evm-foundry",
        version="0.1.0",
        description=(
            "Foundry build, test, fork simulation, and approval-gated broadcast extension pack."
        ),
        package="plugins.evm_foundry",
        profiles=["evm-foundry-dev", "evm-foundry-deploy"],
        tools=[
            "forge_build",
            "forge_test",
            "forge_simulate_script",
            "forge_broadcast_script",
        ],
        prompts=["evm-foundry-safety"],
    )
