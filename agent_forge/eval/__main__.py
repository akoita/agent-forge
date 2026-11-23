"""CLI entrypoint for offline deterministic coding-harness evaluation."""

from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import redirect_stdout
from pathlib import Path

from agent_forge.eval.benchmark import render_markdown_report
from agent_forge.eval.deterministic import check_deterministic_gates, run_deterministic_eval


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_forge.eval",
        description="Run the offline deterministic coding-harness benchmark gate.",
    )
    parser.add_argument(
        "--suite",
        choices=("smoke", "full"),
        default="smoke",
        help="Task suite tier to execute (default: smoke).",
    )
    parser.add_argument(
        "--task",
        dest="tasks",
        action="append",
        default=[],
        help="Specific task ID to run (repeatable; overrides --suite).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Directory to write eval-report.json, eval-report.md, and trajectories/.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write the JSON BenchmarkReport to this file.",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=None,
        help="Write the Markdown summary report to this file.",
    )
    parser.add_argument(
        "--trajectory-dir",
        type=Path,
        default=None,
        help="Directory to write per-attempt JSONL trajectories.",
    )
    parser.add_argument(
        "--assert-gates",
        action="store_true",
        help="Exit non-zero if deterministic quality gates fail.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Maximum concurrent task evaluations per profile (default: 1).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run deterministic evaluation and optionally enforce quality gates."""
    args = _build_parser().parse_args(argv)
    output_json: Path | None = args.output_json
    output_md: Path | None = args.output_md
    trajectory_dir: Path | None = args.trajectory_dir

    if args.report_dir is not None:
        output_json = output_json or (args.report_dir / "eval-report.json")
        output_md = output_md or (args.report_dir / "eval-report.md")
        trajectory_dir = trajectory_dir or (args.report_dir / "trajectories")

    with redirect_stdout(sys.stderr):
        report = asyncio.run(
            run_deterministic_eval(
                suite=args.suite,
                task_ids=args.tasks or None,
                output_json=output_json,
                output_md=output_md,
                trajectory_dir=trajectory_dir,
                max_concurrency=max(1, args.concurrency),
            )
        )
    sys.stdout.write(render_markdown_report(report))

    if args.assert_gates:
        failures = check_deterministic_gates(report)
        if failures:
            for item in failures:
                sys.stderr.write(f"EVAL GATE FAILED: {item}\n")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
