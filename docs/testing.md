# Testing Guide

> How to run, write, and organize tests in Agent Forge.

## Test Suites

| Suite           | Command                 | Requirements              | Markers                        |
| --------------- | ----------------------- | ------------------------- | ------------------------------ |
| **Unit**        | `make test-unit`        | None                      | default (no marker)            |
| **Integration** | `make test-integration` | Docker                    | `@pytest.mark.integration`     |
| **E2E**         | `make test-e2e`         | Docker + `GEMINI_API_KEY` | `@pytest.mark.e2e`             |
| **All non-E2E** | `make test`             | Docker                    | excludes `e2e` + `integration` |

## Grade a fixture patch

From the repository root, install the development dependencies and the sample
app requirements, then run the deterministic evaluation.

```sh
python -m pip install -e '.[dev]'
python -m pip install -r tests/fixtures/sample_repo/requirements.txt
make eval
```

The grader applies each curated patch to a fresh copy of the sample app and
runs the original tests from a separate directory. The base, empty patch, and
incorrect patch must fail the health check. The known-good patch must pass the
health check and the two existing regression tests.

`tests/eval/test_benchmark_corpus.py` exercises the 12-task corpus in
`agent_forge.eval.corpus.BUILTIN_TASKS` across two tiers (`smoke` with 4 tasks
for CI and `full` with all 12 tasks). Each task includes visible unit tests and
hidden verifier tests injected only inside the independent verifier checkout:

- Every base checkout, empty patch, and partial `incomplete_files` edit must fail verification.
- Every `known_good_files` fix must pass both visible and hidden verification tests.
- `test_context_engine_reduces_tokens_per_task_on_m0_corpus` verifies that `ContextEngine`
  auto-discovers hierarchical `AGENTS.md` instructions (deduplicating symlinked `CLAUDE.md`)
  and cuts cumulative prompt tokens across the 4-task smoke suite while holding 4/4 fixes.
- `test_openai_responses_adapter_matches_or_beats_chat_completions_on_m0_corpus` verifies that
  `OpenAIProvider` (`api_mode="responses"`) holds the 4/4 smoke fix rate while transmitting
  fewer continuation bytes and lower estimated USD cost than Chat Completions.
- `test_local_ollama_config_only_completes_m0_smoke_task` verifies that a config-only
  `openai-compatible` profile targeting `http://localhost:11434/v1` without API keys
  completes a smoke task (both with native tool calling and with prompt-emulated
  `tool_calling = false`).
- `test_chat_warm_sandbox_compact_and_resume_across_restart` verifies multi-turn `ChatSessionRuntime` warm sandbox reuse, `/compact`, and `--resume latest` restoration across restarts (complemented by unit tests in `tests/unit/test_cli_chat_and_session.py`).
- `python -m agent_forge.eval --suite smoke --report-dir .eval-artifacts --assert-gates`
  runs scripted `oracle`, `noop`, and `incomplete` profiles through the full
  `react_loop`, tool registry, `VerificationWorkspace`, `CompletionContract`, and
  `run-journal-v1` JSONL trajectory capture (replayable via
  `agent-forge replay <trajectory.jsonl>`), asserting 100% oracle pass,
  `verification_failed` (with structured `unmet_clauses`) on `noop`/`incomplete`,
  and 0% false-positive completions.

To generate or refresh a full release report and measure live models against
those pinned tasks, compare against the checked-in
[v0.8.0 paired deterministic baseline](eval/baseline-v0.8.0.md) and
[v0.4.0 live model baseline](eval/baseline-v0.4.0.md):

```sh
python -m agent_forge.eval --suite full --include-paired \
  --output-json docs/eval/baseline-v0.8.0.json \
  --output-md docs/eval/baseline-v0.8.0.md \
  --assert-gates
```

## Verify a coding run

Use the direct CLI with a committed Git repository. Choose a verification
command that covers the requested change. The sandbox image needs the tools
and dependencies for that command before the run begins.

```sh
agent-forge run --task "Fix the health endpoint" --repo ./my-app \
  --verify-command "python -m pytest -q" --report-file ./run-result.json
```

The agent starts from the repository's committed `HEAD`. Existing local
changes stay in the original checkout. The verifier applies the generated
patch to a second checkout and runs the command with network access and
writable dependency caches disabled. A missing patch, a patch application
error, or a failed check produces a nonzero exit status. For an independent
test of correctness, use a check whose assertions remain under your control.

The report includes the base revision, terminal verification reason,
`completion_contract` clause results (with `unmet_clauses` on
`verification_failed`), and check output. When a patch exists, the result
points to `change.patch` in the run directory. To review and apply the patch,
use a clean checkout of the reported base revision and run `git apply --check`
before `git apply`. For a read-only question, omit `--verify-command` or pass
`--contract qa`; pass `--contract coding-fix` to require edits and checks even
without `--verify-command`.

## Resume and replay an interrupted run

Local runs persist an append-only, fsynced step journal (`journal.jsonl`) alongside
atomic checkpoints (`checkpoint.json` and `checkpoint.patch`). To continue a paused,
signal-interrupted, or crashed run without repeating completed side effects:

```sh
agent-forge resume <run-id> --output-format json
```

On resume, `load_checkpoint()` reconciles completed action results and messages from
`journal.jsonl`, checks out the pinned base revision (for verified runs), reapplies the
partial patch inside an isolated workspace, and skips already-committed idempotency keys.

To replay a persisted run or benchmark trajectory and verify transition integrity offline:

```sh
agent-forge replay <run-id-or-jsonl-path> --output-format json
```

## Check queued run outcomes

The queued CLI uses the queued task ID as the persisted run ID. A model run
that fails or times out produces the matching queue status and a failing CLI
exit code. Use `agent-forge status <run-id>` to inspect the saved result.

When running parallel tasks or deterministic eval suites concurrently (`python -m agent_forge.eval --suite smoke --concurrency 4`), each run executes in its own isolated checkout or Git worktree (`WorktreeManager`) so concurrent diffs never cross-contaminate and aborted runs leave no orphan worktrees (`tests/unit/test_worktree_isolation.py`, `tests/eval/test_benchmark_corpus.py`).

External MCP client integration (stdio + Streamable HTTP transports, `ApprovalGate` enforcement with no blanket trust, timeout isolation, and circuit breaking) is covered in `tests/unit/test_mcp_client.py` and `tests/eval/test_benchmark_corpus.py`. Core suites run with zero external MCP servers configured.

ACP server JSON-RPC framing, `StreamEvent` -> `session/update` mapping, bidirectional `session/request_permission` approval handling, cancellation, and `agent-forge acp` stdio isolation are tested in `tests/unit/test_acp_server.py` and `tests/eval/test_benchmark_corpus.py`.

Host service credential storage (`~/.agent-forge/credentials.json`), `agent-forge auth` CLI commands, `CreatePRTool`/`HttpMCPTransport` credential fallbacks, secret scrubbing, and offline-sandbox host-brokered `git_push` (with `ApprovalGate` enforcement) are covered in `tests/unit/test_auth_and_git_push.py` and `tests/eval/test_benchmark_corpus.py`.

Polyglot toolchain detection (`go.mod`, `Cargo.toml`, `pom.xml`/`build.gradle*`, `package.json`, `pyproject.toml`), loopback port publishing validation/forwarding, and greenfield empty-directory verification (`VerificationWorkspace` bootstrap without pre-existing Git commits) are covered in `tests/unit/test_polyglot_sandbox_and_greenfield.py` and `tests/eval/test_benchmark_corpus.py`.

Per-profile pricing overrides (`--max-cost` budget enforcement on unlisted frontier models), generalized long-context heuristics + `ContextEngine` window auto-sizing, and Anthropic/Gemini/OpenAI reasoning payloads are covered in `tests/unit/test_frontier_models_and_reasoning.py` and `tests/eval/test_benchmark_corpus.py`.

Zero-wrapper stock Docker images (overridden entrypoint, `bash`->`sh` fallback, offline `HOME`), `search_codebase` non-ripgrep fallbacks, and `bwrap` read-only host toolchain passthrough with credential-path rejection are covered in `tests/unit/test_stock_images_and_bwrap_toolchains.py` and `tests/eval/test_benchmark_corpus.py`.

Interactive ADE chat sessions (warm sandbox reuse, `/compact`, `/context`, `/model`, `/diff`, `/undo`, `/sessions`, `/resume`, live streaming, prompt history, and multiline input) are covered by unit tests in `tests/unit/test_cli_chat_and_session.py`, `tests/unit/test_chat_streaming_and_history.py`, and `tests/unit/test_chat_workspace_controls.py`, plus the real Docker warm-container integration test in `tests/integration/test_warm_chat_sandbox_integration.py`.

Bounded hierarchical subagent delegation (read-only research fan-out sharing the parent sandbox, isolated Git worktree workers with orphan cleanup, depth/budget guards, token accumulation, and config/CLI flags) is covered in `tests/unit/test_subagent_delegation.py` and `tests/eval/test_benchmark_corpus.py`.

Opt-in Code Mode programmatic execution (`search_tools` progressive discovery, sandboxed Python host RPC bridge, secret isolation, `ApprovalGate` & `RunJournal` replay parity, and paired A/B benchmark evaluation vs classic `tool_calling`) is covered in `tests/unit/test_code_mode.py` and `tests/eval/test_benchmark_corpus.py`. Chat `/subagents`/`/apply`, `[code-mode]` REPL/ACP progress mapping, and real `--network none` Docker verification for Code Mode and worktree subagents live in `tests/unit/test_ade_subagents_and_code_mode_ux.py` and `tests/integration/test_subagents_and_code_mode_integration.py`.

Polyglot tree-sitter symbol extraction, comment/string-stripped repo-map ranking, and the post-edit diagnostics feedback loop are covered in `tests/unit/test_polyglot_code_intel_and_diagnostics.py` and `tests/eval/test_benchmark_corpus.py`.

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
│   ├── test_acp_server.py              # ACP server initialization, streaming, permissions
│   ├── test_acp_stateful_sessions.py   # Stateful multi-turn ACP sessions & disk replay
│   ├── test_agent_core.py              # ReAct loop with mocked LLM
│   ├── test_bwrap_sandbox.py           # Bubblewrap sandbox unit tests
│   ├── test_challenge_evidence.py      # PoA challenge evidence generator (plugin)
│   ├── test_cli.py                     # CLI commands with CliRunner
│   ├── test_cli_orchestration.py       # Orchestration config matrix
│   ├── test_config.py                  # Config loading + merging
│   ├── test_context_engine.py          # Instruction discovery, repo map, working set, compaction
│   ├── test_cost.py                    # Cost/token tracking
│   ├── test_declarative_approvals_and_egress_proxy.py # Declarative tool approvals & egress proxy
│   ├── test_dev_session_and_context_state.py # DevSession persistence & ContextEngine serialization
│   ├── test_devcontainer_and_setup_phase.py # Devcontainers, Dockerfile.universal, setup_command
│   ├── test_events.py                  # Event bus pub/sub
│   ├── test_llm_anthropic.py           # Anthropic adapter (mocked HTTP)
│   ├── test_llm_base.py               # LLM base classes
│   ├── test_llm_gemini.py             # Gemini adapter (mocked HTTP)
│   ├── test_llm_openai.py             # OpenAI adapter (Responses + Chat Completions fallback)
│   ├── test_model_qualification.py    # Model qualification probe & capabilities
│   ├── test_observability.py           # Structured logging/tracing
│   ├── test_openai_compatible.py      # OpenAI-compatible endpoints, probing & fallbacks
│   ├── test_profiles.py               # Profile loading + validation
│   ├── test_provider_v2.py            # Capabilities, v2 adapters, routing, parallel dispatch
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
