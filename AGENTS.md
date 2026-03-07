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
| Prefix              | Purpose                              | Example                          |
| ------------------- | ------------------------------------ | -------------------------------- |
| `AGENT_FORGE_`      | Application configuration            | `AGENT_FORGE_AGENT_MAX_ITERATIONS` |
| `GEMINI_API_KEY`    | LLM provider key (direct, no prefix) | `GEMINI_API_KEY`                 |
| `OPENAI_API_KEY`    | LLM provider key (direct, no prefix) | `OPENAI_API_KEY`                 |
| `ANTHROPIC_API_KEY` | LLM provider key (direct, no prefix) | `ANTHROPIC_API_KEY`              |

### Required Environment Variables
Document any new env var in `spec.md § Configuration` and the project's `agent-forge.toml` defaults.

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

7. **Use the `/finish-issue` workflow** when completing work on an issue. Run the steps in `.agent/workflows/finish-issue.md`.

---

## Architecture Conventions

### Python

- **Python 3.11+** — use modern syntax: `X | Y` unions, `match` statements, `tomllib`
- **Async-first** — use `async/await` for I/O-bound operations (LLM calls, Docker, file I/O)
- **ABCs for interfaces** — all providers and tools implement abstract base classes
- **Pydantic for validation** — use Pydantic models for external data (config, API responses)
- **Dataclasses for internals** — use `@dataclass` for internal data structures

### Module Layout

| Package                  | Purpose                                      |
| ------------------------ | -------------------------------------------- |
| `agent_forge.llm`        | LLM provider adapters (Gemini, OpenAI, etc.) |
| `agent_forge.tools`      | Built-in tools (file ops, shell, search)     |
| `agent_forge.sandbox`    | Docker sandbox management                    |
| `agent_forge.agent`      | ReAct loop, state machine, prompts           |
| `agent_forge.orchestration` | Task queue, event bus, workers            |
| `agent_forge.observability` | Structured logging, tracing, cost tracking |

### Docker / Sandbox

- Sandbox containers use `--network none` by default
- Never pass API keys into the sandbox
- Resource limits are mandatory: `--cpus`, `--memory`, `--pids-limit`
- All file operations are validated to stay within `/workspace`

---

## 🧪 Testing Standards

### File Naming

| Pattern                        | Purpose                                     | Runner                   |
| ------------------------------ | ------------------------------------------- | ------------------------ |
| `tests/unit/test_*.py`         | Pure unit tests — no Docker, no external I/O | `make test-unit`         |
| `tests/integration/test_*.py`  | Tests with real Docker containers            | `make test-integration`  |
| `tests/e2e/test_*.py`          | Full agent run on sample repos               | `make test` (all)        |

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
