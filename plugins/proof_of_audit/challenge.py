"""Challenge evidence generation — compare two audit reports.

This module lives in the proof-of-audit plugin (extension layer) and
implements the core comparison logic for cross-agent challenge proposals.
"""

from __future__ import annotations

import json
from typing import Any

from plugins.proof_of_audit.models import (
    ChallengeEvidence,
    DivergenceType,
    MissedFinding,
    max_severity,
    severity_rank,
)

# ---------------------------------------------------------------------------
# Finding matching
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase + strip for fuzzy matching."""
    return text.strip().lower()


def _finding_key(finding: dict[str, Any]) -> tuple[str, str, str]:
    """Build a match key from a finding dict.

    We use (category, affected_function, title) as a composite key to
    determine if two findings refer to the same issue — even if their
    ``finding_id`` values differ across agents.
    """
    return (
        _normalize(finding.get("category", "")),
        _normalize(finding.get("affected_function", "") or ""),
        _normalize(finding.get("title", "")),
    )


def _extract_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the findings list from a report, tolerating missing keys."""
    return report.get("findings", [])


# ---------------------------------------------------------------------------
# Pure comparison
# ---------------------------------------------------------------------------


def compare_reports(
    original: dict[str, Any],
    challenger: dict[str, Any],
) -> ChallengeEvidence:
    """Compare two audit reports and produce challenge evidence.

    Args:
        original: The published audit report (the claim being challenged).
        challenger: The challenger agent's analysis of the same contract.

    Returns:
        A ``ChallengeEvidence`` payload ready for the PoA challenge API.
        If no divergence is found, ``missed_findings`` will be empty and
        ``evidence_type`` defaults to ``missed_vulnerability``.
    """
    original_findings = _extract_findings(original)
    challenger_findings = _extract_findings(challenger)

    # Build an index of original findings by composite key
    original_keys: dict[tuple[str, str, str], dict[str, Any]] = {}
    for finding in original_findings:
        original_keys[_finding_key(finding)] = finding

    missed: list[MissedFinding] = []
    downgrades: list[MissedFinding] = []

    for c_finding in challenger_findings:
        key = _finding_key(c_finding)
        o_finding = original_keys.get(key)

        if o_finding is None:
            # Finding exists in challenger but not in original → missed
            missed.append(
                MissedFinding(
                    finding_id=c_finding.get("finding_id", "unknown"),
                    title=c_finding.get("title", "Untitled"),
                    severity=c_finding.get("severity", "medium"),
                    category=c_finding.get("category", "unknown"),
                    confidence=c_finding.get("confidence", "medium"),
                    explanation=(
                        f"The original audit did not detect: "
                        f"{c_finding.get('title', 'this finding')}."
                    ),
                )
            )
        elif severity_rank(c_finding.get("severity", "low")) > severity_rank(
            o_finding.get("severity", "low")
        ):
            # Same finding, but challenger rates it higher → downgrade
            downgrades.append(
                MissedFinding(
                    finding_id=c_finding.get("finding_id", "unknown"),
                    title=c_finding.get("title", "Untitled"),
                    severity=c_finding.get("severity", "medium"),
                    category=c_finding.get("category", "unknown"),
                    confidence=c_finding.get("confidence", "medium"),
                    explanation=(
                        f"The original audit rated this as "
                        f"{o_finding.get('severity', 'unknown')} "
                        f"but challenger analysis rates it as "
                        f"{c_finding.get('severity', 'unknown')}."
                    ),
                )
            )

    # Determine primary divergence type
    if missed:
        evidence_type = DivergenceType.MISSED_VULNERABILITY
        all_divergent = missed + downgrades
    elif downgrades:
        evidence_type = DivergenceType.SEVERITY_DOWNGRADE
        all_divergent = downgrades
    else:
        # No divergence — return empty evidence
        return ChallengeEvidence(
            evidence_type=DivergenceType.MISSED_VULNERABILITY,
            missed_findings=[],
            severity_summary="low",
            challenge_confidence="low",
        )

    # Compute summary
    severities = [f.severity for f in all_divergent]
    sev_summary = max_severity(severities)

    # Confidence based on number + severity of divergences
    if len(all_divergent) >= 3 or sev_summary in ("critical", "high"):
        confidence = "high"
    elif len(all_divergent) >= 2 or sev_summary == "medium":
        confidence = "medium"
    else:
        confidence = "low"

    return ChallengeEvidence(
        evidence_type=evidence_type,
        missed_findings=all_divergent,
        severity_summary=sev_summary,
        challenge_confidence=confidence,
    )


# ---------------------------------------------------------------------------
# LLM-enhanced comparison
# ---------------------------------------------------------------------------


_LLM_COMPARE_PROMPT = """\
You are an expert smart-contract security auditor performing a cross-audit challenge analysis.

## Task
Compare the ORIGINAL audit report with the CHALLENGER audit report for the same smart contract.
Identify any vulnerabilities the ORIGINAL missed or downgraded in severity.

## Original Audit Report
```json
{original}
```

## Challenger Audit Report
```json
{challenger}
```

## Instructions
1. For each finding in the CHALLENGER report that is NOT in the ORIGINAL, explain why it was missed.
2. For findings present in both but with different severity, explain the discrepancy.
3. Rate your overall confidence in the challenge: low, medium, or high.

## Output Format
Return ONLY valid JSON matching this schema (no markdown fencing):
{{
  "evidence_type": "missed_vulnerability" | "severity_downgrade" | "false_negative",
  "missed_findings": [
    {{
      "finding_id": "...",
      "title": "...",
      "severity": "critical" | "high" | "medium" | "low",
      "category": "...",
      "confidence": "low" | "medium" | "high",
      "explanation": "..."
    }}
  ],
  "severity_summary": "critical" | "high" | "medium" | "low",
  "challenge_confidence": "low" | "medium" | "high"
}}
"""


async def llm_enhanced_compare(
    original: dict[str, Any],
    challenger: dict[str, Any],
    *,
    provider: str = "gemini",
    model: str | None = None,
) -> ChallengeEvidence:
    """Use an LLM to perform deep analysis of divergences between two reports.

    Falls back to ``compare_reports()`` if the LLM response cannot be parsed.

    Args:
        original: The published audit report.
        challenger: The challenger agent's analysis.
        provider: LLM provider name (gemini, openai, anthropic).
        model: Optional model override.

    Returns:
        A ``ChallengeEvidence`` payload.
    """
    from agent_forge.llm.factory import create_provider

    llm = create_provider(provider, model=model)

    prompt = _LLM_COMPARE_PROMPT.format(
        original=json.dumps(original, indent=2),
        challenger=json.dumps(challenger, indent=2),
    )

    response = await llm.generate(prompt)

    # Parse the LLM's JSON response
    try:
        # Strip markdown code fences if present
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[: text.rfind("```")]
            text = text.strip()

        data = json.loads(text)
        return ChallengeEvidence.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        # Fallback to pure comparison if LLM output is unparseable
        return compare_reports(original, challenger)
