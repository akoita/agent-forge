# Extending Agent Forge

> How to add new tools, LLM providers, agent profiles, and domain-specific extensions.

## Adding a New Tool

### 1. Implement the `Tool` ABC

Create a new file in `agent_forge/tools/`:

```python
# agent_forge/tools/my_tool.py
from agent_forge.tools.base import Tool, ToolResult

class MyTool(Tool):
    @property
    def name(self) -> str:
        return "my_tool"

    @property
    def description(self) -> str:
        return "Description shown to the LLM."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "What the tool takes as input",
                },
            },
            "required": ["input"],
        }

    async def execute(self, arguments: dict, sandbox) -> ToolResult:
        input_val = arguments.get("input", "")

        # Run something in the sandbox
        result = sandbox.exec(["echo", input_val])

        return ToolResult(
            output=result.stdout,
            error=result.stderr or None,
            exit_code=result.exit_code,
            execution_time_ms=0,
        )
```

### 2. Register in the Default Registry

```python
# agent_forge/tools/__init__.py
from agent_forge.tools.my_tool import MyTool

def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    # ... existing tools ...
    registry.register(MyTool())
    return registry
```

### 3. Add Tests

```python
# tests/unit/test_my_tool.py
@pytest.mark.asyncio
async def test_my_tool_basic():
    tool = MyTool()
    sandbox = AsyncMock()
    sandbox.exec.return_value = ExecResult(stdout="hello", stderr="", exit_code=0)

    result = await tool.execute({"input": "hello"}, sandbox)
    assert result.output == "hello"
    assert result.exit_code == 0
```

### Key Rules for Tools

- `name` must be unique across the registry
- `parameters` must be valid JSON Schema (the LLM uses this)
- `description` should be clear — the LLM reads it to decide when to use the tool
- Always return a `ToolResult` (never raise from `execute`)

## Shipping a Tool Plugin

Agent Forge can load external tool plugins automatically from Python entry
points in the `agent_forge.tools` group.

### 1. Package the Tool

Create a normal Python package that depends on `agent-forge` and expose either:

- a `Tool` subclass with a zero-argument constructor, or
- a pre-instantiated `Tool` object

### 2. Declare the Entry Point

```toml
[project.entry-points."agent_forge.tools"]
my_tool = "my_package.tool:MyTool"
```

### 3. Install the Plugin

```bash
pip install my-package
```

or during development:

```bash
pip install -e ./path/to/plugin
```

After installation, `create_default_registry()` loads the plugin automatically
for the CLI and hosted service.

### Plugin Validation Rules

- entry points must resolve to a `Tool` instance or a `Tool` subclass
- subclasses must be instantiable without constructor arguments
- plugin tool names must remain unique across built-ins and other plugins
- plugin load failures are raised immediately with the entry point name/value

### Example Package

See [`examples/echo-tool-plugin`](/home/koita/dev/ai/agent-forge/examples/echo-tool-plugin)
for a minimal working plugin package.

---

## Adding a New LLM Provider

### 1. Implement the `LLMProvider` ABC

```python
# agent_forge/llm/my_provider.py
from agent_forge.llm.base import LLMProvider, LLMResponse, Message, LLMConfig

class MyProvider(LLMProvider):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        # Convert messages to provider format
        # Make API call
        # Parse response into LLMResponse
        ...

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        # Streaming variant
        ...
```

### 2. Register in the Factory

Add the provider to `agent_forge/llm/factory.py`:

```python
from agent_forge.llm.my_provider import MyProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "my_provider": MyProvider,  # ← add here
}
```

The factory function `create_provider(name, api_key)` will then instantiate
your adapter by name.

### 3. Add Provider Config

In `agent-forge.toml`, add defaults:

```toml
[providers.my_provider]
api_key_env = "MY_PROVIDER_API_KEY"
default_model = "my-model-v1"
```

### Key Rules for Providers

- Must handle tool call mapping (your provider's format ↔ `ToolCall`)
- Must populate `TokenUsage` in responses for cost tracking
- Should implement retry with exponential backoff for transient errors
- Should raise `LLMAuthError`, `LLMRateLimitError`, `LLMTimeoutError` for known error classes

---

## Creating Agent Profiles

Agent profiles configure the agent's persona and behavior for specific task
types. They are defined as YAML files and loaded at startup.

### Profile Schema

```yaml
# my-profile.yaml
id: "my-profile-v1"
name: "My Custom Profile"
description: "Specialized for X tasks"
prompt_scope: |
  You are an expert at X. Focus on Y and Z.
  Always check for A before proceeding.
llm_provider: "gemini"          # optional override
llm_model: "gemini-3.1-flash"   # optional override
max_iterations: 30              # optional override
```

| Field           | Type          | Description                                    |
| --------------- | ------------- | ---------------------------------------------- |
| `id`            | `str`         | Unique identifier (used in `--profile` flag)   |
| `name`          | `str`         | Human-readable name                            |
| `description`   | `str`         | What this profile is optimized for             |
| `prompt_scope`  | `str`         | Injected into the system prompt                |
| `llm_provider`  | `str \| None` | Override the default LLM provider              |
| `llm_model`     | `str \| None` | Override the default model                     |
| `max_iterations`| `int \| None` | Override the maximum iteration count           |

### Built-in Profiles

Agent Forge ships with three built-in profiles in `agent_forge/profiles/builtins/`:

- **`gemini`** — Default Gemini-based profile
- **`openai`** — OpenAI-based profile
- **`thorough`** — Higher iteration limit for complex tasks

### Loading Custom Profiles

Place YAML files in a directory and point Agent Forge to it:

```bash
agent-forge run \
  --task "..." \
  --repo ./project \
  --profiles-dir ./my-profiles/
```

Profiles from `--profiles-dir` override built-ins on duplicate `id`.

### Plugin Profiles

Domain-specific profiles can be discovered from installed extensions via the
`agent_forge.profiles` entry-point group (see [Extension SDK](#extension-sdk)
below), or specified manually with `--profiles-dir`:

```bash
agent-forge run --profiles-dir ./my-profiles --profile my-custom --task "..."
```

Profile precedence (later wins on duplicate `id`):

1. Built-in profiles (`agent_forge/profiles/builtins/`)
2. Extension profiles (`agent_forge.profiles` entry-point group)
3. User-provided directories (`--profiles-dir`)

---

## Extension SDK

Agent Forge includes a scaffolding CLI and auto-discovery system for building
and installing domain-specific extensions as separate Python packages.

### Quick Start

```bash
# Scaffold a new extension project
agent-forge init-extension my-security-scanner

# Develop and install locally
cd my-security-scanner
pip install -e '.[dev]'
pytest

# Verify it's discovered
agent-forge extensions list
```

### Scaffold Structure

Running `agent-forge init-extension my-security-scanner` creates:

```
my-security-scanner/
├── pyproject.toml                        # entry_points pre-configured
├── README.md
├── my_security_scanner/
│   ├── __init__.py                       # ExtensionInfo + PROFILES_DIR
│   ├── profiles/
│   │   └── default.yaml                  # Sample agent profile
│   └── tools/
│       ├── __init__.py
│       └── sample_tool.py                # Sample Tool subclass
└── tests/
    └── test_sample_tool.py               # Sample test
```

### Entry-Point Groups

Extensions register themselves via three Python entry-point groups in
`pyproject.toml`:

| Group                     | Purpose                          | Resolves To       |
| ------------------------- | -------------------------------- | ------------------ |
| `agent_forge.extensions`  | Extension metadata               | `ExtensionInfo`    |
| `agent_forge.profiles`    | Profile directories              | `pathlib.Path`     |
| `agent_forge.tools`       | Tool plugins                     | `Tool` subclass    |

Example `pyproject.toml`:

```toml
[project.entry-points."agent_forge.extensions"]
my-security-scanner = "my_security_scanner:extension_info"

[project.entry-points."agent_forge.profiles"]
my-security-scanner = "my_security_scanner:PROFILES_DIR"

[project.entry-points."agent_forge.tools"]
my_scanner_tool = "my_security_scanner.tools.scanner:ScannerTool"
```

### Managing Extensions

```bash
# List all installed extensions, profiles, and tools
agent-forge extensions list
```

This displays a Rich table showing each extension's name, version, profiles,
and tools.

### Building Domain Extensions

Agent Forge follows a **domain-agnostic core** design. All domain-specific
functionality belongs in the **extension layer**, never in `agent_forge/`.

#### Extension Rules

1. **Core packages must not import domain-specific concepts.** Terms like
   "reentrancy", "vulnerability", "detector" belong in extension code only.
2. **Use generic abstractions in core.** A profile has `prompt_scope`
   (generic), not `detectors` (audit-specific).
3. **Domain features are delivered via extensions** — profiles, tools,
   prompts, and workflows.
4. **Tests for extensions** live in the extension package.

#### Distribution Model

Extensions are **separate installable packages**:

```bash
pip install agent-forge                        # core framework
pip install agent-forge-proof-of-audit         # audit profiles, tools
pip install agent-forge-web-security           # hypothetical extension
```

Runtime discovery uses Python's standard `entry_points` mechanism — no
configuration needed. Install the extension and it's automatically available.

---

## Custom Sandbox Configuration

### Sandbox Backends

Agent Forge supports multiple sandbox backends:

| Backend | Description | Requires |
|---------|-------------|----------|
| `docker` | Full Docker container isolation | Docker daemon |
| `bwrap` | Linux bubblewrap (lightweight, no daemon) | Linux + `bubblewrap` package |
| `auto` | Prefer Docker, fall back to bwrap | Either |

Set the backend in `agent-forge.toml`:

```toml
[sandbox]
backend = "auto"   # "docker", "bwrap", or "auto"
```

### Dockerfile

The Docker sandbox image is defined in `agent_forge/sandbox/Dockerfile`. To add dependencies:

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ripgrep tree curl jq build-essential \
    # Add your packages here
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir ruff pytest mypy
# Add your pip packages here

WORKDIR /workspace
RUN useradd -m -s /bin/bash agent && chown agent:agent /workspace
USER agent
CMD ["sleep", "infinity"]
```

Rebuild after changes:

```bash
make build-sandbox
```

### Runtime Settings

Configure in `agent-forge.toml`:

```toml
[sandbox]
backend = "auto"
image = "my-custom-sandbox:latest"  # Custom image
cpu_limit = 2.0                      # CPU cores
memory_limit = "1g"                  # RAM limit
timeout_seconds = 600                # Container timeout
network_enabled = true               # Allow network (for pip install, etc.)
```

### Per-Project Sandbox

Different projects can use different sandbox configs by placing `agent-forge.toml` in the project root:

```toml
# In a Node.js project
[sandbox]
image = "agent-forge-node:latest"
network_enabled = true
```

For the full interface contracts, see [spec.md](spec.md) § 4.
