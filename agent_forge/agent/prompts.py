"""System prompt templates for the agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_forge.llm.base import ToolDefinition

_SYSTEM_PROMPT_TEMPLATE = """\
You are Agent Forge, an autonomous coding agent. You are given a coding task \
and a workspace containing a code repository.

## Your Capabilities
You can use the following tools to complete the task:
{tool_descriptions}

## Rules
1. Always read relevant files before making changes.
2. Make small, focused changes. Do not rewrite entire files unnecessarily.
3. After making changes, verify they work (e.g., run tests or linters).
4. If you encounter an error, analyze it and try a different approach.
5. When the task is complete, provide a summary of what you changed and why.
6. {network_rule}
7. Stay within the /workspace directory.

## Sandbox
- Runtime image: {sandbox_image}
- Network: {network_status}
- Max shell command timeout: {command_timeout_seconds}s

## Task
{task_description}"""


def build_system_prompt(
    task: str,
    tool_definitions: list[ToolDefinition],
    *,
    sandbox_image: str = "agent-forge-sandbox:latest",
    network_enabled: bool = False,
    command_timeout_seconds: int = 300,
) -> str:
    """Render the system prompt with tool descriptions and task."""
    tool_lines: list[str] = []
    for td in tool_definitions:
        props: dict[str, dict[str, object]] = td.parameters.get("properties", {})  # type: ignore[assignment]
        params = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
        tool_lines.append(f"- **{td.name}**({params}) — {td.description}")

    if network_enabled:
        network_rule = (
            "Network access is enabled in this sandbox. Use it only when required for "
            "the task, such as installing dependencies or fetching remote resources."
        )
        network_status = "enabled"
    else:
        network_rule = "Do NOT attempt to access the internet or external services."
        network_status = "disabled"

    return _SYSTEM_PROMPT_TEMPLATE.format(
        tool_descriptions="\n".join(tool_lines),
        sandbox_image=sandbox_image,
        network_rule=network_rule,
        network_status=network_status,
        command_timeout_seconds=command_timeout_seconds,
        task_description=task,
    )
