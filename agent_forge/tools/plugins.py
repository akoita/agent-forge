"""Plugin discovery and loading for external tool packages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from importlib.metadata import EntryPoint, entry_points
from inspect import isclass
from typing import cast

from agent_forge.tools.base import Tool, ToolRegistry

TOOL_PLUGIN_GROUP = "agent_forge.tools"


class ToolPluginError(ValueError):
    """Raised when a declared tool plugin cannot be loaded safely."""


EntryPointIterable = Iterable[EntryPoint]
EntryPointsFactory = Callable[[], EntryPointIterable]


def _default_entry_points() -> EntryPointIterable:
    """Return configured tool plugin entry points."""
    return entry_points(group=TOOL_PLUGIN_GROUP)


def discover_tool_plugins(
    *,
    entry_points_factory: EntryPointsFactory | None = None,
) -> list[EntryPoint]:
    """Discover configured tool plugins from Python entry points."""
    factory = entry_points_factory or _default_entry_points
    return sorted(factory(), key=lambda entry_point: entry_point.name)


def _instantiate_plugin(entry_point: EntryPoint) -> Tool:
    """Load one entry point and coerce it into a Tool instance."""
    try:
        loaded = entry_point.load()
    except Exception as exc:
        msg = f"Failed to load tool plugin '{entry_point.name}': {exc}"
        raise ToolPluginError(msg) from exc

    if isinstance(loaded, Tool):
        return loaded

    if isclass(loaded) and issubclass(cast("type[object]", loaded), Tool):
        tool_class = cast("type[Tool]", loaded)
        try:
            return tool_class()
        except Exception as exc:
            msg = (
                f"Failed to instantiate tool plugin '{entry_point.name}' "
                f"from '{entry_point.value}': {exc}"
            )
            raise ToolPluginError(msg) from exc

    msg = (
        f"Invalid tool plugin '{entry_point.name}' from '{entry_point.value}': "
        "entry point must resolve to a Tool instance or Tool subclass"
    )
    raise ToolPluginError(msg)


def load_plugin_tools(
    *,
    entry_points_factory: EntryPointsFactory | None = None,
) -> list[Tool]:
    """Load all configured external tool plugins."""
    return [
        _instantiate_plugin(entry_point)
        for entry_point in discover_tool_plugins(entry_points_factory=entry_points_factory)
    ]


def register_plugin_tools(
    registry: ToolRegistry,
    *,
    entry_points_factory: EntryPointsFactory | None = None,
) -> None:
    """Register all configured external tool plugins on a registry."""
    for tool in load_plugin_tools(entry_points_factory=entry_points_factory):
        registry.register(tool)
