"""Tests for the Proof-of-Audit challenge evidence generator."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from click.testing import CliRunner

from plugins.proof_of_audit.challenge import compare_reports, llm_enhanced_compare
from plugins.proof_of_audit.cli import poa_cli
from plugins.proof_of_audit.models import (
    ChallengeEvidence,
    DivergenceType,
    MissedFinding,
    max_severity,
    severity_rank,
)

# ---------------------------------------------------------------------------
# Fixtures — sample reports
# ---------------------------------------------------------------------------


def _make_finding(**overrides: Any) -> dict[str, Any]:
    """Build a minimal finding dict with sensible defaults."""
    base: dict[str, Any] = {
        "finding_id": "F-001",
        "title": "Reentrancy in withdraw()",
        "severity": "high",
        "category": "reentrancy",
        "description": "CEI pattern not followed.",
        "impact": "Fund drain",
        "recommendation": "Use ReentrancyGuard",
        "confidence": "high",
        "affected_function": "withdraw",
    }
    base.update(overrides)
    return base


def _make_report(
    findings: list[dict[str, Any]] | None = None,
    run_id: str = "run-test",
) -> dict[str, Any]:
    """Build a minimal PoA report."""
    return {
        "schema_version": "proof-of-audit-report-v1",
        "run_id": run_id,
        "summary": "Test report",
        "confidence": "high",
        "findings": findings or [],
        "stats": {
            "finding_count": len(findings or []),
            "max_severity": None,
            "severity_breakdown": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        },
    }


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for challenge evidence Pydantic models."""

    def test_missed_finding_serializes(self) -> None:
        mf = MissedFinding(
            finding_id="F-001",
            title="Reentrancy",
            severity="high",
            category="reentrancy",
            confidence="high",
            explanation="Not detected.",
        )
        data = mf.model_dump()
        assert data["finding_id"] == "F-001"
        assert data["severity"] == "high"

    def test_challenge_evidence_serializes(self) -> None:
        evidence = ChallengeEvidence(
            evidence_type=DivergenceType.MISSED_VULNERABILITY,
            missed_findings=[],
            severity_summary="low",
            challenge_confidence="low",
        )
        data = json.loads(evidence.model_dump_json())
        assert data["evidence_type"] == "missed_vulnerability"

    def test_divergence_type_values(self) -> None:
        assert DivergenceType.MISSED_VULNERABILITY.value == "missed_vulnerability"
        assert DivergenceType.SEVERITY_DOWNGRADE.value == "severity_downgrade"
        assert DivergenceType.FALSE_NEGATIVE.value == "false_negative"

    def test_severity_rank_ordering(self) -> None:
        assert severity_rank("critical") > severity_rank("high")
        assert severity_rank("high") > severity_rank("medium")
        assert severity_rank("medium") > severity_rank("low")
        assert severity_rank("unknown") == 0

    def test_max_severity(self) -> None:
        assert max_severity(["low", "high", "medium"]) == "high"
        assert max_severity(["low"]) == "low"
        assert max_severity([]) == "low"
        assert max_severity(["critical", "low"]) == "critical"


# ---------------------------------------------------------------------------
# Comparison logic tests
# ---------------------------------------------------------------------------


class TestCompareReports:
    """Tests for pure structural comparison."""

    def test_no_divergence_returns_empty_evidence(self) -> None:
        finding = _make_finding()
        original = _make_report([finding])
        challenger = _make_report([finding])

        evidence = compare_reports(original, challenger)

        assert evidence.missed_findings == []
        assert evidence.challenge_confidence == "low"

    def test_missed_vulnerability_detected(self) -> None:
        original = _make_report([])  # original found nothing
        challenger = _make_report([_make_finding()])  # challenger found reentrancy

        evidence = compare_reports(original, challenger)

        assert evidence.evidence_type == DivergenceType.MISSED_VULNERABILITY
        assert len(evidence.missed_findings) == 1
        assert evidence.missed_findings[0].title == "Reentrancy in withdraw()"
        assert evidence.severity_summary == "high"

    def test_severity_downgrade_detected(self) -> None:
        original_finding = _make_finding(severity="medium")
        challenger_finding = _make_finding(severity="critical")

        original = _make_report([original_finding])
        challenger = _make_report([challenger_finding])

        evidence = compare_reports(original, challenger)

        assert evidence.evidence_type == DivergenceType.SEVERITY_DOWNGRADE
        assert len(evidence.missed_findings) == 1
        assert "medium" in evidence.missed_findings[0].explanation
        assert "critical" in evidence.missed_findings[0].explanation

    def test_multiple_missed_findings_high_confidence(self) -> None:
        findings = [
            _make_finding(
                finding_id=f"F-{i:03d}",
                title=f"Issue {i}",
                category=f"cat-{i}",
                affected_function=f"func_{i}",
            )
            for i in range(3)
        ]
        original = _make_report([])
        challenger = _make_report(findings)

        evidence = compare_reports(original, challenger)

        assert evidence.evidence_type == DivergenceType.MISSED_VULNERABILITY
        assert len(evidence.missed_findings) == 3
        assert evidence.challenge_confidence == "high"

    def test_mixed_missed_and_downgrade(self) -> None:
        shared_finding = _make_finding(
            finding_id="F-SHARED",
            title="Access control",
            category="access_control",
            affected_function="setOwner",
        )
        new_finding = _make_finding(
            finding_id="F-NEW",
            title="Unchecked return",
            category="unchecked_return",
            affected_function="transfer",
        )

        original = _make_report([{**shared_finding, "severity": "low"}])
        challenger = _make_report(
            [{**shared_finding, "severity": "high"}, new_finding]
        )

        evidence = compare_reports(original, challenger)

        # Missed takes precedence over downgrade
        assert evidence.evidence_type == DivergenceType.MISSED_VULNERABILITY
        assert len(evidence.missed_findings) == 2  # 1 missed + 1 downgrade

    def test_empty_reports(self) -> None:
        evidence = compare_reports(_make_report([]), _make_report([]))
        assert evidence.missed_findings == []
        assert evidence.challenge_confidence == "low"

    def test_case_insensitive_matching(self) -> None:
        original = _make_report(
            [_make_finding(title="REENTRANCY IN WITHDRAW()", category="REENTRANCY")]
        )
        challenger = _make_report(
            [_make_finding(title="reentrancy in withdraw()", category="reentrancy")]
        )

        evidence = compare_reports(original, challenger)
        assert evidence.missed_findings == []

    def test_finding_id_mismatch_still_matches_by_key(self) -> None:
        """Two agents may assign different finding_ids to the same issue."""
        original = _make_report([_make_finding(finding_id="ORIGINAL-001")])
        challenger = _make_report([_make_finding(finding_id="CHALLENGER-001")])

        evidence = compare_reports(original, challenger)
        assert evidence.missed_findings == []


# ---------------------------------------------------------------------------
# LLM-enhanced comparison tests
# ---------------------------------------------------------------------------


class TestLLMEnhancedCompare:
    """Tests for LLM-enhanced comparison (mocked LLM)."""

    @pytest.mark.asyncio
    async def test_llm_parses_valid_response(self) -> None:
        expected_evidence = ChallengeEvidence(
            evidence_type=DivergenceType.MISSED_VULNERABILITY,
            missed_findings=[
                MissedFinding(
                    finding_id="F-LLM",
                    title="Unchecked call",
                    severity="high",
                    category="unchecked_return",
                    confidence="high",
                    explanation="LLM detected missed unchecked return value.",
                )
            ],
            severity_summary="high",
            challenge_confidence="high",
        )

        mock_response = AsyncMock()
        mock_response.text = expected_evidence.model_dump_json()

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(return_value=mock_response)

        with patch(
            "agent_forge.llm.factory.create_provider",
            return_value=mock_provider,
        ):
            evidence = await llm_enhanced_compare(
                _make_report([]),
                _make_report([_make_finding()]),
                provider="gemini",
            )

        assert evidence.evidence_type == DivergenceType.MISSED_VULNERABILITY
        assert len(evidence.missed_findings) == 1
        assert evidence.missed_findings[0].title == "Unchecked call"

    @pytest.mark.asyncio
    async def test_llm_falls_back_on_invalid_json(self) -> None:
        mock_response = AsyncMock()
        mock_response.text = "This is not JSON"

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(return_value=mock_response)

        with patch(
            "agent_forge.llm.factory.create_provider",
            return_value=mock_provider,
        ):
            evidence = await llm_enhanced_compare(
                _make_report([]),
                _make_report([_make_finding()]),
                provider="gemini",
            )

        # Should fall back to pure comparison
        assert evidence.evidence_type == DivergenceType.MISSED_VULNERABILITY
        assert len(evidence.missed_findings) == 1

    @pytest.mark.asyncio
    async def test_llm_strips_markdown_fences(self) -> None:
        expected_evidence = ChallengeEvidence(
            evidence_type=DivergenceType.SEVERITY_DOWNGRADE,
            missed_findings=[],
            severity_summary="low",
            challenge_confidence="low",
        )

        mock_response = AsyncMock()
        mock_response.text = f"```json\n{expected_evidence.model_dump_json()}\n```"

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(return_value=mock_response)

        with patch(
            "agent_forge.llm.factory.create_provider",
            return_value=mock_provider,
        ):
            evidence = await llm_enhanced_compare(
                _make_report([]),
                _make_report([]),
                provider="openai",
            )

        assert evidence.evidence_type == DivergenceType.SEVERITY_DOWNGRADE


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCLI:
    """Tests for the challenge-evidence CLI command."""

    def test_challenge_evidence_structural(self, tmp_path: Path) -> None:
        original = _make_report([])
        challenger = _make_report([_make_finding()])

        (tmp_path / "original.json").write_text(
            json.dumps(original), encoding="utf-8"
        )
        (tmp_path / "challenger.json").write_text(
            json.dumps(challenger), encoding="utf-8"
        )
        output = tmp_path / "evidence.json"

        runner = CliRunner()
        result = runner.invoke(
            poa_cli,
            [
                "challenge-evidence",
                "--original", str(tmp_path / "original.json"),
                "--challenger", str(tmp_path / "challenger.json"),
                "--output", str(output),
            ],
        )

        assert result.exit_code == 0, result.output
        assert output.exists()

        evidence = json.loads(output.read_text(encoding="utf-8"))
        assert evidence["evidence_type"] == "missed_vulnerability"
        assert len(evidence["missed_findings"]) == 1

    def test_challenge_evidence_no_divergence(self, tmp_path: Path) -> None:
        finding = _make_finding()
        report = _make_report([finding])

        (tmp_path / "original.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        (tmp_path / "challenger.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        output = tmp_path / "evidence.json"

        runner = CliRunner()
        result = runner.invoke(
            poa_cli,
            [
                "challenge-evidence",
                "--original", str(tmp_path / "original.json"),
                "--challenger", str(tmp_path / "challenger.json"),
                "--output", str(output),
            ],
        )

        assert result.exit_code == 0
        evidence = json.loads(output.read_text(encoding="utf-8"))
        assert evidence["missed_findings"] == []
        assert evidence["challenge_confidence"] == "low"

    def test_challenge_evidence_invalid_json(self, tmp_path: Path) -> None:
        (tmp_path / "original.json").write_text("not json", encoding="utf-8")
        (tmp_path / "challenger.json").write_text("{}", encoding="utf-8")
        output = tmp_path / "evidence.json"

        runner = CliRunner()
        result = runner.invoke(
            poa_cli,
            [
                "challenge-evidence",
                "--original", str(tmp_path / "original.json"),
                "--challenger", str(tmp_path / "challenger.json"),
                "--output", str(output),
            ],
        )

        assert result.exit_code != 0

    def test_challenge_evidence_missing_file(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            poa_cli,
            [
                "challenge-evidence",
                "--original", str(tmp_path / "nonexistent.json"),
                "--challenger", str(tmp_path / "nonexistent2.json"),
                "--output", str(tmp_path / "evidence.json"),
            ],
        )

        assert result.exit_code != 0
