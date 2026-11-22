"""Configuration and recorded exchanges for the model qualification command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
import respx
from click.testing import CliRunner

from agent_forge.cli import main
from agent_forge.config import ForgeConfig, ModelProfileSettings
from agent_forge.llm.qualification import probe_model

if TYPE_CHECKING:
    from pathlib import Path


def test_validate_profile_from_toml_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new alias can be validated without contacting a provider."""
    (tmp_path / "agent-forge.toml").write_text(
        '[model_profiles.candidate]\nprovider = "gemini"\nmodel = "candidate-v1"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = CliRunner().invoke(main, ["models", "validate", "candidate"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["requested_model"] == "candidate-v1"
    assert report["configuration"]["status"] == "passed"
    assert report["live_tool_exchange"]["status"] == "not_run"
    assert report["independent_fix"]["status"] == "not_run"

    missing_key = CliRunner().invoke(main, ["models", "validate", "candidate", "--live"])
    assert missing_key.exit_code == 1
    live = json.loads(missing_key.stdout)["live_tool_exchange"]
    assert live["requests"] == 0
    assert live["status"] == "failed"


@respx.mock
@pytest.mark.asyncio
async def test_gemini_qualification_preserves_tool_name_and_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "recorded-key")
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/candidate-v1:generateContent"
    )
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "modelVersion": "candidate-version",
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {"name": "read_probe_value", "args": {}},
                                    "thoughtSignature": "recorded-signature",
                                }
                            ]
                        }
                    }
                ],
            },
        ),
        httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "forge-probe-7391"}]}}]},
        ),
    ]
    result = await probe_model(
        ForgeConfig(), ModelProfileSettings(provider="gemini", model="candidate-v1")
    )
    assert result.status == "passed"
    assert result.reported_models == ["candidate-version"]
    assert route.call_count == result.requests == 2
    contents = json.loads(route.calls.last.request.content)["contents"]
    assert contents[-2]["parts"][0]["thoughtSignature"] == "recorded-signature"
    reply = contents[-1]["parts"][0]["functionResponse"]
    assert reply == {"name": "read_probe_value", "response": {"result": "forge-probe-7391"}}


@respx.mock
@pytest.mark.asyncio
async def test_openai_qualification_consumes_correlated_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "recorded-key")
    route = respx.post("https://api.openai.com/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_42",
                                    "type": "function",
                                    "function": {"name": "read_probe_value", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ]
            },
        ),
        httpx.Response(
            200,
            json={
                "model": "served-version",
                "choices": [{"finish_reason": "stop", "message": {"content": "forge-probe-7391"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
            },
        ),
    ]
    result = await probe_model(
        ForgeConfig(), ModelProfileSettings(provider="openai", model="candidate-v1")
    )
    assert result.status == "passed"
    assert result.usage.total_tokens == 17
    assert result.reported_models == ["served-version"]
    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == "candidate-v1"
    assert payload["max_tokens"] == 256
    assert payload["messages"][-1]["tool_call_id"] == "call_42"


def test_run_uses_named_model_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "agent-forge.toml").write_text(
        '[model_profiles.candidate]\nprovider = "openai"\nmodel = "candidate-v1"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "recorded-key")
    with patch("agent_forge.cli._run_agent") as run, patch("agent_forge.cli._emit_run_output"):
        result = CliRunner().invoke(
            main, ["run", "--task", "Fix a fixture", "--repo", ".", "--model-profile", "candidate"]
        )
    assert result.exit_code == 0, result.output
    assert run.call_args.args[2].agent.default_model == "candidate-v1"
    assert run.call_args.args[3] == "openai"


@respx.mock
@pytest.mark.asyncio
async def test_live_probe_does_not_retry_service_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "recorded-key")
    route = respx.post("https://api.openai.com/v1/chat/completions").respond(503)
    result = await probe_model(
        ForgeConfig(), ModelProfileSettings(provider="openai", model="candidate-v1")
    )
    assert result.status == "failed"
    assert result.reason == "LLMResponseError"
    assert route.call_count == result.requests == 1


@pytest.mark.parametrize("reason", ["verified", "check_failed"])
def test_qualification_reports_independent_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    from agent_forge.agent.models import AgentConfig, AgentRun, RunState
    from agent_forge.agent.verification import VerificationResult

    (tmp_path / "agent-forge.toml").write_text(
        '[model_profiles.candidate]\nprovider = "openai"\nmodel = "candidate-v1"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "recorded-key")
    passed = reason == "verified"
    run = AgentRun(
        "Fix the fixture",
        str(tmp_path),
        AgentConfig(),
        state=RunState.COMPLETED if passed else RunState.FAILED,
        verification=VerificationResult(
            "base", "recorded patch", reason, "fixture-check", int(not passed)
        ),
    )
    report = {"live_tool_exchange": {"status": "passed"}, "independent_fix": {"status": "not_run"}}
    with (
        patch("agent_forge.llm.qualification.qualification_report", return_value=report),
        patch("agent_forge.cli._run_agent", return_value=run) as run_agent,
    ):
        result = CliRunner().invoke(
            main,
            [
                "models",
                "validate",
                "candidate",
                "--live",
                "--fix-repo",
                str(tmp_path),
                "--fix-task",
                "Fix the fixture",
                "--verify-command",
                "fixture-check",
            ],
        )
    assert result.exit_code == int(not passed), result.output
    grade = json.loads(result.stdout)["independent_fix"]
    assert grade["status"] == ("passed" if passed else "failed")
    assert grade["reason"] == reason
    assert grade["base_revision"] == "base"
    assert grade["run_id"] == run.id
    assert grade["check_exit_code"] == int(not passed)
    task, repo, config, provider, _ = run_agent.call_args.args
    assert (task, repo, provider) == ("Fix the fixture", str(tmp_path), "openai")
    assert config.agent.default_model == "candidate-v1"
    assert config.agent.max_iterations == 6
    assert config.agent.max_tokens_per_run == 24_000
    assert config.sandbox.network_enabled is False
    assert run_agent.call_args.kwargs["verify_command"] == "fixture-check"


def test_qualification_fix_requires_all_options(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["models", "validate", "candidate", "--fix-repo", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "A coding probe requires" in result.output
