"""Click CLI entrypoint for Agent Forge."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table

from agent_forge.config import USER_CONFIG_DIR, load_config
from agent_forge.sandbox.base import SandboxConfig

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
@click.option("--sandbox-image", default=None, help="Sandbox image to run")
@click.option(
    "--network/--no-network",
    default=None,
    help="Enable or disable network access inside the sandbox",
)
@click.option(
    "--command-timeout",
    default=None,
    type=int,
    help="Per-command sandbox timeout cap in seconds",
)
@click.option(
    "--queue",
    "queue_backend",
    default=None,
    type=click.Choice(["memory", "redis"], case_sensitive=False),
    help="Enable queue mode (memory or redis)",
)
@click.option(
    "--redis-url",
    default="redis://localhost:6379/0",
    help="Redis URL when --queue=redis",
)
@click.option(
    "--max-concurrent-runs",
    default=0,
    type=int,
    help="Max concurrent tasks for queue worker (0=unlimited)",
)
@click.option(
    "--output-format",
    default="text",
    type=click.Choice(["text", "json"], case_sensitive=False),
    help="Render a human summary or a machine-readable JSON result.",
)
@click.option(
    "--report-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the machine-readable run result to a JSON file.",
)
def run(
    task: str,
    repo: str,
    model: str | None,
    provider: str | None,
    max_iterations: int | None,
    sandbox_image: str | None,
    network: bool | None,
    command_timeout: int | None,
    queue_backend: str | None,
    redis_url: str,
    max_concurrent_runs: int,
    output_format: str,
    report_file: Path | None,
) -> None:
    """Run an agent task on a repository."""
    cfg = load_config(
        cli_overrides=_build_cli_overrides(
            model=model,
            provider=provider,
            max_iterations=max_iterations,
            sandbox_image=sandbox_image,
            network=network,
            command_timeout=command_timeout,
        )
    )

    # Resolve API key
    provider_name = cfg.agent.default_provider
    provider_cfg = cfg.providers.get(provider_name)
    if provider_cfg is None:
        err_console.print(
            f"[red]Unknown provider:[/red] '{provider_name}'. Available: {', '.join(cfg.providers)}"
        )
        sys.exit(1)

    api_key = os.environ.get(provider_cfg.api_key_env, "")
    if not api_key:
        err_console.print(
            f"[red]Missing API key:[/red] set [bold]{provider_cfg.api_key_env}[/bold] "
            f"environment variable for provider '{provider_name}'"
        )
        sys.exit(1)

    if queue_backend is not None:
        if output_format == "json" or report_file is not None:
            err_console.print(
                "[red]Machine-readable output is not supported in queue mode yet.[/red]"
            )
            sys.exit(1)
        asyncio.run(
            _run_agent_queued(
                task,
                repo,
                cfg,
                provider_name,
                api_key,
                queue_backend=queue_backend,
                redis_url=redis_url,
                max_concurrent_runs=max_concurrent_runs,
            )
        )
    else:
        result = asyncio.run(_run_agent(task, repo, cfg, provider_name, api_key))
        _emit_run_output(result, output_format=output_format, report_file=report_file)


async def _run_agent(
    task: str,
    repo: str,
    cfg: Any,
    provider_name: str,
    api_key: str,
) -> Any:
    """Execute the full agent pipeline (direct mode).

    Creates an ``EventBus`` so lifecycle events are emitted even in
    direct mode — this enables observability subscribers.
    """
    from agent_forge.agent.core import react_loop
    from agent_forge.agent.models import AgentConfig, AgentRun
    from agent_forge.agent.prompts import build_system_prompt
    from agent_forge.orchestration.events import EventBus
    from agent_forge.sandbox.docker import DockerSandbox
    from agent_forge.tools import create_default_registry

    sandbox_config = _build_sandbox_config(cfg)
    agent_config = AgentConfig(
        model=cfg.agent.default_model,
        max_iterations=cfg.agent.max_iterations,
        max_tokens_per_run=cfg.agent.max_tokens_per_run,
        temperature=cfg.agent.temperature,
    )

    event_bus = EventBus()
    llm = _create_llm(provider_name, api_key)
    tools = create_default_registry()
    agent_config.system_prompt = build_system_prompt(
        task,
        tools.list_definitions(),
        sandbox_image=sandbox_config.image,
        network_enabled=sandbox_config.network_enabled,
        command_timeout_seconds=sandbox_config.timeout_seconds,
    )
    agent_run = AgentRun(task=task, repo_path=repo, config=agent_config)
    sandbox = DockerSandbox()

    with console.status("[bold green]Agent running...", spinner="dots"):
        try:
            await sandbox.start(repo_path=repo, config=sandbox_config)
            result = await react_loop(
                agent_run,
                llm,
                tools,
                sandbox,
                event_bus=event_bus,
            )
        except Exception as exc:  # noqa: BLE001 — top-level catch-all for CLI
            err_console.print(f"[red]Agent failed:[/red] {exc}")
            sys.exit(1)
        finally:
            await sandbox.stop()
            await llm.close()

    return result


async def _run_agent_queued(
    task: str,
    repo: str,
    cfg: Any,
    provider_name: str,
    api_key: str,
    *,
    queue_backend: str,
    redis_url: str,
    max_concurrent_runs: int,
) -> None:
    """Execute the agent via queue → worker → react_loop.

    Enqueues a :class:`Task`, starts a :class:`Worker`, and waits for
    the task to complete or fail.
    """
    from agent_forge.agent.models import AgentConfig
    from agent_forge.orchestration.events import EventBus
    from agent_forge.orchestration.queue import InMemoryQueue, Task, TaskQueue, TaskStatus
    from agent_forge.orchestration.worker import Worker

    agent_config = AgentConfig(
        model=cfg.agent.default_model,
        max_iterations=cfg.agent.max_iterations,
        max_tokens_per_run=cfg.agent.max_tokens_per_run,
        temperature=cfg.agent.temperature,
    )

    event_bus = EventBus()

    # Select queue backend
    queue: TaskQueue
    if queue_backend == "redis":
        from agent_forge.orchestration.redis_queue import RedisQueue

        queue = RedisQueue(
            redis_url=redis_url,
            max_concurrent_runs=max_concurrent_runs or 0,
        )
    else:
        queue = InMemoryQueue()

    # Build the task runner — this is what the Worker calls for each task
    task_runner = _make_task_runner(cfg, provider_name, api_key, event_bus)

    worker = Worker(queue=queue, event_bus=event_bus, task_runner=task_runner)

    # Enqueue and run
    queue_task = Task(
        id="",
        task_description=task,
        repo_path=repo,
        config=agent_config,
    )

    with console.status("[bold green]Agent running (queue mode)...", spinner="dots"):
        try:
            task_id = await queue.enqueue(queue_task)
            console.print(f"[dim]Enqueued task {task_id}[/dim]")
            await worker.start()

            # Poll until the task completes or fails
            while True:
                status = await queue.get_status(task_id)
                if status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    break
                await asyncio.sleep(0.5)

            if status == TaskStatus.FAILED:
                err_console.print("[red]Task failed.[/red]")
                sys.exit(1)
            console.print("[green]Task completed.[/green]")
        except Exception as exc:  # noqa: BLE001 — top-level catch-all
            err_console.print(f"[red]Agent failed:[/red] {exc}")
            sys.exit(1)
        finally:
            await worker.stop()
            if hasattr(queue, "close"):
                await queue.close()


def _create_llm(provider_name: str, api_key: str) -> Any:
    """Create an LLM provider instance by name."""
    from agent_forge.llm.gemini import GeminiProvider

    if provider_name == "gemini":
        return GeminiProvider(api_key=api_key)
    err_console.print(
        f"[red]Provider '{provider_name}' not yet implemented.[/red] Only 'gemini' is available."
    )
    sys.exit(1)


def _make_task_runner(
    _cfg: Any,
    provider_name: str,
    api_key: str,
    event_bus: Any,
) -> Any:
    """Build an async callable that the Worker invokes for each task.

    Each invocation creates its own LLM client, sandbox, and tool
    registry — resources are scoped to the single run.
    """
    from agent_forge.agent.core import react_loop
    from agent_forge.agent.models import AgentRun
    from agent_forge.agent.prompts import build_system_prompt
    from agent_forge.sandbox.docker import DockerSandbox
    from agent_forge.tools import create_default_registry

    sandbox_config = _build_sandbox_config(_cfg)

    async def _runner(task: Any) -> None:
        tools = create_default_registry()
        task.config.system_prompt = build_system_prompt(
            task.task_description,
            tools.list_definitions(),
            sandbox_image=sandbox_config.image,
            network_enabled=sandbox_config.network_enabled,
            command_timeout_seconds=sandbox_config.timeout_seconds,
        )
        agent_run = AgentRun(
            task=task.task_description,
            repo_path=task.repo_path,
            config=task.config,
        )
        llm = _create_llm(provider_name, api_key)
        sandbox = DockerSandbox()

        try:
            await sandbox.start(repo_path=task.repo_path, config=sandbox_config)
            await react_loop(
                agent_run,
                llm,
                tools,
                sandbox,
                event_bus=event_bus,
            )
        finally:
            await sandbox.stop()
            await llm.close()

    return _runner


def _build_sandbox_config(cfg: Any) -> SandboxConfig:
    """Convert resolved app config into a sandbox runtime config."""
    return SandboxConfig(
        image=cfg.sandbox.image,
        cpu_limit=cfg.sandbox.cpu_limit,
        memory_limit=cfg.sandbox.memory_limit,
        timeout_seconds=cfg.sandbox.timeout_seconds,
        network_enabled=cfg.sandbox.network_enabled,
        writable_cache_mounts=cfg.sandbox.writable_cache_mounts,
    )


def _build_cli_overrides(
    *,
    model: str | None,
    provider: str | None,
    max_iterations: int | None,
    sandbox_image: str | None,
    network: bool | None,
    command_timeout: int | None,
) -> dict[str, Any] | None:
    """Collect non-empty CLI overrides into config dotted keys."""
    cli_overrides: dict[str, Any] = {}
    if model is not None:
        cli_overrides["agent.default_model"] = model
    if provider is not None:
        cli_overrides["agent.default_provider"] = provider
    if max_iterations is not None:
        cli_overrides["agent.max_iterations"] = max_iterations
    if sandbox_image is not None:
        cli_overrides["sandbox.image"] = sandbox_image
    if network is not None:
        cli_overrides["sandbox.network_enabled"] = network
    if command_timeout is not None:
        cli_overrides["sandbox.timeout_seconds"] = command_timeout
    return cli_overrides or None


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


def _run_output_payload(run: Any) -> dict[str, object]:
    """Build a stable machine-readable summary for a completed run."""
    duration_seconds: float | None = None
    if run.completed_at:
        duration_seconds = (run.completed_at - run.created_at).total_seconds()

    run_dir = USER_CONFIG_DIR / "runs" / run.id
    return {
        "schema_version": "agent-forge-run-result-v1",
        "run_id": run.id,
        "state": run.state.value,
        "task": run.task,
        "repo_path": run.repo_path,
        "iterations": run.iterations,
        "created_at": run.created_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_seconds": duration_seconds,
        "error": run.error,
        "total_tokens": {
            "prompt_tokens": run.total_tokens.prompt_tokens,
            "completion_tokens": run.total_tokens.completion_tokens,
            "total_tokens": run.total_tokens.total_tokens,
        },
        "artifacts": {
            "run_dir": str(run_dir),
            "run_json": str(run_dir / "run.json"),
            "messages_jsonl": str(run_dir / "messages.jsonl"),
            "events_jsonl": str(run_dir / "events.jsonl"),
            "summary_json": str(run_dir / "summary.json"),
        },
    }


def _emit_run_output(
    run: Any,
    *,
    output_format: str,
    report_file: Path | None,
) -> None:
    """Emit either rich text or machine-readable JSON for a run."""
    payload = _run_output_payload(run)

    if report_file is not None:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if output_format == "json":
        click.echo(json.dumps(payload))
        return

    _display_run_summary(run)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@main.command()
@click.argument("run_id")
@click.option(
    "--output-format",
    default="text",
    type=click.Choice(["text", "json"], case_sensitive=False),
    help="Render a human summary or a machine-readable JSON result.",
)
def status(run_id: str, output_format: str) -> None:
    """Show the status and summary of a specific run."""
    from agent_forge.agent.persistence import load_run

    try:
        agent_run = load_run(run_id)
    except FileNotFoundError:
        err_console.print(f"[red]Run not found:[/red] {run_id}")
        sys.exit(1)

    _emit_run_output(agent_run, output_format=output_format, report_file=None)


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


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@main.command()
@click.option("--host", default=None, help="Bind host override.")
@click.option("--port", default=None, type=int, help="Bind port override.")
@click.option(
    "--service-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the hosted service root directory.",
)
def serve(host: str | None, port: int | None, service_root: Path | None) -> None:
    """Run the hosted FastAPI service."""
    import uvicorn

    cli_overrides: dict[str, Any] = {}
    if host is not None:
        cli_overrides["service.host"] = host
    if port is not None:
        cli_overrides["service.port"] = port
    if service_root is not None:
        cli_overrides["service.root_dir"] = str(service_root)

    cfg = load_config(cli_overrides=cli_overrides or None)

    from agent_forge.service import create_app

    app = create_app(service_root=Path(cfg.service.root_dir).expanduser(), config=cfg)
    uvicorn.run(app, host=cfg.service.host, port=cfg.service.port)


if __name__ == "__main__":
    main()
