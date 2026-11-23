"""Extension SDK — discover, scaffold, and manage Agent Forge extensions."""

from agent_forge.extensions.discovery import (
    CONTRACT_PLUGIN_GROUP,
    EXTENSION_PLUGIN_GROUP,
    PROFILE_PLUGIN_GROUP,
    PROMPT_PLUGIN_GROUP,
    WORKFLOW_PLUGIN_GROUP,
    ExtensionInfo,
    discover_extension_contracts,
    discover_extension_prompt_fragments,
    discover_extension_workflow_dirs,
    discover_extensions,
)

__all__ = [
    "CONTRACT_PLUGIN_GROUP",
    "EXTENSION_PLUGIN_GROUP",
    "PROFILE_PLUGIN_GROUP",
    "PROMPT_PLUGIN_GROUP",
    "WORKFLOW_PLUGIN_GROUP",
    "ExtensionInfo",
    "discover_extension_contracts",
    "discover_extension_prompt_fragments",
    "discover_extension_workflow_dirs",
    "discover_extensions",
]
