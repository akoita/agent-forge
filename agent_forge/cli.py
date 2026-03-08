"""Click CLI entrypoint for Agent Forge."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import click
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table

from agent_forge.config import USER_CONFIG_DIR, load_config

console = Console()
err_console = Console(stderr=True)


@click.group()
@click.version_option(package_name="agent-forge")
def main() -> None:
    """Agent Forge — Sandboxed AI coding agent runtime."""


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


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

    cfg = load_config(cli_overrides=cli_overrides or None)

    # Resolve API key
    provider_name = cfg.agent.default_provider
    provider_cfg = cfg.providers.get(provider_name)
    if provider_cfg is None:
        err_console.print(
            f"[red]Unknown provider:[/red] '{provider_name}'. "
            f"Available: {', '.join(cfg.providers)}"
        )
        sys.exit(1)

    api_key = os.environ.get(provider_cfg.api_key_env, "")
    if not api_key:
        err_console.print(
            f"[red]Missing API key:[/red] set [bold]{provider_cfg.api_key_env}[/bold] "
            f"environment variable for provider '{provider_name}'"
        )
        sys.exit(1)

    asyncio.run(_run_agent(task, repo, cfg, provider_name, api_key))


async def _run_agent(
    task: str,
    repo: str,
    cfg: Any,
    provider_name: str,
    api_key: str,
) -> None:
    """Execute the full agent pipeline."""
    from agent_forge.agent.core import react_loop
    from agent_forge.agent.models import AgentConfig, AgentRun
    from agent_forge.llm.gemini import GeminiProvider
    from agent_forge.sandbox.docker import DockerSandbox
    from agent_forge.tools import create_default_registry

    # Build agent config from resolved settings
    agent_config = AgentConfig(
        model=cfg.agent.default_model,
        max_iterations=cfg.agent.max_iterations,
        max_tokens_per_run=cfg.agent.max_tokens_per_run,
        temperature=cfg.agent.temperature,
    )

    agent_run = AgentRun(task=task, repo_path=repo, config=agent_config)

    # Create LLM provider
    if provider_name == "gemini":
        llm = GeminiProvider(api_key=api_key)
    else:
        err_console.print(
            f"[red]Provider '{provider_name}' not yet implemented.[/red] "
            "Only 'gemini' is available."
        )
        sys.exit(1)

    # Create tools and sandbox
    tools = create_default_registry()
    sandbox = DockerSandbox()

    with console.status("[bold green]Agent running...", spinner="dots"):
        try:
            await sandbox.start(repo_path=repo)
            result = await react_loop(agent_run, llm, tools, sandbox)
        except Exception as exc:  # noqa: BLE001 — top-level catch-all for CLI
            err_console.print(f"[red]Agent failed:[/red] {exc}")
            sys.exit(1)
        finally:
            await sandbox.stop()
            await llm.close()

    # Display results
    _display_run_summary(result)


def _display_run_summary(run: Any) -> None:
    """Display a rich summary panel for a completed agent run."""
    state_color = {
        "completed": "green",
        "failed": "red",
        "timeout": "yellow",
        "cancelled": "dim",
    }.get(run.state.value, "white")

    table = Table(show_header=False, padding=(0, 2))
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", run.id)
    table.add_row("State", f"[{state_color}]{run.state.value}[/{state_color}]")
    table.add_row("Task", run.task[:80] + ("..." if len(run.task) > 80 else ""))
    table.add_row("Iterations", str(run.iterations))
    table.add_row("Tokens", f"{run.total_tokens.total_tokens:,}")
    if run.completed_at:
        elapsed = (run.completed_at - run.created_at).total_seconds()
        table.add_row("Duration", f"{elapsed:.1f}s")
    if run.error:
        table.add_row("Error", f"[red]{run.error}[/red]")

    console.print(Panel(table, title="[bold]Agent Run Complete", border_style=state_color))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@main.command()
@click.argument("run_id")
def status(run_id: str) -> None:
    """Show the status and summary of a specific run."""
    from agent_forge.agent.persistence import load_run

    try:
        agent_run = load_run(run_id)
    except FileNotFoundError:
        err_console.print(f"[red]Run not found:[/red] {run_id}")
        sys.exit(1)

    _display_run_summary(agent_run)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@main.command("list")
def list_runs() -> None:
    """List recent agent runs."""
    runs_dir = USER_CONFIG_DIR / "runs"

    if not runs_dir.exists():
        console.print("[dim]No runs found yet.[/dim]")
        return

    run_dirs = sorted(runs_dir.iterdir(), reverse=True)
    if not run_dirs:
        console.print("[dim]No runs found yet.[/dim]")
        return

    table = Table(title="Recent Runs")
    table.add_column("Run ID", style="cyan", max_width=36)
    table.add_column("State", justify="center")
    table.add_column("Task", max_width=50)
    table.add_column("Iterations", justify="right")
    table.add_column("Created")

    for run_dir in run_dirs[:20]:  # Show last 20
        run_json = run_dir / "run.json"
        if not run_json.exists():
            continue
        try:
            meta = json.loads(run_json.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        state = meta.get("state", "unknown")
        state_color = {
            "completed": "green",
            "failed": "red",
            "timeout": "yellow",
            "cancelled": "dim",
            "running": "blue",
            "pending": "white",
        }.get(state, "white")

        task_text = meta.get("task", "")
        if len(task_text) > 50:
            task_text = task_text[:47] + "..."

        table.add_row(
            meta.get("id", run_dir.name),
            f"[{state_color}]{state}[/{state_color}]",
            task_text,
            str(meta.get("iterations", 0)),
            meta.get("created_at", "")[:19],
        )

    console.print(table)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@main.command()
def config() -> None:
    """Show the resolved configuration."""
    cfg = load_config()
    console.print(
        Panel(
            JSON(cfg.model_dump_json(indent=2)),
            title="[bold]Resolved Configuration",
            border_style="blue",
        )
    )


if __name__ == "__main__":
    main()
