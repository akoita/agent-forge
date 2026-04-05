"""Extension discovery — scan entry_points for installed extensions.

Discovers extensions registered via Python entry-point groups:

- ``agent_forge.extensions`` — extension metadata (``ExtensionInfo``)
- ``agent_forge.profiles`` — profile directories (``Path``)
- ``agent_forge.prompts`` — system prompt fragments (``str`` or ``Path``)
- ``agent_forge.workflows`` — workflow directories (``Path``)
- ``agent_forge.tools`` — tool plugins (handled by ``tools.plugins``)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EXTENSION_PLUGIN_GROUP = "agent_forge.extensions"
PROFILE_PLUGIN_GROUP = "agent_forge.profiles"
PROMPT_PLUGIN_GROUP = "agent_forge.prompts"
WORKFLOW_PLUGIN_GROUP = "agent_forge.workflows"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ExtensionInfo:
    """Metadata for an installed Agent Forge extension."""

    name: str
    version: str = ""
    description: str = ""
    package: str = ""
    profiles: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Generic entry-point helpers
# ---------------------------------------------------------------------------

EntryPointIterable = Iterable[EntryPoint]
EntryPointsFactory = Callable[[str], EntryPointIterable]


def _default_entry_points(group: str) -> EntryPointIterable:
    """Return entry points for a given group."""
    return entry_points(group=group)


# ---------------------------------------------------------------------------
# Extension discovery
# ---------------------------------------------------------------------------


def _load_extension_info(ep: EntryPoint) -> ExtensionInfo | None:
    """Load a single ``agent_forge.extensions`` entry point.

    Returns ``None`` (with a warning) if the entry point cannot be loaded.
    """
    try:
        loaded = ep.load()
    except (ImportError, AttributeError, ModuleNotFoundError):
        logger.warning("Failed to load extension entry point '%s'", ep.name, exc_info=True)
        return None

    if isinstance(loaded, ExtensionInfo):
        return loaded

    # Allow a callable that returns ExtensionInfo
    if callable(loaded):
        try:
            result = loaded()
            if isinstance(result, ExtensionInfo):
                return result
        except (TypeError, ValueError, RuntimeError):
            logger.warning("Extension factory '%s' raised an error", ep.name, exc_info=True)
            return None

    logger.warning(
        "Extension entry point '%s' did not resolve to ExtensionInfo (got %s)",
        ep.name,
        type(loaded).__name__,
    )
    return None


def discover_extensions(
    *,
    entry_points_factory: EntryPointsFactory | None = None,
) -> list[ExtensionInfo]:
    """Discover all installed Agent Forge extensions.

    Scans the ``agent_forge.extensions`` entry-point group. Each entry
    point should resolve to an :class:`ExtensionInfo` instance or a
    zero-argument callable returning one.

    Args:
        entry_points_factory: Override for testing. Receives a group name
            string and returns an iterable of ``EntryPoint``.

    Returns:
        Sorted list of discovered extensions.
    """
    factory = entry_points_factory or _default_entry_points
    eps = sorted(factory(EXTENSION_PLUGIN_GROUP), key=lambda ep: ep.name)

    extensions: list[ExtensionInfo] = []
    for ep in eps:
        info = _load_extension_info(ep)
        if info is not None:
            extensions.append(info)

    return extensions


# ---------------------------------------------------------------------------
# Profile directory discovery
# ---------------------------------------------------------------------------


def discover_extension_profile_dirs(
    *,
    entry_points_factory: EntryPointsFactory | None = None,
) -> list[Path]:
    """Discover profile directories from installed extensions.

    Scans the ``agent_forge.profiles`` entry-point group. Each entry
    point should resolve to a :class:`Path` pointing to a directory
    containing YAML profile files.

    Args:
        entry_points_factory: Override for testing.

    Returns:
        List of valid profile directories from installed extensions.
    """
    factory = entry_points_factory or _default_entry_points
    eps = sorted(factory(PROFILE_PLUGIN_GROUP), key=lambda ep: ep.name)

    dirs: list[Path] = []
    for ep in eps:
        try:
            loaded: Any = ep.load()
        except (ImportError, AttributeError, ModuleNotFoundError):
            logger.warning("Failed to load profile entry point '%s'", ep.name, exc_info=True)
            continue

        if isinstance(loaded, Path):
            if loaded.is_dir():
                dirs.append(loaded)
            else:
                logger.warning(
                    "Profile entry point '%s' resolved to a non-directory path: %s",
                    ep.name,
                    loaded,
                )
        else:
            logger.warning(
                "Profile entry point '%s' did not resolve to a Path (got %s)",
                ep.name,
                type(loaded).__name__,
            )

    return dirs


# ---------------------------------------------------------------------------
# Prompt fragment discovery
# ---------------------------------------------------------------------------


def discover_extension_prompt_fragments(
    *,
    entry_points_factory: EntryPointsFactory | None = None,
) -> list[str]:
    """Discover system prompt fragments from installed extensions.

    Scans the ``agent_forge.prompts`` entry-point group. Each entry
    point should resolve to one of:

    - A ``str`` — raw markdown prompt fragment.
    - A ``Path`` — pointing to a ``.md`` file containing the fragment.
    - A callable returning a ``str``.

    Args:
        entry_points_factory: Override for testing.

    Returns:
        List of prompt fragment strings from installed extensions.
    """
    factory = entry_points_factory or _default_entry_points
    eps = sorted(factory(PROMPT_PLUGIN_GROUP), key=lambda ep: ep.name)

    fragments: list[str] = []
    for ep in eps:
        try:
            loaded: Any = ep.load()
        except (ImportError, AttributeError, ModuleNotFoundError):
            logger.warning(
                "Failed to load prompt entry point '%s'",
                ep.name,
                exc_info=True,
            )
            continue

        fragment = _resolve_prompt_fragment(ep.name, loaded)
        if fragment is not None:
            fragments.append(fragment)

    return fragments


def _resolve_prompt_fragment(name: str, loaded: Any) -> str | None:
    """Resolve a loaded entry_point into a prompt fragment string."""
    # Direct string
    if isinstance(loaded, str):
        return loaded

    # Path to a .md file
    if isinstance(loaded, Path):
        if loaded.is_file():
            return loaded.read_text(encoding="utf-8").strip()
        logger.warning(
            "Prompt entry point '%s' resolved to a non-file path: %s",
            name,
            loaded,
        )
        return None

    # Callable returning a string
    if callable(loaded):
        try:
            result = loaded()
            if isinstance(result, str):
                return result
        except (TypeError, ValueError, RuntimeError):
            logger.warning(
                "Prompt factory '%s' raised an error",
                name,
                exc_info=True,
            )
            return None

    logger.warning(
        "Prompt entry point '%s' did not resolve to str or Path (got %s)",
        name,
        type(loaded).__name__,
    )
    return None


# ---------------------------------------------------------------------------
# Workflow directory discovery
# ---------------------------------------------------------------------------


def discover_extension_workflow_dirs(
    *,
    entry_points_factory: EntryPointsFactory | None = None,
) -> list[Path]:
    """Discover workflow directories from installed extensions.

    Scans the ``agent_forge.workflows`` entry-point group. Each entry
    point should resolve to a :class:`Path` pointing to a directory
    containing ``.md`` workflow files.

    Args:
        entry_points_factory: Override for testing.

    Returns:
        List of valid workflow directories from installed extensions.
    """
    factory = entry_points_factory or _default_entry_points
    eps = sorted(factory(WORKFLOW_PLUGIN_GROUP), key=lambda ep: ep.name)

    dirs: list[Path] = []
    for ep in eps:
        try:
            loaded: Any = ep.load()
        except (ImportError, AttributeError, ModuleNotFoundError):
            logger.warning(
                "Failed to load workflow entry point '%s'",
                ep.name,
                exc_info=True,
            )
            continue

        if isinstance(loaded, Path):
            if loaded.is_dir():
                dirs.append(loaded)
            else:
                logger.warning(
                    "Workflow entry point '%s' resolved to a non-directory path: %s",
                    ep.name,
                    loaded,
                )
        else:
            logger.warning(
                "Workflow entry point '%s' did not resolve to a Path (got %s)",
                ep.name,
                type(loaded).__name__,
            )

    return dirs
