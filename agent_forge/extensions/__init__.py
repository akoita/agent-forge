"""Extension SDK — discover, scaffold, and manage Agent Forge extensions."""

from agent_forge.extensions.discovery import (
    EXTENSION_PLUGIN_GROUP,
    PROFILE_PLUGIN_GROUP,
    ExtensionInfo,
    discover_extensions,
)

__all__ = [
    "EXTENSION_PLUGIN_GROUP",
    "PROFILE_PLUGIN_GROUP",
    "ExtensionInfo",
    "discover_extensions",
]
