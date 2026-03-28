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
