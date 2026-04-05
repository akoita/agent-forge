"""CLI for the Proof-of-Audit plugin.

Provides the ``agent-forge-poa`` entry point with audit-specific commands
that build on the core Agent Forge framework.

Usage::

    python -m plugins.proof_of_audit.cli challenge-evidence \\
        --original report_a.json \\
        --challenger report_b.json \\
        --output evidence.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click

from plugins.proof_of_audit.challenge import compare_reports, llm_enhanced_compare


@click.group()
def poa_cli() -> None:
    """Proof-of-Audit — domain-specific tools for smart-contract auditing."""


@poa_cli.command("challenge-evidence")
@click.option(
    "--original",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the original (published) audit report JSON.",
)
@click.option(
    "--challenger",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the challenger agent's audit report JSON.",
)
@click.option(
    "--output",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to write the challenge evidence JSON.",
)
@click.option(
    "--llm-provider",
    default=None,
    type=click.Choice(["gemini", "openai", "anthropic"]),
    help="LLM provider for enhanced analysis (optional).",
)
@click.option(
    "--llm-model",
    default=None,
    help="LLM model override (e.g., gemini-2.0-flash).",
)
def challenge_evidence(
    original: Path,
    challenger: Path,
    output: Path,
    llm_provider: str | None,
    llm_model: str | None,
) -> None:
    """Generate challenge evidence by comparing two audit reports.

    Reads the ORIGINAL published audit report and the CHALLENGER agent's
    independent analysis, then produces structured evidence identifying
    missed vulnerabilities or severity downgrades.

    Without --llm-provider, performs a pure structural comparison.
    With --llm-provider, uses the specified LLM for deeper reasoning.
    """
    # Load reports
    try:
        original_report = json.loads(original.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        click.echo(f"Error reading original report: {exc}", err=True)
        sys.exit(1)

    try:
        challenger_report = json.loads(challenger.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        click.echo(f"Error reading challenger report: {exc}", err=True)
        sys.exit(1)

    # Compare
    if llm_provider:
        click.echo(f"Running LLM-enhanced comparison (provider={llm_provider})...")
        evidence = asyncio.run(
            llm_enhanced_compare(
                original_report,
                challenger_report,
                provider=llm_provider,
                model=llm_model,
            )
        )
    else:
        click.echo("Running structural comparison...")
        evidence = compare_reports(original_report, challenger_report)

    # Write output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        evidence.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    # Summary
    n = len(evidence.missed_findings)
    click.echo(
        f"Challenge evidence written to {output}\n"
        f"  Type:       {evidence.evidence_type.value}\n"
        f"  Findings:   {n}\n"
        f"  Severity:   {evidence.severity_summary}\n"
        f"  Confidence: {evidence.challenge_confidence}"
    )


def main() -> None:
    """Entry point for ``agent-forge-poa``."""
    poa_cli()


if __name__ == "__main__":
    main()
