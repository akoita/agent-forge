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
6. Do NOT attempt to access the internet or external services.
7. Stay within the /workspace directory.

## Task
{task_description}"""


def build_system_prompt(
    task: str,
    tool_definitions: list[ToolDefinition],
) -> str:
    """Render the system prompt with tool descriptions and task."""
    tool_lines: list[str] = []
    for td in tool_definitions:
        props: dict[str, dict[str, object]] = td.parameters.get("properties", {})  # type: ignore[assignment]
        params = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
        tool_lines.append(f"- **{td.name}**({params}) — {td.description}")

    return _SYSTEM_PROMPT_TEMPLATE.format(
        tool_descriptions="\n".join(tool_lines),
        task_description=task,
    )
