# Extending Agent Forge

> How to add new tools, LLM providers, and custom sandbox configurations.

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

### 2. Wire into the CLI

In `agent_forge/cli.py`, add your provider to the factory:

```python
if provider_name == "my_provider":
    from agent_forge.llm.my_provider import MyProvider
    llm = MyProvider(api_key)
```

### 3. Add Provider Config

In `agent_forge/config.py`, add defaults:

```python
"my_provider": ProviderSettings(
    api_key_env="MY_PROVIDER_API_KEY",
    default_model="my-model-v1",
),
```

### Key Rules for Providers

- Must handle tool call mapping (your provider's format ↔ `ToolCall`)
- Must populate `TokenUsage` in responses for cost tracking
- Should implement retry with exponential backoff for transient errors
- Should raise `LLMAuthError`, `LLMRateLimitError`, `LLMTimeoutError` for known error classes

---

## Custom Sandbox Configuration

### Dockerfile

The sandbox image is defined in `agent_forge/sandbox/Dockerfile`. To add dependencies:

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
