# Agent Forge — AI Agent Coding Standards

> This file is read by every AI coding assistant working in this repo
> (Claude Code, Codex CLI, Gemini Code Assist, GitHub Copilot, etc.).
> `CLAUDE.md` is a symlink to this file — there is one source of truth.
> Keep it concise and current. Tool-specific configuration lives in
> `.codex/config.toml` and `.claude/settings.json`.

## 1. Mission & direction

Agent Forge is a **small, measurable, high-correctness coding harness** — not a
broad agent platform. Every change should make the harness more correct or
better measured, not merely broader.

- **Product references:** [vision](docs/vision.md) and [strategy](docs/strategy.md).
- **Canonical roadmap:** `docs/roadmap.md`
- **Pivot decision:** `docs/adr/002-harness-first-reset.md`

For a substantive change, link the vision or strategy objective and its
verification evidence in the issue or PR. Update the reference when an
accepted decision changes direction. Use an RFC to compare options for
significant changes and an ADR in `docs/adr/` to record accepted architectural
decisions. Follow the [reference maintenance guide](docs/strategy.md#maintain-the-references).

**Quality gates for every substantive change** (state each in the PR body):

1. **Evidence** — benchmark/eval results, or an explicit `N/A` with a reason.
2. **Boundary check green** — `python scripts/check_boundaries.py` passes.
3. **ADR** — architecture-affecting decisions get an ADR in `docs/adr/`.
4. **Docs in the same branch** — behavior changes ship with their doc updates.

---

## 2. Authority boundaries — git workflow

This section is the single source of truth for git authority. There is exactly
one merge policy, stated once below.

- **Never push to `main`.** All work happens on a feature branch. Naming:
  - `feat/<issue-number>-<short-description>` — features
  - `fix/<issue-number>-<short-description>` — bug fixes
  - `docs/<issue-number>-<short-description>` — documentation
  - `refactor/<short-description>` — refactoring
  - `test/<short-description>` — test additions
- **Commit and push autonomously on a feature branch.** No per-commit approval
  is needed. Use [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat(#N): ...`, `fix(#N): ...`) and keep commits atomic.
- **Open PRs autonomously**, targeting `main`, referencing the issue
  (`Closes #N`). The PR body follows the template: evidence, benchmark impact,
  boundary analysis, rollback.
- **MERGE GATE:** merging requires **CI green AND developer approval**. A
  developer's *standing approval* (pre-authorizing merges for a task or
  session) satisfies the approval half — record that standing approval in the
  PR. Absent standing approval, request review and stop; do not merge.
- **Never force-push `main`.** Force-push feature branches only when necessary.
- **Use git worktrees for parallel tasks** so concurrent branches stay isolated.
- **Clean up after merge** — delete the merged feature branch (local + remote)
  and realign local `main`.

Use the workflows in `.agents/workflows/start-issue.md` and
`.agents/workflows/finish-issue.md`.

---

## 3. No hardcoded configuration

**Never hardcode** URLs, ports, secrets, API keys, or environment-dependent
values in source.

- **Use environment variables** with a sensible local-dev fallback
  (`os.environ.get("GEMINI_API_KEY")`), never a literal key or URL.
- **Load config through `agent_forge.config`** (`load_config()`) — do not
  redeclare config per file.
- **Never commit secrets.** Keys, tokens, and credentials come from the
  environment. `.env` is gitignored.
- **Document new env vars** in `docs/spec.md § Configuration` and the
  `agent-forge.toml` defaults.

| Prefix              | Purpose                              | Example                            |
| ------------------- | ------------------------------------ | ---------------------------------- |
| `AGENT_FORGE_`      | Application configuration            | `AGENT_FORGE_AGENT_MAX_ITERATIONS` |
| `GEMINI_API_KEY`    | LLM provider key (direct, no prefix) | `GEMINI_API_KEY`                   |
| `OPENAI_API_KEY`    | LLM provider key (direct, no prefix) | `OPENAI_API_KEY`                   |
| `ANTHROPIC_API_KEY` | LLM provider key (direct, no prefix) | `ANTHROPIC_API_KEY`                |

---

## 4. Domain-agnostic core — extension-first

Agent Forge's core is a **generic coding harness**. Anything tied to a specific
use case (smart-contract auditing, web-security scanning, migration, etc.)
lives in the extension layer, never in `agent_forge/*`.

```
CORE  (agent_forge/*)        Generic: LLM adapters, ReAct loop, sandbox,
                             tools, profiles, orchestration, observability,
                             CLI, hosted-service shell.
EXTENSION LAYER              Domain features loaded at runtime:
(plugins/, skills/,          plugins/proof-of-audit/ (audit profiles,
 workflows/)                 detectors, report schemas), other domains,
                             --profiles-dir, entry_points, prompt_scope.
```

**Rules**

1. **No domain vocabulary in `agent_forge/*`.** Terms like `audit`, `solidity`,
   `severity`, `finding`, `detector`, `reentrancy`, `vulnerability` are
   audit-domain vocabulary and must not appear in core packages.
2. **Use generic abstractions in core.** A profile carries `prompt_scope`
   (generic), not `detectors`. A report is a JSON artifact, not a
   "proof-of-audit report".
3. **Deliver domain features via extensions** — profiles as YAML under a
   plugin's `profiles/` (`--profiles-dir`), tools via `agent_forge.tools` entry
   points, prompts through the generic `prompt_scope` field, workflows as
   markdown under `.agents/workflows/`.
4. **Test accordingly.** Core tests must not depend on any domain profile or
   plugin existing; domain tests live with the plugin.

**Enforcement is mechanical.** `scripts/check_boundaries.py` runs in CI.
`scripts/boundary_baseline.txt` lists the historical violations being extracted
(issue #140) — it may only **shrink**, never grow. To mark a deliberate false
positive, add `# boundary-ok` on that line.

---

## 5. Architecture conventions

**Python**

- **Python 3.11+** — modern syntax: `X | Y` unions, `match`, `tomllib`.
- **Async-first** — `async/await` for I/O (LLM calls, Docker, file I/O).
- **ABCs for interfaces** — providers and tools implement abstract base classes.
- **Pydantic for external data** (config, API responses); `@dataclass` for
  internal structures.

| Package                     | Purpose                                      |
| --------------------------- | -------------------------------------------- |
| `agent_forge.llm`           | LLM provider adapters (Gemini, OpenAI, etc.) |
| `agent_forge.tools`         | Built-in tools (file ops, shell, search)     |
| `agent_forge.sandbox`       | Docker sandbox management                    |
| `agent_forge.agent`         | ReAct loop, state machine, prompts           |
| `agent_forge.orchestration` | Task queue, event bus, workers               |
| `agent_forge.observability` | Structured logging, tracing, cost tracking   |

**Docker / sandbox** — containers run `--network none` by default; never pass
API keys into the sandbox; resource limits (`--cpus`, `--memory`,
`--pids-limit`) are mandatory; all file ops stay within `/workspace`.

---

## 6. Testing standards

| Pattern                       | Purpose                                      | Runner                  |
| ----------------------------- | -------------------------------------------- | ----------------------- |
| `tests/unit/test_*.py`        | Pure unit tests — no Docker, no external I/O | `make test-unit`        |
| `tests/integration/test_*.py` | Tests with real Docker containers            | `make test-integration` |
| `tests/e2e/test_*.py`         | Full agent run on sample repos               | `make test` (all)       |

**Rules**

1. **Mock LLM responses, not tools.** Test tools against a real sandbox where
   possible; use recorded/cached LLM responses (VCR pattern) for determinism.
2. **Use `pytest` fixtures** for sandbox setup/teardown — no manual setup in
   the test body.
3. **Never mock the sandbox in integration tests** — they exist to verify real
   Docker interactions.
4. **Use `respx`** for HTTP mocking in LLM adapter unit tests.
5. **e2e and eval tests must assert outcomes.** A "fix" test asserts the patch
   exists/applies and verification passes. Asserting only exit codes or "the
   agent ran" is not acceptable.

Runners: `make test-unit`, `make test-integration`, `make test` (all + coverage).

---

## 7. Code quality

- Run `make lint` (ruff check + mypy) and `make test-unit` before committing.
- Run `make format` (ruff format) to auto-format.
- All public functions and methods have **type hints**.
- Use **Google-style docstrings** for public APIs.
- Follow [Conventional Commits](https://www.conventionalcommits.org/) for
  all commit messages.

<!-- agent-toolkit:writing-policy -->

## Writing documentation

Write each document for one named reader and one task. Hold it to three tests:

- **Useful:** lead with the answer, result or command. Every section helps the
  reader decide, do or verify something. Cut history, narration and what the
  code already says.
- **Concise:** one point per paragraph, at most four sentences, sentences under
  about 25 words, plain words. Use a list or table for parallel items, and
  restructure prose blocks longer than about 120 words. Source line wrapping is
  not paragraph structure.
- **Complete:** give what the reader needs to finish without asking:
  prerequisites, copy-paste-safe commands, the expected result, and what to do
  when it fails. Link to the canonical source instead of copying it.

Describe the system by concept, not as a changelog, and draw a Mermaid diagram
for each flow, state machine or component map. Split a section past about 150
lines and a page past about 4,000 words. State what is true now, label
proposals and plans, and delete stale content in the same change. When the
tests pull apart, completeness wins in procedures and concision wins in
overviews.

The full guide is the agent-toolkit `document-software` skill
(`references/writing.md`). This section is installed by its
`scripts/install_policy.py`; edit the skill, not this copy.

<!-- /agent-toolkit:writing-policy -->
