"""Click CLI entrypoint for Agent Forge."""

import click


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
    click.echo(f"Task: {task}")
    click.echo(f"Repo: {repo}")
    click.echo("Agent Forge is not yet implemented. See spec.md for details.")


if __name__ == "__main__":
    main()
