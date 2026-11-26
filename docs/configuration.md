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
execution_mode = "tool_calling" # "tool_calling" or opt-in "code_mode"
post_edit_diagnostics = false   # run lightweight post-edit syntax/compiler checks
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
worktree_isolation = true       # isolate parallel queued runs in git worktrees

[subagents]
enabled = false                 # opt-in spawn_subagent delegation (or --subagents)
max_depth = 1                   # recursion guard (1..3)
max_subagents_per_run = 8       # per-run delegation budget (1..32)
max_iterations = 10             # per-child step cap (1..50)
max_tokens = 60000              # per-child token cap
max_result_chars = 8000         # bounded parent tool response length
branch_prefix = "agent-forge/subagent"
worktree_root = ""              # empty defaults to ~/.agent-forge/worktrees/<repo>
worktree_branch_prefix = "agent-forge/run"
auto_remove_unchanged_worktrees = true
merge_strategy = "merge"        # "merge" or "rebase" for merge-time integration

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

[providers.openai-compatible]
base_url = ""
api_key_env = ""
default_model = ""
api_mode = "chat_completions"
api_key_required = false

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
default_model = "anthropic/claude-sonnet-4-6"
api_mode = "chat_completions"
fallback_models = []
# Optional: allow_fallbacks, require_parameters, data_collection ("allow"|"deny"), zdr,
#           provider_order, only_providers, ignore_providers, provider_sort, http_referer, app_title
```

## Environment Variables

### API Keys

| Variable            | Provider      |
| ------------------- | ------------- |
| `GEMINI_API_KEY`    | Google Gemini |
| `OPENAI_API_KEY`    | OpenAI        |
| `ANTHROPIC_API_KEY` | Anthropic     |
| `OPENROUTER_API_KEY` | OpenRouter gateway |

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
  --execution-mode code_mode \      # "tool_calling" (default) or "code_mode"
  --diagnostics \                   # run fast syntax/compiler checks after edits
  --subagents \                     # enable bounded spawn_subagent delegation
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
agent-forge worktrees list --repo ./my-project
agent-forge worktrees integrate <run-id-or-branch> --repo ./my-project --strategy merge
agent-forge worktrees cleanup --repo ./my-project
agent-forge memory list --repo ./my-project
agent-forge memory add "Run make test-unit before pushing" --category testing --repo ./my-project
agent-forge mcp list
agent-forge mcp inspect --output-format json
agent-forge auth status --output-format json
agent-forge auth login github
agent-forge auth import-gh
agent-forge acp --approval-mode prompt
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

### OpenAI-Compatible Endpoints (Ollama, vLLM, llama.cpp, Gateways)

Use `--provider openai-compatible` with `--base-url` directly from the CLI, or configure a `[model_profiles.<name>]` entry. Localhost endpoints loop through without requiring an API key unless `api_key_required = true` is set:

```toml
# Ollama (localhost, no API key needed)
[model_profiles.local-ollama]
provider = "openai-compatible"
model = "gemma3:12b"
base_url = "http://localhost:11434/v1"

# vLLM with explicit capability declarations
[model_profiles.local-vllm]
provider = "openai-compatible"
model = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
base_url = "http://localhost:8000/v1"
capabilities = { parallel_tool_calls = true, structured_output = true }

# llama.cpp server without native tool calling (uses prompt emulation)
[model_profiles.local-llamacpp]
provider = "openai-compatible"
model = "local-model"
base_url = "http://localhost:8080/v1"
capabilities = { tool_calling = false, structured_output = false }

# Hosted gateway (API key required when api_key_env is set)
[model_profiles.gateway]
provider = "openai-compatible"
model = "deepseek/deepseek-chat"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"

[mcp]
enabled = true

[mcp.servers.filesystem]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
approval_mode = "prompt"
allow_read_only_without_approval = true
timeout_seconds = 30.0
failure_threshold = 3

[mcp.servers.remote-api]
transport = "http"
url = "https://mcp.example.com/mcp"
auth_env = "MCP_API_TOKEN"
allow_tools = ["search_docs", "get_issue"]
deny_tools = ["delete_issue"]
```

### Editor Interop (ACP Server)

Point any Agent Client Protocol (ACP) editor (Zed, Neovim, JetBrains ACP clients) to launch `agent-forge acp` over stdio:

```json
{
  "agent_servers": {
    "Agent Forge": {
      "command": "agent-forge",
      "args": ["acp", "--approval-mode", "prompt"]
    }
  }
}
```

For browser clients (`agent-forge[service]`), run the loopback-default authenticated WebSocket ACP and workspace companion bridge:

```bash
export AGENT_FORGE_ACP_TOKEN="replace-with-strong-token"
agent-forge acp --transport ws --host 127.0.0.1 --port 8765 --workspace .
```

Run directly or probe what a live endpoint supports before benchmarking:

```bash
agent-forge run --task "Fix health check" --repo ./my-app \
  --provider openai-compatible --base-url http://localhost:11434/v1 --model gemma3:12b

agent-forge models capabilities local-ollama --probe --output-format json
```

Provider settings can also be overridden with `AGENT_FORGE_PROVIDERS_{PROVIDER}_{KEY}` (for example `AGENT_FORGE_PROVIDERS_OPENAI_COMPATIBLE_BASE_URL` and `AGENT_FORGE_PROVIDERS_OPENAI_COMPATIBLE_DEFAULT_MODEL`).

### Qualify a named model

Give a candidate an alias in the project or user TOML file. Set the model
identifier for an API the selected adapter supports.

```toml
[model_profiles.candidate]
provider = "openai"
model = "gpt-5.4"
# Optional overrides for OpenAI-compatible, frontier, or custom endpoints:
# base_url = "https://api.together.xyz/v1"
# api_key_env = "TOGETHER_API_KEY"
# temperature = 0.0
# input_cost_per_million = 2.5
# output_cost_per_million = 15.0
# cache_read_cost_per_million = 0.25
# reasoning_effort = "high"      # OpenAI/Anthropic effort level
# thinking_mode = "adaptive"     # "adaptive" | "enabled" | "disabled" (Anthropic)
# thinking_level = "high"        # "minimal" | "low" | "medium" | "high" (Gemini)
# thinking_budget_tokens = 2048  # Explicit thinking token budget
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
and model together (along with optional `base_url`, `api_key_env`, and
`temperature` overrides). The alias takes precedence over the other provider and model
selections for that invocation. Other run settings still use normal precedence.

Run the pinned multi-profile benchmark to compare accessible models on verified
fixes per attempt, median latency, and total cost including failures:

```sh
agent-forge models benchmark \
  --sandbox-backend bwrap \
  --output-json docs/eval/baseline-v0.4.0.json \
  --output-md docs/eval/baseline-v0.4.0.md
```

See the [latest measured baseline](eval/baseline-v0.4.0.md) for current results
and settings.

### JSON output

Direct CLI runs with `--output-format json` write one result object to stdout.
Runtime logs and the human summary go to stderr. Use `--report-file` to save the
same result object to disk.

### Sandbox Tuning

```toml
[sandbox]
backend = "auto"
toolchain = "auto"            # "auto" | "python" | "node" | "go" | "rust" | "jvm" | "full" | "universal"
image = "agent-forge-sandbox:full"
dockerfile = ""               # Optional repo-relative Dockerfile built and cached per content hash
setup_command = ""            # Optional Phase 1 online bootstrap command before offline Phase 2
setup_timeout_seconds = 300   # Timeout cap for Phase 1 setup_command
memory_limit = "1g"
timeout_seconds = 600
network_enabled = true        # Allow network access (e.g. package installs / port publishing)
cpu_limit = 2.0
writable_cache_mounts = true  # Mount /cache for pip/npm/pnpm/yarn/go/cargo/gradle caches
mount_host_toolchains = true  # Mount user toolchain dirs read-only in bwrap
extra_ro_binds = []           # Optional extra read-only toolchain paths (credentials blocked)
allowed_egress_domains = []   # Optional host proxy allowlist (e.g. ["pypi.org", "*.googleapis.com"])
published_ports = ["127.0.0.1:8080:8080"]
```

Repositories with `.devcontainer/devcontainer.json` or `.devcontainer.json` resolve their declared `image` or `build.dockerfile` automatically when `toolchain = "auto"` and default `image` are in effect (with explicit `--sandbox-dockerfile`, `--sandbox-image`, and `--toolchain` taking precedence). Use `--setup-command` (or `sandbox.setup_command`) to run Phase 1 online setup into an ephemeral `/cache` before transitioning to `--network none` for Phase 2 agent turns (`VerificationWorkspace` keeps `setup_command` disabled during final verification).

Any stock upstream image (such as `mcr.microsoft.com/dotnet/sdk:9.0`, `haskell:9.8`, `php:8.4-cli`, `ghcr.io/foundry-rs/foundry`, `gcc:14`, or `alpine`) works directly via `--sandbox-image` without wrapper Dockerfiles (`entrypoint` is overridden, POSIX `sh` fallback handles images without `bash`, offline `HOME=/tmp/home` stays writable, and `search_codebase` falls back to host search or POSIX `grep` when `rg` is absent).

### Sandbox Images & Toolchains

- `agent-forge-sandbox:latest`: Python-focused base image (`python`, default & empty greenfield dirs)
- `agent-forge-sandbox:node`: Node-focused image with `node`, `npm`, `pnpm`, and `yarn`
- `agent-forge-sandbox:go`: Go 1.23 image (`go`, `git`, `ripgrep`)
- `agent-forge-sandbox:rust`: Rust 1.85 image (`rustc`, `cargo`, `rustfmt`, `clippy`)
- `agent-forge-sandbox:jvm`: JDK 21 image (`java`, `javac`, `maven`, `gradle`)
- `agent-forge-sandbox:full`: Python + Node image for mixed-language repos
- `agent-forge-sandbox:universal`: `debian:bookworm-slim` with `build-essential` and `mise` (`mise.toml`, `.mise.toml`, `.tool-versions`)

With `sandbox.toolchain = "auto"` (and default `image = "agent-forge-sandbox:latest"`), `.devcontainer/devcontainer.json` takes priority when present; otherwise top-level markers (`mise.toml`/`.mise.toml`/`.tool-versions`, `go.mod`, `Cargo.toml`, `pom.xml`/`build.gradle*`, `package.json`, `pyproject.toml`) select the matching ecosystem image automatically, falling back to `python` on empty greenfield directories. Override per run with `--toolchain`, `--sandbox-dockerfile`, `--setup-command`, or `--publish-port 8080:3000 --network`.

Build them with:

```bash
./scripts/build-sandbox.sh python
./scripts/build-sandbox.sh node
./scripts/build-sandbox.sh go
./scripts/build-sandbox.sh rust
./scripts/build-sandbox.sh jvm
./scripts/build-sandbox.sh full
./scripts/build-sandbox.sh universal
```

### Sandbox Backends

- `docker`: use the Docker daemon-backed sandbox
- `bwrap`: use Linux bubblewrap directly
- `auto`: prefer Docker and fall back to bubblewrap when Docker is unavailable

## Inspecting Config

```bash
agent-forge config
```

This displays the fully resolved configuration as syntax-highlighted JSON, showing which values came from defaults vs overrides.

## Service Credentials (`agent-forge auth`)

Store service credentials (such as GitHub PATs or MCP bearer tokens) on the host in `~/.agent-forge/credentials.json` (`0600`). Sandboxes never receive these credentials.

```bash
# Authenticate interactively or pipe a token on stdin
agent-forge auth login github
printf '%s\n' "$GITHUB_TOKEN" | agent-forge auth login github --token-stdin

# Import an existing GitHub CLI session explicitly
agent-forge auth import-gh

# Inspect or remove stored host credentials
agent-forge auth status
agent-forge auth status --output-format json
agent-forge auth logout github
```

`git_push`, `create_pr`, and HTTP MCP transports (`auth_env`) resolve environment variables first and fall back to the host credential store when absent.

## Interactive Chat & Session Management (`agent-forge chat` / `session`)

Start an interactive multi-turn ADE terminal session with a warm session-scoped sandbox and resume it across restarts:

```bash
# Start a new interactive session with live streaming and persistent history
agent-forge chat --repo . --idle-timeout 1800

# Disable streaming or customize prompt history / approval mode
agent-forge chat --repo . --no-stream --history-file ~/.agent-forge/chat_history --approval-mode prompt

# Resume the latest session for this repository (or pass a specific session ID)
agent-forge chat --repo . --resume latest

# Inspect, manually compact, or delete saved sessions offline
agent-forge session list --repo .
agent-forge session show <session-id>
agent-forge session compact <session-id> --keep-recent 2
agent-forge session delete <session-id>
```

Inside `agent-forge chat`, use `/diff [--stat]`, `/undo [--force]`, `/subagents`, `/apply <branch> [--strategy merge|rebase]`, `/sessions [--all] [N]`, `/resume <id|latest>`, `/compact [keep_recent]`, `/context`, `/model [spec]`, `/clear [--sandbox]`, `/help`, or `/exit`. Enter multiline prompts using trailing `\` continuation or `"""..."""` blocks.
