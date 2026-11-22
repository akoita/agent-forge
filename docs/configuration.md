# Configuration

> Complete reference for all configuration options.

## Precedence Order

Configuration is resolved in layers (highest priority first):

1. **CLI flags** — `--max-iterations 10`
2. **Environment variables** — `AGENT_FORGE_AGENT_MAX_ITERATIONS=10`
3. **Project config** — `./agent-forge.toml` in your repo root
4. **User config** — `~/.agent-forge/config.toml`
5. **Built-in defaults** — Pydantic model defaults

## Config File Format

Both project and user configs use TOML:

```toml
[agent]
max_iterations = 25
max_tokens_per_run = 200000
default_provider = "gemini"
default_model = "gemini-3.1-flash-lite-preview"
temperature = 0.0
system_prompt_path = ""
approval_mode = "auto"          # "auto", "prompt", or "deny"
budget_action = "timeout"       # "timeout" or "pause"
# max_cost_usd = 1.00           # optional estimated USD limit per run
# max_duration_seconds = 300.0  # optional wall-clock limit per run

[sandbox]
backend = "docker"
image = "agent-forge-sandbox:latest"
cpu_limit = 1.0
memory_limit = "512m"
timeout_seconds = 300
network_enabled = false
writable_cache_mounts = true

[queue]
backend = "memory"              # "memory" or "redis"
redis_url = "redis://localhost:6379/0"
max_concurrent_runs = 4

[logging]
level = "INFO"
format = "text"                 # "text" or "json"
log_file = ""

[service]
host = "127.0.0.1"
port = 8000
root_dir = "~/.agent-forge/service"
healthcheck_path = "/healthz"
auth_enabled = false
api_key_header = "X-Agent-Forge-API-Key"
clients_path = "~/.agent-forge/service/clients.toml"
allow_local_path_sources = false
max_source_size_bytes = 50_000_000

[providers.gemini]
api_key_env = "GEMINI_API_KEY"
default_model = "gemini-3.1-flash-lite-preview"

[providers.openai]
api_key_env = "OPENAI_API_KEY"
default_model = "gpt-4o"

[providers.anthropic]
api_key_env = "ANTHROPIC_API_KEY"
default_model = "claude-sonnet-4-20250514"
```

## Environment Variables

### API Keys

| Variable            | Provider      |
| ------------------- | ------------- |
| `GEMINI_API_KEY`    | Google Gemini |
| `OPENAI_API_KEY`    | OpenAI        |
| `ANTHROPIC_API_KEY` | Anthropic     |

### Config Overrides

Any setting can be overridden via environment variable using the pattern:

```
AGENT_FORGE_{SECTION}_{KEY}
```

Examples:

```bash
export AGENT_FORGE_AGENT_MAX_ITERATIONS=10
export AGENT_FORGE_AGENT_APPROVAL_MODE=prompt
export AGENT_FORGE_SANDBOX_MEMORY_LIMIT=1g
export AGENT_FORGE_SANDBOX_BACKEND=auto
export AGENT_FORGE_SANDBOX_IMAGE=agent-forge-sandbox:full
export AGENT_FORGE_LOGGING_LEVEL=DEBUG
export AGENT_FORGE_SERVICE_AUTH_ENABLED=true
export AGENT_FORGE_SERVICE_PORT=8000
```

For hosted-mode client policy and deployment-specific settings, see the
[Hosted Service Guide](hosted-service.md).
Hosted client registry entries must include `allowed_report_schemas` alongside
`api_key_env`, `allowed_profiles`, and the quota/source policy fields.

## CLI Flags

```bash
agent-forge run \
  --task "Fix the bug in auth.py" \
  --repo ./my-project \
  --provider gemini \
  --model gemini-3.1-flash-lite-preview \
  --max-iterations 25 \
  --max-cost 1.00 \                 # optional USD cap per run
  --max-duration 300 \              # optional wall-clock seconds cap
  --budget-action pause \           # "timeout" or "pause" on budget exceed
  --approval-mode prompt \          # "auto", "prompt", or "deny"
  --sandbox-backend auto \
  --sandbox-image agent-forge-sandbox:full \
  --network \
  --command-timeout 480 \
  --output-format text \            # or "json" for machine output
  --report-file ./artifacts/run-result.json \  # optional JSON file output
  --queue memory \                  # or "redis" (omit for direct mode)
  --redis-url redis://localhost:6379/0 \   # only with --queue redis
  --max-concurrent-runs 4           # worker concurrency limit

agent-forge status <run-id> --output-format json
agent-forge resume <run-id> --max-tokens 400000 --max-cost 2.0
agent-forge cancel <run-id> --reason "Stopped by operator"
agent-forge list
agent-forge config        # Show resolved configuration
```

Machine-readable output is only available in direct mode. Queue-backed runs
still emit human-oriented status until that contract is stabilized.

## Common Setups

### Minimal (Gemini)

```bash
export GEMINI_API_KEY="your-key"
agent-forge run --task "Fix the health endpoint" --repo ./my-app
```

### OpenAI Provider

Direct CLI runs, queued runs, and hosted runs use the same adapter registry
for Gemini, OpenAI, and Anthropic. Configure both the provider and a model
identifier supported by its API. When switching providers in the CLI, set
both `--provider` and `--model` to override the configured defaults.

```toml
# agent-forge.toml
[agent]
default_provider = "openai"
default_model = "gpt-4o"
```

```bash
export OPENAI_API_KEY="sk-..."
agent-forge run --task "Add input validation" --repo ./my-app
```

### Qualify a named model

Give a candidate an alias in the project or user TOML file. Set the model
identifier for an API the selected adapter supports.

```toml
[model_profiles.candidate]
provider = "openai"
model = "gpt-4o"
```

Run these commands from the project directory.

```sh
agent-forge models validate candidate
agent-forge models validate candidate --live
```

The first command checks configuration without contacting a provider. The
optional live command needs the provider's configured API key and can incur
API charges. It sends at most two requests with retries disabled, a 45-second
overall deadline, and a requested maximum of 256 output tokens per request.
The JSON report records usage and any provider-reported model versions. A
failed probe returns a nonzero exit code. Add a committed fixture, a task, and an independent check to grade a coding fix.

```sh
agent-forge models validate candidate --live \
  --fix-repo ./fixture-repo --fix-task "Fix the failing health check" \
  --verify-command "python -m pytest -q"
```

The coding stage runs after a passing tool exchange. It uses the [verified
coding workflow](testing.md#verify-a-coding-run), a six-iteration cap, a
24,000-token run threshold, and a 180-second deadline. A lower configured
iteration or token limit still applies. The report links the run and patch
and records the base revision and check exit code. A failed check gives the
qualification command a nonzero exit code.

Pass `--model-profile candidate` to `agent-forge run` to select the alias's provider
and model together. The alias takes precedence over the other provider and model
selections for that invocation. Other run settings still use normal precedence.

### JSON output

Direct CLI runs with `--output-format json` write one result object to stdout.
Runtime logs and the human summary go to stderr. Use `--report-file` to save the
same result object to disk.

### Sandbox Tuning

```toml
[sandbox]
backend = "auto"
image = "agent-forge-sandbox:full"
memory_limit = "1g"
timeout_seconds = 600
network_enabled = true   # Allow network access (e.g. pip install)
cpu_limit = 2.0
writable_cache_mounts = true  # Mount /cache for npm/pip/pnpm/yarn caches

### Sandbox Images

- `agent-forge-sandbox:latest`: Python-focused base image
- `agent-forge-sandbox:node`: Node-focused image with `node`, `npm`, `pnpm`, and `yarn`
- `agent-forge-sandbox:full`: Python + Node image for mixed-language repos

Build them with:

```bash
./scripts/build-sandbox.sh python
./scripts/build-sandbox.sh node
./scripts/build-sandbox.sh full
```

### Sandbox Backends

- `docker`: use the Docker daemon-backed sandbox
- `bwrap`: use Linux bubblewrap directly
- `auto`: prefer Docker and fall back to bubblewrap when Docker is unavailable
```

## Inspecting Config

```bash
agent-forge config
```

This displays the fully resolved configuration as syntax-highlighted JSON, showing which values came from defaults vs overrides.
