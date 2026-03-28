"""Unit tests for external tool plugin discovery and registration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent_forge.tools import ToolPluginError, create_default_registry, discover_tool_plugins
from agent_forge.tools.base import Tool, ToolRegistry, ToolResult
from agent_forge.tools.plugins import load_plugin_tools, register_plugin_tools


class ExamplePluginTool(Tool):
    @property
    def name(self) -> str:
        return "example_plugin"

    @property
    def description(self) -> str:
        return "Example plugin tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
        }

    async def execute(self, arguments: dict[str, Any], sandbox: Any) -> ToolResult:
        return ToolResult(output=str(arguments.get("value", "")))


@dataclass
class FakeEntryPoint:
    name: str
    value: str
    loaded: object

    def load(self) -> object:
        if isinstance(self.loaded, Exception):
            raise self.loaded
        return self.loaded


class TestToolPluginDiscovery:
    def test_discover_sorts_entry_points(self) -> None:
        discovered = discover_tool_plugins(
            entry_points_factory=lambda: [
                FakeEntryPoint("zeta", "pkg:ZetaTool", ExamplePluginTool),
                FakeEntryPoint("alpha", "pkg:AlphaTool", ExamplePluginTool),
            ]
        )

        assert [entry_point.name for entry_point in discovered] == ["alpha", "zeta"]


class TestToolPluginLoading:
    def test_loads_tool_subclass(self) -> None:
        tools = load_plugin_tools(
            entry_points_factory=lambda: [
                FakeEntryPoint("example", "pkg:ExamplePluginTool", ExamplePluginTool)
            ]
        )

        assert len(tools) == 1
        assert isinstance(tools[0], ExamplePluginTool)

    def test_loads_tool_instance(self) -> None:
        tools = load_plugin_tools(
            entry_points_factory=lambda: [
                FakeEntryPoint("example", "pkg:tool", ExamplePluginTool())
            ]
        )

        assert len(tools) == 1
        assert tools[0].name == "example_plugin"

    def test_rejects_invalid_plugin_type(self) -> None:
        with pytest.raises(ToolPluginError, match="Tool instance or Tool subclass"):
            load_plugin_tools(
                entry_points_factory=lambda: [FakeEntryPoint("bad", "pkg:not_tool", object())]
            )

    def test_surfaces_plugin_load_error(self) -> None:
        with pytest.raises(ToolPluginError, match="Failed to load tool plugin 'bad'"):
            load_plugin_tools(
                entry_points_factory=lambda: [
                    FakeEntryPoint("bad", "pkg:broken", RuntimeError("boom"))
                ]
            )

    def test_surfaces_instantiation_error(self) -> None:
        class BrokenTool(ExamplePluginTool):
            def __init__(self) -> None:
                msg = "bad init"
                raise RuntimeError(msg)

        with pytest.raises(ToolPluginError, match="Failed to instantiate tool plugin 'broken'"):
            load_plugin_tools(
                entry_points_factory=lambda: [
                    FakeEntryPoint("broken", "pkg:BrokenTool", BrokenTool)
                ]
            )


class TestToolPluginRegistration:
    def test_register_plugin_tools(self) -> None:
        registry = ToolRegistry()
        register_plugin_tools(
            registry,
            entry_points_factory=lambda: [
                FakeEntryPoint("example", "pkg:ExamplePluginTool", ExamplePluginTool)
            ],
        )

        assert registry.get("example_plugin").name == "example_plugin"

    def test_default_registry_can_include_plugins(self) -> None:
        registry = create_default_registry(
            entry_points_factory=lambda: [
                FakeEntryPoint("example", "pkg:ExamplePluginTool", ExamplePluginTool)
            ]
        )

        assert registry.get("example_plugin").name == "example_plugin"
        assert len(registry) == 11

    def test_duplicate_plugin_name_is_rejected(self) -> None:
        class DuplicateReadFileTool(ExamplePluginTool):
            @property
            def name(self) -> str:
                return "read_file"

        registry = create_default_registry(load_plugins=False)
        with pytest.raises(ValueError, match="already registered"):
            register_plugin_tools(
                registry,
                entry_points_factory=lambda: [
                    FakeEntryPoint("read_file", "pkg:DuplicateReadFileTool", DuplicateReadFileTool)
                ]
            )
