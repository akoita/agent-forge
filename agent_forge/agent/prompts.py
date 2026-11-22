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
2. Make small, focused changes. Prefer apply_patch for multi-file or \
precondition-checked edits; do not rewrite entire files unnecessarily.
3. After making changes, verify they work (e.g., run tests or linters).
4. If you encounter an error, analyze it and try a different approach.
5. When the task is complete, provide a summary of what you changed and why.
6. {network_rule}
7. Stay within the /workspace directory.

## Sandbox
- Backend: {sandbox_backend}
- Runtime image: {sandbox_image}
- Network: {network_status}
- Max shell command timeout: {command_timeout_seconds}s

## Task
{task_description}"""


def _format_tool_descriptions(tool_definitions: list[ToolDefinition]) -> str:
    """Format tool definitions into a readable list for system prompts."""
    tool_lines: list[str] = []
    for td in tool_definitions:
        props: dict[str, dict[str, object]] = td.parameters.get("properties", {})  # type: ignore[assignment]
        params = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
        tool_lines.append(f"- **{td.name}**({params}) — {td.description}")
    return "\n".join(tool_lines)


def _format_network_rule(network_enabled: bool) -> tuple[str, str]:
    """Return (network_rule, network_status) based on network flag."""
    if network_enabled:
        return (
            "Network access is enabled in this sandbox. Use it only when required for "
            "the task, such as installing dependencies or fetching remote resources.",
            "enabled",
        )
    return (
        "Do NOT attempt to access the internet or external services.",
        "disabled",
    )


def render_system_prompt(
    template: str,
    task: str,
    tool_definitions: list[ToolDefinition],
    *,
    sandbox_backend: str = "docker",
    sandbox_image: str = "agent-forge-sandbox:latest",
    network_enabled: bool = False,
    command_timeout_seconds: int = 300,
    prompt_scope: str | None = None,
    extension_prompts: list[str] | None = None,
    **template_vars: object,
) -> str:
    """Render a system prompt template with runtime context and extensions."""
    network_rule, network_status = _format_network_rule(network_enabled)

    prompt = template.format(
        tool_descriptions=_format_tool_descriptions(tool_definitions),
        sandbox_backend=sandbox_backend,
        sandbox_image=sandbox_image,
        network_rule=network_rule,
        network_status=network_status,
        command_timeout_seconds=command_timeout_seconds,
        task_description=task,
        **template_vars,
    )

    if prompt_scope:
        prompt += f"\n\n## Profile Scope\n{prompt_scope}"

    if extension_prompts:
        prompt += "\n\n## Extension Prompts"
        for fragment in extension_prompts:
            prompt += f"\n{fragment}"

    return prompt


def build_system_prompt(
    task: str,
    tool_definitions: list[ToolDefinition],
    *,
    sandbox_backend: str = "docker",
    sandbox_image: str = "agent-forge-sandbox:latest",
    network_enabled: bool = False,
    command_timeout_seconds: int = 300,
    prompt_scope: str | None = None,
    extension_prompts: list[str] | None = None,
) -> str:
    """Render the system prompt with tool descriptions and task."""
    return render_system_prompt(
        _SYSTEM_PROMPT_TEMPLATE,
        task,
        tool_definitions,
        sandbox_backend=sandbox_backend,
        sandbox_image=sandbox_image,
        network_enabled=network_enabled,
        command_timeout_seconds=command_timeout_seconds,
        prompt_scope=prompt_scope,
        extension_prompts=extension_prompts,
    )


_HOSTED_SYSTEM_PROMPT_TEMPLATE = """\
You are Agent Forge running a hosted coding agent task.

## CRITICAL OUTPUT REQUIREMENT
Before you stop, you MUST write a valid JSON report to .agent-forge/report.json \
using the write_file tool. The hosted run will FAIL if this file is not written. \
This is not optional — if you skip this step the run output cannot be finalized.

## Report Schema ({report_schema})
The JSON report MUST contain these top-level fields:
- "schema_version": must be exactly "{report_schema}"
- "run_id": the run identifier (use the task description for reference)
- "summary": a brief natural-language overall assessment

Optional report fields: confidence, benchmark_id, target, provenance

Additional schema-specific fields may also be required by the active profile or \
extension instructions below.

## Your Capabilities
{tool_descriptions}

## Rules
1. Always read relevant files before making changes.
2. Make small, focused changes. Do not rewrite entire files unnecessarily.
3. Follow all scope and modification constraints specified in the task or profile.
4. {network_rule}
5. Stay within the /workspace directory.
6. Your FINAL action before stopping MUST be writing .agent-forge/report.json.

## Sandbox
- Backend: {sandbox_backend}
- Runtime image: {sandbox_image}
- Network: {network_status}
- Max shell command timeout: {command_timeout_seconds}s

## Task
{task_description}"""


def build_hosted_system_prompt(
    task: str,
    tool_definitions: list[ToolDefinition],
    *,
    report_schema: str = "agent-forge-report-v1",
    sandbox_backend: str = "docker",
    sandbox_image: str = "agent-forge-sandbox:latest",
    network_enabled: bool = False,
    command_timeout_seconds: int = 300,
    prompt_scope: str | None = None,
    extension_prompts: list[str] | None = None,
) -> str:
    """Render the hosted system prompt with structured report emission."""
    return render_system_prompt(
        _HOSTED_SYSTEM_PROMPT_TEMPLATE,
        task,
        tool_definitions,
        sandbox_backend=sandbox_backend,
        sandbox_image=sandbox_image,
        network_enabled=network_enabled,
        command_timeout_seconds=command_timeout_seconds,
        prompt_scope=prompt_scope,
        extension_prompts=extension_prompts,
        report_schema=report_schema,
    )
