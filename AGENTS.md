# Agent Forge — AI Agent Coding Standards

> This file is read by AI coding assistants (GitHub Copilot, Gemini Code Assist, Claude, etc.)
> to enforce project-wide conventions. Keep it up to date.

## 🚨 No Hardcoded Configuration Values

**NEVER hardcode** URLs, ports, secrets, API keys, or any
environment-dependent values directly in source code.

### Rules

1. **Always use environment variables** with a sensible local-dev fallback:

   ```python
   # ✅ CORRECT
   api_key = os.environ.get("GEMINI_API_KEY")

   # ❌ WRONG — hardcoded key
   api_key = "AIzaSy..."

   # ❌ WRONG — hardcoded URL
   redis_url = "redis://production-host:6379/0"
   ```

2. **Use the configuration system** — don't redeclare config in every file:

   ```python
   # ✅ Import from the canonical source
   from agent_forge.config import load_config
   config = load_config()

   # ❌ Don't redeclare per-file
   REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
   ```

3. **Never commit secrets** — API keys, tokens, and credentials must come from
   environment variables, never from source. The `.env` file is in `.gitignore`.

### Environment Variable Naming

| Prefix              | Purpose                              | Example                            |
| ------------------- | ------------------------------------ | ---------------------------------- |
| `AGENT_FORGE_`      | Application configuration            | `AGENT_FORGE_AGENT_MAX_ITERATIONS` |
| `GEMINI_API_KEY`    | LLM provider key (direct, no prefix) | `GEMINI_API_KEY`                   |
| `OPENAI_API_KEY`    | LLM provider key (direct, no prefix) | `OPENAI_API_KEY`                   |
| `ANTHROPIC_API_KEY` | LLM provider key (direct, no prefix) | `ANTHROPIC_API_KEY`                |

### Required Environment Variables

Document any new env var in `docs/spec.md § Configuration` and the project's `agent-forge.toml` defaults.

---

## 🚨 Git Workflow — Branch & PR Only

**NEVER push directly to `main`.** All changes must go through a feature branch and Pull Request.

### Rules

1. **Always work on a branch** — use the naming conventions:
   - `feat/<issue-number>-<short-description>` for features
   - `fix/<issue-number>-<short-description>` for bug fixes
   - `docs/<issue-number>-<short-description>` for documentation
   - `refactor/<short-description>` for refactoring
   - `test/<short-description>` for test additions

2. **Submit a Pull Request** targeting `main` — include a clear description and reference the issue (`Closes #N`).

3. **Merge only on explicit developer request** — never merge a PR autonomously. Wait for the developer to say "merge", "you can merge", or equivalent.

4. **Never force-push to `main`** — only force-push on feature branches if absolutely necessary.

5. **Clean up after merge** — delete the feature branch (local + remote) and align local `main`.

6. **Use the `/start-issue` workflow** when beginning work on any issue or task. Run the steps in `.agent/workflows/start-issue.md`.

7. **Automatically run `/finish-issue` when completing work.** When work on any issue or task is done, **always execute every step** in `.agents/workflows/finish-issue.md` — verify coverage, run tests, lint, commit, push, open PR, wait for CI green, and merge. This workflow is mandatory, not optional. Do not skip steps or ask whether to run it.

---

## Architecture Conventions

### Python

- **Python 3.11+** — use modern syntax: `X | Y` unions, `match` statements, `tomllib`
- **Async-first** — use `async/await` for I/O-bound operations (LLM calls, Docker, file I/O)
- **ABCs for interfaces** — all providers and tools implement abstract base classes
- **Pydantic for validation** — use Pydantic models for external data (config, API responses)
- **Dataclasses for internals** — use `@dataclass` for internal data structures

### Module Layout

| Package                     | Purpose                                      |
| --------------------------- | -------------------------------------------- |
| `agent_forge.llm`           | LLM provider adapters (Gemini, OpenAI, etc.) |
| `agent_forge.tools`         | Built-in tools (file ops, shell, search)     |
| `agent_forge.sandbox`       | Docker sandbox management                    |
| `agent_forge.agent`         | ReAct loop, state machine, prompts           |
| `agent_forge.orchestration` | Task queue, event bus, workers               |
| `agent_forge.observability` | Structured logging, tracing, cost tracking   |

### Docker / Sandbox

- Sandbox containers use `--network none` by default
- Never pass API keys into the sandbox
- Resource limits are mandatory: `--cpus`, `--memory`, `--pids-limit`
- All file operations are validated to stay within `/workspace`

---

## 🚨 Domain-Agnostic Core — Extension-First Architecture

Agent Forge is a **generic coding agent framework** — comparable to Claude Code, Codex, or Antigravity.
It must remain **domain-agnostic**. Any feature tied to a specific use case (smart contract auditing,
web security scanning, code migration, etc.) belongs in the **extension layer**, never in the core packages.

### The Boundary

```
┌──────────────────────────────────────────────────────────────┐
│  CORE  (agent_forge/*)                                       │
│  Generic, domain-agnostic capabilities:                      │
│  LLM adapters, ReAct loop, sandbox, tools, profiles,         │
│  orchestration, observability, CLI, hosted service shell      │
├──────────────────────────────────────────────────────────────┤
│  EXTENSION LAYER  (plugins/, skills/, workflows/)            │
│  Domain-specific capabilities loaded at runtime:             │
│  - plugins/proof-of-audit/  → audit profiles, detectors,    │
│    report schemas, challenge evidence, multi-agent personas  │
│  - plugins/<other-domain>/  → any future specialization      │
│  - --profiles-dir, entry_points, skill files, workflows      │
└──────────────────────────────────────────────────────────────┘
```

### Rules

1. **Core packages must not import or reference domain-specific concepts.**
   Terms like "reentrancy", "access control", "vulnerability", "finding", "severity",
   "detector" are audit-domain vocabulary — they do not belong in `agent_forge.*`.

2. **Use generic abstractions in core.** A profile has `prompt_scope` (generic),
   not `detectors` (audit-specific). A report is a JSON artifact, not a
   "proof-of-audit report".

3. **Domain features are delivered via extensions:**
   - **Profiles** → YAML files in a plugin's `profiles/` directory, loaded with `--profiles-dir`
   - **Tools** → Python entry points registered under `agent_forge.tools`
   - **Prompts** → Injected through the generic `prompt_scope` field on `AgentProfile`
   - **Workflows** → Markdown files in `.agent/workflows/`

4. **Test accordingly.** Core tests must not depend on any domain-specific profile
   or plugin existing. Domain tests live alongside the plugin.

### Example: Adding a New Domain

To add a "web-security-scanner" domain, create `plugins/web-security-scanner/` with its own
profiles, tools, and workflows. **Do not modify any file under `agent_forge/`** to add
web-security concepts.

### Distribution Model

Extensions can be **separate installable packages** — they do not need to live in
this monorepo. A user installs the core agent and then adds domain capabilities:

```bash
pip install agent-forge                        # core framework
pip install agent-forge-proof-of-audit         # audit profiles, tools, report schemas
pip install agent-forge-web-security           # hypothetical web-security extension
```

The `plugins/` directory in this repo is a **development convenience** for first-party
extensions. At runtime, extensions are discovered through:

- **`entry_points`** — Python's standard plugin mechanism (already used for tools
  via the `agent_forge.tools` group in `tools/plugins.py`).
  Future groups: `agent_forge.profiles`, `agent_forge.prompts`.
- **`--profiles-dir`** — CLI flag pointing to a directory of profile YAMLs.
- **Config** — `agent-forge.toml` can declare extension paths.

---

## 🧪 Testing Standards

### File Naming

| Pattern                       | Purpose                                      | Runner                  |
| ----------------------------- | -------------------------------------------- | ----------------------- |
| `tests/unit/test_*.py`        | Pure unit tests — no Docker, no external I/O | `make test-unit`        |
| `tests/integration/test_*.py` | Tests with real Docker containers            | `make test-integration` |
| `tests/e2e/test_*.py`         | Full agent run on sample repos               | `make test` (all)       |

### Rules

1. **Mock LLM responses, not tools.** Tools should be tested against a real sandbox when possible. Use recorded/cached LLM responses (VCR pattern) for deterministic tests.

2. **Use `pytest` fixtures** for sandbox setup/teardown:

   ```python
   # ✅ CORRECT — use fixture
   @pytest.fixture
   async def sandbox():
       sb = DockerSandbox()
       await sb.start("./tests/fixtures/sample_repo", SandboxConfig())
       yield sb
       await sb.stop()

   # ❌ WRONG — manual setup in test body
   ```

3. **Never mock the sandbox in integration tests.** Integration tests exist to verify real Docker interactions.

4. **Use `respx` for HTTP mocking** in LLM adapter unit tests:

   ```python
   # ✅ CORRECT — mock HTTP, not the adapter
   respx.post("https://generativelanguage.googleapis.com/...").respond(json={...})
   ```

### Running Tests

```bash
# Unit tests only (fast, no Docker needed)
make test-unit

# Integration tests (requires Docker)
make test-integration

# All tests with coverage
make test
```

---

## Code Quality

- Run `make lint` before committing (ruff check + mypy)
- Run `make format` to auto-format (ruff format)
- All public functions and methods must have **type hints**
- Use **Google-style docstrings** for public APIs
- Follow [Conventional Commits](https://www.conventionalcommits.org/) for all commit messages
