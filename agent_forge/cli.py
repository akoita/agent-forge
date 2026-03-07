"""Click CLI entrypoint for Agent Forge."""

from __future__ import annotations

from typing import Any

import click

from agent_forge.config import load_config


@click.group()
@click.version_option(package_name="agent-forge")
def main() -> None:
    """Agent Forge — Sandboxed AI coding agent runtime."""


@main.command()
@click.option("--task", required=True, help="Coding task description")
@click.option("--repo", required=True, help="Path to target repository")
@click.option("--model", default=None, help="LLM model to use")
@click.option("--provider", default=None, help="LLM provider (gemini, openai, anthropic)")
@click.option("--max-iterations", default=None, type=int, help="Max ReAct loop iterations")
def run(
    task: str,
    repo: str,
    model: str | None,
    provider: str | None,
    max_iterations: int | None,
) -> None:
    """Run an agent task on a repository."""
    # Build CLI overrides from provided flags
    cli_overrides: dict[str, Any] = {}
    if model is not None:
        cli_overrides["agent.default_model"] = model
    if provider is not None:
        cli_overrides["agent.default_provider"] = provider
    if max_iterations is not None:
        cli_overrides["agent.max_iterations"] = max_iterations

    config = load_config(cli_overrides=cli_overrides or None)

    click.echo(f"Task:     {task}")
    click.echo(f"Repo:     {repo}")
    click.echo(f"Provider: {config.agent.default_provider}")
    click.echo(f"Model:    {config.agent.default_model}")
    click.echo("Agent Forge is not yet implemented. See spec.md for details.")


@main.command()
def config() -> None:
    """Show the resolved configuration."""
    cfg = load_config()
    click.echo(cfg.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
