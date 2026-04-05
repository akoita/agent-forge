# Testing Guide

> How to run, write, and organize tests in Agent Forge.

## Test Suites

| Suite           | Command                 | Requirements              | Markers                        |
| --------------- | ----------------------- | ------------------------- | ------------------------------ |
| **Unit**        | `make test-unit`        | None                      | default (no marker)            |
| **Integration** | `make test-integration` | Docker                    | `@pytest.mark.integration`     |
| **E2E**         | `make test-e2e`         | Docker + `GEMINI_API_KEY` | `@pytest.mark.e2e`             |
| **All non-E2E** | `make test`             | Docker                    | excludes `e2e` + `integration` |

## Running Tests

```bash
# Unit tests only (fast, no Docker needed)
make test-unit

# Full test suite (unit + integration, requires Docker)
make test

# E2E tests (manual trigger, requires API key)
export GEMINI_API_KEY="your-key"
make test-e2e

# Single test file
pytest tests/unit/test_tools.py -v

# Single test
pytest tests/unit/test_tools.py::TestReadFileTool::test_read_existing_file -v

# With coverage
pytest --cov=agent_forge --cov-report=term-missing
```

## Test Structure

```
tests/
├── conftest.py                         # Shared fixtures + marker registration
├── fixtures/
│   ├── conftest.py                     # Fixture-specific config
│   └── sample_repo/                    # Flask app fixture for agent testing
│       ├── app.py
│       ├── requirements.txt
│       ├── test_app.py
│       └── utils.py
├── unit/                               # Fast, isolated, no external deps
│   ├── test_agent_core.py              # ReAct loop with mocked LLM
│   ├── test_bwrap_sandbox.py           # Bubblewrap sandbox unit tests
│   ├── test_challenge_evidence.py      # PoA challenge evidence generator (plugin)
│   ├── test_cli.py                     # CLI commands with CliRunner
│   ├── test_cli_orchestration.py       # Orchestration config matrix
│   ├── test_config.py                  # Config loading + merging
│   ├── test_cost.py                    # Cost/token tracking
│   ├── test_events.py                  # Event bus pub/sub
│   ├── test_llm_anthropic.py           # Anthropic adapter (mocked HTTP)
│   ├── test_llm_base.py               # LLM base classes
│   ├── test_llm_gemini.py             # Gemini adapter (mocked HTTP)
│   ├── test_llm_openai.py             # OpenAI adapter (mocked HTTP)
│   ├── test_observability.py           # Structured logging/tracing
│   ├── test_profiles.py               # Profile loading + validation
│   ├── test_queue.py                   # In-memory task queue
│   ├── test_redis_queue.py             # Redis task queue (mocked)
│   ├── test_sandbox.py                 # Docker sandbox with mocked Docker
│   ├── test_sandbox_factory.py         # Sandbox factory (auto/docker/bwrap)
│   ├── test_service_app.py             # Hosted service API
│   ├── test_service_client.py          # PoA client harness
│   ├── test_service_report_finalization.py # Report finalization logic
│   ├── test_state_machine.py           # State transitions
│   ├── test_tool_plugins.py            # Entry point plugin discovery
│   ├── test_tools.py                   # Built-in tool logic with mocked sandbox
│   ├── test_tools_extra.py             # Additional tool edge cases
│   ├── test_tools_git.py              # Git tools (diff, commit, branch, PR)
│   └── test_worker.py                  # Worker dequeue + execution
├── integration/                        # Require Docker or system deps
│   ├── test_bwrap_sandbox_integration.py  # Bwrap in real Linux namespace
│   ├── test_redis_queue_integration.py    # Real Redis interactions
│   └── test_tools_integration.py          # Tools in real Docker sandbox
└── e2e/                                # Require Docker + API key
    ├── test_agent_e2e.py               # Full agent pipeline
    ├── test_cli_e2e.py                 # CLI with real LLM
    └── test_pipeline_e2e.py            # Pipeline with mocked LLM (VCR)
```

## Shared Fixtures (`conftest.py`)

| Fixture            | Type           | Description                            |
| ------------------ | -------------- | -------------------------------------- |
| `sample_repo_path` | `str`          | Path to `tests/fixtures/sample_repo/`  |
| `agent_config`     | `AgentConfig`  | Config with sensible test defaults     |
| `agent_run`        | `AgentRun`     | Fresh run using tmp_path workspace     |
| `mock_llm`         | `LLMProvider`  | AsyncMock that returns a stop response |
| `tool_registry`    | `ToolRegistry` | Registry with all built-in tools       |

## Writing New Tests

### Unit Test Pattern

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_my_feature(mock_llm, tool_registry, agent_run):
    """Describe what this test validates."""
    # Arrange
    mock_llm.complete.return_value = ...

    # Act
    result = await some_function(agent_run, mock_llm, tool_registry)

    # Assert
    assert result.state == RunState.COMPLETED
```

### Plugin Test Pattern

Plugin tests follow the same conventions but import from the plugin package:

```python
from plugins.proof_of_audit.challenge import compare_reports
from plugins.proof_of_audit.models import ChallengeEvidence

def test_comparison_detects_missed_finding():
    evidence = compare_reports(original_report, challenger_report)
    assert len(evidence.missed_findings) == 1
```

Plugin tests live in `tests/unit/` alongside core tests, prefixed with
`test_<plugin_feature>.py`.

### E2E Test Pattern

```python
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_agent_does_thing(llm, tools, sandbox, workspace):
    run = _make_run("Task description", workspace)
    sandbox.start(str(workspace))
    try:
        result = await react_loop(run, llm, tools, sandbox)
    finally:
        sandbox.stop()
        await llm.close()

    assert result.state in (RunState.COMPLETED, RunState.TIMEOUT)
```

## CI Workflows

### Automated (`ci.yml`)

Runs on every push/PR to `main`:

1. **Lint** — `ruff check` + `mypy`
2. **Code Quality** — additional static analysis
3. **Test** — Unit tests on Python 3.11, 3.12, 3.13
4. **Integration** — Tools in Docker sandbox

### Manual (`e2e-tests.yml`)

Triggered manually via GitHub Actions UI:

- Restricted to repository owner (`github.actor == 'akoita'`)
- Uses `secrets.GEMINI_API_KEY`
- Builds sandbox Docker image
- Runs all e2e tests

## Coverage

Target: **90%+** line coverage on `agent_forge/` package.

```bash
pytest --cov=agent_forge --cov-report=term-missing --cov-report=html
open htmlcov/index.html
```
