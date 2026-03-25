# Agent Forge — Technical Specification

> **Status:** Draft v1.0  
> **Last updated:** 2026-03-07

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Goals & Vision](#2-goals--vision)
3. [System Architecture](#3-system-architecture)
4. [Module Specifications](#4-module-specifications)
   - 4.1 [LLM Client Layer](#41-llm-client-layer)
   - 4.2 [Tool System](#42-tool-system)
   - 4.3 [Sandbox Runtime](#43-sandbox-runtime)
   - 4.4 [Agent Core (ReAct Loop)](#44-agent-core-react-loop)
   - 4.5 [Orchestration Layer](#45-orchestration-layer)
   - 4.6 [Observability](#46-observability)
5. [Data Models & Schemas](#5-data-models--schemas)
6. [Configuration](#6-configuration)
7. [Error Handling & Reliability](#7-error-handling--reliability)
8. [Security Model](#8-security-model)
9. [Testing Strategy](#9-testing-strategy)
10. [Project Structure](#10-project-structure)
11. [Deployment & Infrastructure](#11-deployment--infrastructure)
12. [Roadmap](#12-roadmap)

---

## 1. Executive Summary

**Agent Forge** is a sandboxed AI coding agent runtime that autonomously modifies codebases by orchestrating LLM reasoning with isolated tool execution. It implements the **ReAct** (Reasoning + Acting) pattern: an agent receives a coding task, iteratively reasons about what to do, invokes tools inside Docker containers, and loops until the task is complete.

### Key differentiators

| Differentiator               | What it means                                                                   |
| ---------------------------- | ------------------------------------------------------------------------------- |
| **Sandboxed execution**      | Every tool invocation runs in an ephemeral Docker container — never on the host |
| **Multi-provider LLM**       | Gemini (primary), OpenAI, Anthropic via a unified adapter layer                 |
| **Production observability** | Structured logs, trace IDs, token/cost tracking on every run                    |
| **Queue-based scaling**      | Redis + task queue for concurrent, isolated agent runs                          |

---

## 2. Goals & Vision

### Core Goals

- Provide a **working end-to-end agent** that can autonomously modify code in a sandboxed repository.
- Implement **production-grade patterns**: retries, timeouts, structured logging, cost tracking.
- Support **multiple LLM providers** behind a clean abstraction.
- Run all tools **inside isolated Docker containers** to prevent host contamination.
- Be **easily extensible** — adding a new tool or LLM provider should require only implementing an interface.
- Provide clear **documentation and demo recordings** to showcase the system's capabilities.

### Long-Term Vision

Agent Forge is designed to evolve from a single-agent CLI tool into a **full-stack AI coding agent platform**. Every layer is architected with this trajectory in mind:

| Dimension          | Starting Point              | Evolution Path                                                    |
| ------------------ | --------------------------- | ----------------------------------------------------------------- |
| **Interface**      | CLI-first                   | → REST API → Real-time Web Dashboard → IDE plugins                |
| **Isolation**      | Docker containers           | → Firecracker microVMs → Kubernetes pod sandboxes                 |
| **Agent topology** | Single agent, single task   | → Multi-agent collaboration → hierarchical delegation             |
| **LLM strategy**   | Foundation model APIs as-is | → Custom system prompts → fine-tuned routing → self-hosted models |
| **Multi-tenancy**  | Single-user local           | → Auth + RBAC → team workspaces → SaaS billing                    |
| **Tool ecosystem** | Built-in tools only         | → Plugin system → MCP server → community marketplace              |
| **Memory**         | Stateless per-run           | → Persistent memory → cross-run learning → vector-backed RAG      |

Each step on these paths is captured as a concrete milestone in [§12. Roadmap](#12-roadmap).

---

## 3. System Architecture

### 3.1 High-Level Diagram

```
┌───────────────────────────────────────────────────────────────────┐
│                          CLI / API                                │
│                   (click CLI + optional REST)                     │
└────────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
┌───────────────────────────────────────────────────────────────────┐
│                     Orchestration Layer                            │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────────┐  │
│  │ Task Queue   │  │ State Machine│  │ Event Bus               │  │
│  │ (Redis/      │  │ (per-run     │  │ (in-process pub/sub     │  │
│  │  in-memory)  │  │  lifecycle)  │  │  + optional WebSocket)  │  │
│  └──────┬───────┘  └──────┬───────┘  └────────────┬────────────┘  │
└─────────┼─────────────────┼───────────────────────┼───────────────┘
          │                 │                       │
          ▼                 ▼                       ▼
┌───────────────────────────────────────────────────────────────────┐
│                        Agent Core                                 │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │                    ReAct Loop                                │ │
│  │  1. Observe  → 2. Reason (LLM) → 3. Act (Tool) → 4. Repeat │ │
│  └──────────┬───────────────┬───────────────────────────────────┘ │
│             │               │                                     │
│             ▼               ▼                                     │
│  ┌──────────────┐  ┌───────────────┐                              │
│  │  LLM Client  │  │ Tool Registry │                              │
│  │  (adapters)  │  │ (dispatch)    │                              │
│  └──────┬───────┘  └───────┬───────┘                              │
└─────────┼──────────────────┼──────────────────────────────────────┘
          │                  │
          ▼                  ▼
┌──────────────────┐  ┌─────────────────────────────────────────────┐
│  LLM Providers   │  │          Sandbox Runtime                    │
│  ┌────────────┐  │  │  ┌─────────────────────────────────────┐    │
│  │ Gemini API │  │  │  │ Docker Container (ephemeral)        │    │
│  ├────────────┤  │  │  │  - Mounted workspace (read/write)   │    │
│  │ OpenAI API │  │  │  │  - Resource limits (CPU/mem/time)   │    │
│  ├────────────┤  │  │  │  - No network (configurable)        │    │
│  │ Anthropic  │  │  │  └─────────────────────────────────────┘    │
│  └────────────┘  │  └─────────────────────────────────────────────┘
└──────────────────┘
          │
          ▼
┌───────────────────────────────────────────────────────────────────┐
│                      Observability                                │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────────┐ │
│  │ Structured   │  │ Token/Cost   │  │ Trace Context           │ │
│  │ JSON Logs    │  │ Tracker      │  │ (run_id propagation)    │ │
│  └──────────────┘  └──────────────┘  └─────────────────────────┘ │
└───────────────────────────────────────────────────────────────────┘
```

### 3.2 Request Flow (Sequence)

```
User ──▶ CLI/API
           │
           ├─ 1. Create AgentRun (state=PENDING)
           ├─ 2. Enqueue task in Task Queue
           │
     Task Queue ──▶ Worker picks up
           │
           ├─ 3. Transition state → RUNNING
           ├─ 4. Provision sandbox (Docker container)
           ├─ 5. Enter ReAct loop:
           │      ├─ 5a. Build prompt with conversation history + tool results
           │      ├─ 5b. Call LLM (streaming)
           │      ├─ 5c. Parse tool call from LLM response
           │      ├─ 5d. Execute tool inside sandbox
           │      ├─ 5e. Append tool result to conversation
           │      ├─ 5f. Check termination conditions
           │      └─ 5g. Repeat from 5a
           ├─ 6. Transition state → COMPLETED | FAILED
           ├─ 7. Tear down sandbox
           └─ 8. Emit completion event + log summary
```

---

## 4. Module Specifications

### 4.1 LLM Client Layer

#### Purpose

Provide a **unified interface** to interact with multiple LLM providers, hiding provider-specific serialization, auth, and error formats behind a common adapter.

#### Interface Contract

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator

class Role(Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

@dataclass
class Message:
    role: Role
    content: str
    tool_call_id: str | None = None       # set when role=TOOL
    tool_calls: list["ToolCall"] | None = None  # set when role=ASSISTANT

@dataclass
class ToolCall:
    id: str               # unique ID for this call
    name: str             # tool name, e.g. "read_file"
    arguments: dict       # parsed JSON arguments

@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict      # JSON Schema object

@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    usage: "TokenUsage"
    model: str
    finish_reason: str    # "stop", "tool_calls", "length", "error"

@dataclass
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

@dataclass
class LLMConfig:
    model: str
    temperature: float = 0.0
    max_tokens: int = 4096
    top_p: float = 1.0
    timeout_seconds: int = 120

class LLMProvider(ABC):
    """Base class for all LLM provider adapters."""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> LLMResponse:
        """Send a completion request and return the full response."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: LLMConfig | None = None,
    ) -> AsyncIterator[LLMResponse]:
        """Stream partial responses as they arrive."""
        ...
```

#### Provider Implementations

| Provider      | Module                      | Auth                        | Model Default              |
| ------------- | --------------------------- | --------------------------- | -------------------------- |
| **Gemini**    | `agent_forge.llm.gemini`    | `GEMINI_API_KEY` env var    | `gemini-2.0-flash`         |
| **OpenAI**    | `agent_forge.llm.openai`    | `OPENAI_API_KEY` env var    | `gpt-4o`                   |
| **Anthropic** | `agent_forge.llm.anthropic` | `ANTHROPIC_API_KEY` env var | `claude-sonnet-4-20250514` |

#### Provider-Specific Mapping Rules

- **Gemini:** Translate `ToolDefinition.parameters` to Gemini's `FunctionDeclaration` schema. Map `tool_calls` ↔ `function_call` / `function_response` parts.
- **OpenAI:** Direct mapping to `tools` array in chat completions API. Use `tool_choice: "auto"`.
- **Anthropic:** Map to Anthropic's `tools` format. Handle `tool_use` / `tool_result` content blocks.

#### Error Handling

| Error Type         | Strategy                                                    |
| ------------------ | ----------------------------------------------------------- |
| Rate limit (429)   | Exponential backoff: 1s → 2s → 4s → 8s → 16s, max 3 retries |
| Auth error (401)   | Fail immediately with clear message                         |
| Timeout            | Cancel after `config.timeout_seconds`, retry once           |
| Malformed response | Log raw response, retry once with same prompt               |
| Context overflow   | Truncate oldest non-system messages, retry                  |

---

### 4.2 Tool System

#### Purpose

Define a **pluggable tool registry** where each tool describes itself (name, description, parameter schema) and executes inside the sandbox runtime.

#### Interface Contract

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ToolResult:
    output: str             # stdout or primary output
    error: str | None       # stderr or error message
    exit_code: int          # 0 = success
    execution_time_ms: int  # wall-clock time

class Tool(ABC):
    """Base class for all agent tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name, e.g. 'read_file'."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for the LLM."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """JSON Schema describing accepted arguments."""
        ...

    @abstractmethod
    async def execute(self, arguments: dict, sandbox: "Sandbox") -> ToolResult:
        """Execute the tool inside the given sandbox."""
        ...

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )
```

#### Built-in Tools

##### `read_file`

| Field       | Value                                                                               |
| ----------- | ----------------------------------------------------------------------------------- |
| Description | Read the contents of a file at the given path                                       |
| Parameters  | `path` (string, required) — relative path within the workspace                      |
| Behavior    | Runs `cat <path>` inside sandbox. Returns file content or error.                    |
| Limits      | Max file size: 100 KB. Files larger than this return a truncated preview + warning. |

##### `write_file`

| Field       | Value                                                                     |
| ----------- | ------------------------------------------------------------------------- |
| Description | Create or overwrite a file at the given path with the provided content    |
| Parameters  | `path` (string, required), `content` (string, required)                   |
| Behavior    | Writes content to the specified path inside sandbox. Creates parent dirs. |
| Validation  | Path must be within `/workspace`. Reject paths containing `..` traversal. |

##### `edit_file`

| Field       | Value                                                                                   |
| ----------- | --------------------------------------------------------------------------------------- |
| Description | Apply a targeted edit to a file by replacing a specific text block                      |
| Parameters  | `path` (string, required), `old_text` (string, required), `new_text` (string, required) |
| Behavior    | Reads file, replaces first occurrence of `old_text` with `new_text`, writes back.       |
| Error       | If `old_text` not found, return error with message + file content preview.              |

##### `run_shell`

| Field       | Value                                                                                   |
| ----------- | --------------------------------------------------------------------------------------- | ------------------------------------------ |
| Description | Execute a shell command inside the sandboxed workspace                                  |
| Parameters  | `command` (string, required), `timeout_seconds` (int, optional, default 30)             |
| Behavior    | Runs command via `bash -c` in sandbox. Captures stdout + stderr.                        |
| Limits      | Hard timeout at `min(timeout_seconds, 120)`. Max output: 50 KB (truncate with warning). |
| Blocklist   | Reject commands matching: `rm -rf /`, `:(){ :                                           | :& };:`, `mkfs`, `dd if=/dev/`, `shutdown` |

##### `search_codebase`

| Field       | Value                                                                                                   |
| ----------- | ------------------------------------------------------------------------------------------------------- |
| Description | Search for a pattern across files in the workspace using ripgrep                                        |
| Parameters  | `pattern` (string, required), `file_glob` (string, optional), `max_results` (int, optional, default 20) |
| Behavior    | Runs `rg --json` inside sandbox. Parses and formats results.                                            |
| Output      | Returns list of `{file, line, content}` matches.                                                        |

##### `list_directory`

| Field       | Value                                                                                                                                |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Description | List files and directories at the given path                                                                                         |
| Parameters  | `path` (string, optional, default `/workspace`), `recursive` (bool, optional, default false), `max_depth` (int, optional, default 3) |
| Behavior    | Runs `find` or `tree` inside sandbox.                                                                                                |

#### Tool Registry

```python
class ToolRegistry:
    """Manages available tools and dispatches executions."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolNotFoundError(f"Unknown tool: '{name}'")
        return self._tools[name]

    def list_definitions(self) -> list[ToolDefinition]:
        return [t.to_definition() for t in self._tools.values()]
```

---

### 4.3 Sandbox Runtime

#### Purpose

Provide **isolated execution environments** for tool invocations using Docker containers. Each agent run gets its own container with the target repository mounted.

#### Interface Contract

```python
from dataclasses import dataclass

@dataclass
class SandboxConfig:
    image: str = "agent-forge-sandbox:latest"    # Pre-built image with common tools
    workspace_path: str = "/workspace"           # Mount point inside container
    cpu_limit: float = 1.0                       # CPU cores
    memory_limit: str = "512m"                   # Memory limit
    timeout_seconds: int = 300                   # Max container lifetime
    network_enabled: bool = False                # Network access (default: isolated)
    env_vars: dict[str, str] = field(default_factory=dict)

class Sandbox(ABC):
    """Manages an isolated execution environment."""

    @abstractmethod
    async def start(self, repo_path: str, config: SandboxConfig) -> None:
        """Create and start the sandbox container with the repo mounted."""
        ...

    @abstractmethod
    async def exec(self, command: str, timeout: int = 30) -> ToolResult:
        """Execute a command inside the running sandbox."""
        ...

    @abstractmethod
    async def read_file(self, path: str) -> str:
        """Read a file from the sandbox filesystem."""
        ...

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None:
        """Write a file to the sandbox filesystem."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop and remove the sandbox container."""
        ...

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the sandbox is still running."""
        ...
```

#### Sandbox Docker Image

The sandbox base image should be built with common development tools pre-installed:

```dockerfile
# agent_forge/sandbox/Dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ripgrep \
    tree \
    curl \
    jq \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install common Python dev tools
RUN pip install --no-cache-dir \
    black \
    ruff \
    pytest \
    mypy

WORKDIR /workspace

# Non-root user for security
RUN useradd -m -s /bin/bash agent && chown agent:agent /workspace
USER agent
```

#### Container Lifecycle

```
┌──────────┐     start()      ┌─────────┐     exec() × N     ┌──────────┐
│  IDLE    │ ──────────────▶  │ RUNNING │ ◀───────────────▶ │ RUNNING  │
└──────────┘                  └────┬────┘                   └──────────┘
                                   │
                           stop() or timeout
                                   │
                                   ▼
                              ┌──────────┐
                              │ STOPPED  │
                              └──────────┘
```

#### Sandbox Philosophy

> **Sandboxing means isolation, not restriction.**

The sandbox is an **isolation boundary** — it prevents agent actions from affecting the host system, other agents, or external services. However, _inside_ the sandbox, the agent should have access to all the compute resources it needs to complete its task.

This follows the model established by platforms like [E2B](https://e2b.dev): each agent runs in its own isolated environment with full access to filesystem, network, package managers, and runtimes — the boundary is between the sandbox and the outside world, not between the agent and its own tools.

**Core principles:**

| Principle                     | Description                                                                                                                             |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **Isolation**                 | Each agent run gets its own ephemeral container. No shared state between runs.                                                          |
| **Secure by default**         | New sandboxes start with restricted permissions (no network, read-only root).                                                           |
| **Configurable capabilities** | Permissions are opt-in — operators explicitly grant network access, writable paths, and custom runtimes based on the task requirements. |
| **Ephemeral**                 | Containers are created per-run and destroyed after completion. No persistence between runs.                                             |

#### Permission Model

Sandbox capabilities are configured through `SandboxConfig` and CLI flags. Each permission starts restricted and can be explicitly enabled:

| Capability          | Default           | Opt-in via                           | Use case                          |
| ------------------- | ----------------- | ------------------------------------ | --------------------------------- |
| **Network access**  | ❌ Disabled       | `network_enabled=True` / `--network` | Install dependencies, fetch APIs  |
| **Custom runtime**  | Python 3.12       | `image="..."` / `--sandbox-image`    | Node.js, Go, Rust, multi-runtime  |
| **Writable paths**  | `/workspace` only | Future: `--writable-paths`           | Cache dirs (`~/.npm`, `~/.cache`) |
| **Command timeout** | 120s              | Future: `--command-timeout`          | Long builds, `npm install`        |
| **Tmpfs size**      | 64 MB (noexec)    | Future: `--tmpfs-size`               | Larger temporary storage          |

> **Design decision:** We start restricted and let operators widen permissions, rather than starting open and asking them to lock down. This ensures that forgetting to configure is safe, not dangerous.

#### Security Constraints (Defaults)

These are the **default** security constraints applied to every sandbox. They represent the most restrictive baseline and can be selectively relaxed through the permission model above.

| Constraint            | Implementation                                       |
| --------------------- | ---------------------------------------------------- |
| **No host network**   | `--network none` by default                          |
| **Read-only root FS** | `--read-only` with `/workspace` as a bind-mount (rw) |
| **No privileged**     | Never use `--privileged`                             |
| **Resource caps**     | `--cpus`, `--memory`, `--pids-limit 256`             |
| **No new privileges** | `--security-opt no-new-privileges`                   |
| **Tmpfs for temp**    | `--tmpfs /tmp:rw,noexec,nosuid,size=64m`             |
| **Auto-remove**       | `--rm` flag, plus cleanup on agent run completion    |

> **Note:** `--privileged` and `no-new-privileges` are **hard constraints** that cannot be relaxed. All other constraints are configurable.

---

### 4.4 Agent Core (ReAct Loop)

#### Purpose

Implement the **Reasoning + Acting** loop: the agent iteratively calls the LLM, parses tool invocations, executes them, and feeds results back until the task is complete or a termination condition is reached.

#### Interface Contract

```python
@dataclass
class AgentConfig:
    max_iterations: int = 25          # Safety guard
    max_tokens_per_run: int = 200_000 # Budget guard
    model: str = "gemini-2.0-flash"
    provider: str = "gemini"          # "gemini" | "openai" | "anthropic"
    temperature: float = 0.0
    system_prompt: str | None = None  # Override default system prompt

@dataclass
class AgentRun:
    id: str                           # UUID
    task: str                         # User's coding task description
    repo_path: str                    # Path to target repository
    config: AgentConfig
    state: "RunState"
    messages: list[Message]           # Full conversation history
    iterations: int = 0
    total_tokens: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0, 0))
    tool_invocations: list["ToolInvocation"] = field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None

class RunState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

@dataclass
class ToolInvocation:
    tool_name: str
    arguments: dict
    result: ToolResult
    iteration: int
    timestamp: datetime
    duration_ms: int
```

#### ReAct Loop Algorithm

```
function react_loop(run: AgentRun, llm: LLMProvider, tools: ToolRegistry, sandbox: Sandbox):

    run.messages.append(system_prompt(run.task))
    run.messages.append(user_message(run.task))

    while run.iterations < run.config.max_iterations:
        run.iterations += 1

        # 1. REASON — ask the LLM what to do next
        response = await llm.complete(
            messages=run.messages,
            tools=tools.list_definitions(),
            config=run.config,
        )
        run.total_tokens += response.usage

        # 2. CHECK BUDGET
        if run.total_tokens.total_tokens > run.config.max_tokens_per_run:
            run.state = RunState.TIMEOUT
            break

        # 3. CHECK COMPLETION
        if response.finish_reason == "stop" and not response.tool_calls:
            run.messages.append(assistant_message(response.content))
            run.state = RunState.COMPLETED
            break

        # 4. ACT — execute each tool call
        run.messages.append(assistant_message(response.content, response.tool_calls))

        for tool_call in response.tool_calls:
            tool = tools.get(tool_call.name)
            result = await tool.execute(tool_call.arguments, sandbox)

            run.tool_invocations.append(ToolInvocation(
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                result=result,
                iteration=run.iterations,
                timestamp=now(),
                duration_ms=result.execution_time_ms,
            ))

            run.messages.append(tool_message(tool_call.id, result))

    else:
        # Max iterations reached
        run.state = RunState.TIMEOUT

    return run
```

#### System Prompt Design

```
You are Agent Forge, an autonomous coding agent. You are given a coding task
and a workspace containing a code repository.

## Your Capabilities
You can use the following tools to complete the task:
{tool_descriptions}

## Rules
1. Always read relevant files before making changes.
2. Make small, focused changes. Do not rewrite entire files unnecessarily.
3. After making changes, verify they work (e.g., run tests or linters).
4. If you encounter an error, analyze it and try a different approach.
5. When the task is complete, provide a summary of what you changed and why.
6. Do NOT attempt to access the internet or external services.
7. Stay within the /workspace directory.

## Task
{task_description}
```

#### Termination Conditions

| Condition                                          | Action          | State       |
| -------------------------------------------------- | --------------- | ----------- |
| LLM returns no tool calls and `finish_reason=stop` | Task complete   | `COMPLETED` |
| `iterations >= max_iterations`                     | Force stop      | `TIMEOUT`   |
| `total_tokens >= max_tokens_per_run`               | Budget exceeded | `TIMEOUT`   |
| Unrecoverable error                                | Log error, stop | `FAILED`    |
| User cancellation                                  | Graceful stop   | `CANCELLED` |

---

### 4.5 Orchestration Layer

#### Purpose

Manage task queuing, concurrent agent runs, and lifecycle state transitions.

#### Components

##### Task Queue

```python
@dataclass
class Task:
    id: str                        # UUID
    task_description: str
    repo_path: str
    config: AgentConfig
    priority: int = 0              # Higher = more urgent
    created_at: datetime
    status: TaskStatus

class TaskStatus(Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class TaskQueue(ABC):
    @abstractmethod
    async def enqueue(self, task: Task) -> str:
        """Add a task to the queue. Returns task ID."""
        ...

    @abstractmethod
    async def dequeue(self) -> Task | None:
        """Get the next task from the queue."""
        ...

    @abstractmethod
    async def get_status(self, task_id: str) -> TaskStatus:
        ...

    @abstractmethod
    async def cancel(self, task_id: str) -> bool:
        ...
```

##### Queue Implementations

| Implementation | Module                     | Use Case                             |
| -------------- | -------------------------- | ------------------------------------ |
| **InMemory**   | `agent_forge.queue.memory` | Development, testing, single-process |
| **Redis**      | `agent_forge.queue.redis`  | Production, multi-worker             |

##### State Machine

Valid state transitions for an `AgentRun`:

```
PENDING ──▶ RUNNING ──▶ COMPLETED
                    ──▶ FAILED
                    ──▶ TIMEOUT
                    ──▶ CANCELLED
```

Invalid transitions (e.g., `COMPLETED → RUNNING`) must raise `InvalidStateTransitionError`.

##### Event Bus

```python
class EventType(Enum):
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    ITERATION_STARTED = "iteration.started"
    ITERATION_COMPLETED = "iteration.completed"
    TOOL_CALLED = "tool.called"
    TOOL_COMPLETED = "tool.completed"
    TOKEN_USAGE = "token.usage"

@dataclass
class Event:
    type: EventType
    run_id: str
    timestamp: datetime
    data: dict

class EventBus:
    async def publish(self, event: Event) -> None: ...
    async def subscribe(self, event_type: EventType, handler: Callable) -> str: ...
    async def unsubscribe(self, subscription_id: str) -> None: ...
```

---

### 4.6 Observability

#### Purpose

Provide **structured logging**, **distributed trace context**, and **usage/cost tracking** for every agent run.

#### Structured Logging

All log entries are JSON objects with a consistent schema:

```json
{
  "timestamp": "2026-03-07T00:30:00.000Z",
  "level": "INFO",
  "run_id": "a1b2c3d4-...",
  "iteration": 3,
  "component": "agent_core",
  "event": "tool_executed",
  "tool_name": "read_file",
  "duration_ms": 45,
  "message": "Tool 'read_file' completed successfully"
}
```

Logger configuration:

| Destination      | Format       | Level | When              |
| ---------------- | ------------ | ----- | ----------------- |
| Console (stderr) | Colored text | INFO  | Always            |
| File             | JSON         | DEBUG | Always            |
| Structured sink  | JSON         | INFO  | Optional (future) |

#### Token & Cost Tracking

```python
@dataclass
class CostTracker:
    """Tracks token usage and estimated cost per agent run."""

    run_id: str
    entries: list["CostEntry"] = field(default_factory=list)

    def record(self, usage: TokenUsage, model: str) -> None:
        cost = self._estimate_cost(usage, model)
        self.entries.append(CostEntry(
            model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            estimated_cost_usd=cost,
            timestamp=now(),
        ))

    def total_cost(self) -> float:
        return sum(e.estimated_cost_usd for e in self.entries)

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "total_prompt_tokens": sum(e.prompt_tokens for e in self.entries),
            "total_completion_tokens": sum(e.completion_tokens for e in self.entries),
            "total_tokens": sum(e.prompt_tokens + e.completion_tokens for e in self.entries),
            "total_cost_usd": self.total_cost(),
            "llm_calls": len(self.entries),
        }

# Approximate pricing (per 1M tokens) — update as providers change
COST_TABLE = {
    "gemini-2.0-flash":        {"prompt": 0.075,  "completion": 0.30},
    "gpt-4o":                  {"prompt": 2.50,   "completion": 10.00},
    "claude-sonnet-4-20250514":  {"prompt": 3.00,   "completion": 15.00},
}
```

#### Run Summary Report

At the end of each run, produce a summary printed to console:

```
╔══════════════════════════════════════════════════════════╗
║                    Agent Forge — Run Summary             ║
╠══════════════════════════════════════════════════════════╣
║ Run ID:        a1b2c3d4-e5f6-...                        ║
║ Status:        ✅ COMPLETED                              ║
║ Task:          Add input validation to /api/users        ║
║ Duration:      47.3s                                     ║
║ Iterations:    6                                         ║
║ Model:         gemini-2.0-flash                         ║
╠──────────────────────────────────────────────────────────╣
║ Token Usage                                              ║
║   Prompt:      12,450 tokens                             ║
║   Completion:  3,210 tokens                              ║
║   Total:       15,660 tokens                             ║
║   Est. Cost:   $0.0019                                   ║
╠──────────────────────────────────────────────────────────╣
║ Tool Invocations                                         ║
║   read_file:       3 calls (avg 42ms)                    ║
║   write_file:      2 calls (avg 38ms)                    ║
║   run_shell:       1 call  (avg 1,203ms)                 ║
╠──────────────────────────────────────────────────────────╣
║ Files Modified                                           ║
║   M  src/api/users.py                                    ║
║   M  tests/test_users.py                                 ║
╚══════════════════════════════════════════════════════════╝
```

---

## 5. Data Models & Schemas

### 5.1 Hosted Service Run API

Hosted mode exposes a versioned machine API for external clients. The first
contract version is intentionally polling-based and centered on a single run
resource:

- `POST /v1/runs` accepts a `RunRequest` document with `client`, `profile`,
  `source`, optional `target`, and optional artifact delivery preferences.
- `GET /v1/runs/{run_id}` returns a `RunStatus` document that includes the run
  lifecycle state, client/profile echo fields, and stable artifact references.
- All request and response models are versioned with explicit
  `schema_version` fields so hosted clients do not have to infer compatibility
  from CLI behavior.

The v1 request supports these source payload patterns:

- `local_path` for already-resolved source paths in a colocated environment
- `archive_uri` for source bundles such as `.zip` and `.tar.gz`
- `repository_uri` or `git_repository` for externally-resolved repository inputs

The v1 status model standardizes lifecycle states as:

- `accepted`
- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`

The v1 error envelope standardizes machine-readable failures such as:

- validation failures (`invalid_request`)
- source acquisition failures (`source_fetch_failed`)
- sandbox/runtime failures (`sandbox_start_failed`, `sandbox_execution_failed`)
- policy failures (`policy_denied`, `unauthorized`, `quota_exceeded`)

Artifact references are part of the status document rather than being implied:

- `report`
- `logs`
- `run_metadata`

### 5.1 Configuration File Schema

Agent Forge uses a TOML configuration file (`agent-forge.toml`):

```toml
[agent]
max_iterations = 25
max_tokens_per_run = 200_000
default_provider = "gemini"
default_model = "gemini-2.0-flash"
temperature = 0.0
system_prompt_path = ""          # Optional custom system prompt file

[sandbox]
image = "agent-forge-sandbox:latest"
cpu_limit = 1.0
memory_limit = "512m"
timeout_seconds = 300
network_enabled = false

[queue]
backend = "memory"               # "memory" or "redis"
redis_url = "redis://localhost:6379/0"
max_concurrent_runs = 4

[logging]
level = "INFO"                   # DEBUG, INFO, WARNING, ERROR
format = "text"                  # "text" or "json"
log_file = ""                    # Optional path for JSON log file

[providers.gemini]
api_key_env = "GEMINI_API_KEY"
default_model = "gemini-2.0-flash"

[providers.openai]
api_key_env = "OPENAI_API_KEY"
default_model = "gpt-4o"

[providers.anthropic]
api_key_env = "ANTHROPIC_API_KEY"
default_model = "claude-sonnet-4-20250514"
```

### 5.2 Run Persistence

Agent runs are persisted as JSON files under `~/.agent-forge/runs/<run_id>/`:

```
~/.agent-forge/runs/<run_id>/
├── run.json           # AgentRun metadata (state, config, timestamps)
├── messages.jsonl     # Full conversation history (line-delimited JSON)
├── events.jsonl       # All events emitted during the run
└── summary.json       # Final cost/token summary
```

---

## 6. Configuration

### 6.1 Configuration Precedence

Configuration is resolved in the following order (highest priority first):

1. **CLI flags** — e.g., `--model gpt-4o --max-iterations 10`
2. **Environment variables** — e.g., `AGENT_FORGE_MAX_ITERATIONS=10`
3. **Project config** — `./agent-forge.toml` in the current directory
4. **User config** — `~/.agent-forge/config.toml`
5. **Built-in defaults** — hardcoded in `AgentConfig` / `SandboxConfig`

### 6.2 Environment Variable Mapping

Format: `AGENT_FORGE_{SECTION}_{KEY}` (uppercase, underscored).

| Env Var                            | Config Path            |
| ---------------------------------- | ---------------------- |
| `AGENT_FORGE_AGENT_MAX_ITERATIONS` | `agent.max_iterations` |
| `AGENT_FORGE_SANDBOX_MEMORY_LIMIT` | `sandbox.memory_limit` |
| `AGENT_FORGE_QUEUE_BACKEND`        | `queue.backend`        |
| `GEMINI_API_KEY`                   | (direct, not prefixed) |
| `OPENAI_API_KEY`                   | (direct, not prefixed) |
| `ANTHROPIC_API_KEY`                | (direct, not prefixed) |

---

## 7. Error Handling & Reliability

### 7.1 Error Taxonomy

```python
class AgentForgeError(Exception):
    """Base exception for all Agent Forge errors."""

class LLMError(AgentForgeError):
    """Errors from LLM provider interactions."""

class LLMRateLimitError(LLMError):
    """Rate limit exceeded — should trigger retry with backoff."""

class LLMAuthError(LLMError):
    """Invalid API key or unauthorized — fail immediately."""

class LLMContextOverflowError(LLMError):
    """Prompt exceeds model context window."""

class ToolError(AgentForgeError):
    """Errors from tool execution."""

class ToolNotFoundError(ToolError):
    """LLM requested a tool that doesn't exist."""

class ToolTimeoutError(ToolError):
    """Tool execution exceeded timeout."""

class ToolExecutionError(ToolError):
    """Tool returned non-zero exit code."""

class SandboxError(AgentForgeError):
    """Errors from sandbox operations."""

class SandboxStartupError(SandboxError):
    """Failed to create or start the sandbox container."""

class SandboxTimeoutError(SandboxError):
    """Sandbox container lifetime exceeded."""

class InvalidStateTransitionError(AgentForgeError):
    """Invalid run state transition attempted."""
```

### 7.2 Retry Policies

| Component      | Retry Strategy                                    | Max Retries | Backoff               |
| -------------- | ------------------------------------------------- | ----------- | --------------------- |
| LLM API calls  | Retry on 429, 500, 502, 503, timeout              | 3           | Exponential (1s base) |
| Tool execution | Retry on sandbox transient failure                | 1           | Fixed 2s              |
| Sandbox start  | Retry if Docker daemon is temporarily unavailable | 2           | Fixed 5s              |

### 7.3 Graceful Degradation

- **LLM tool-call parse failure:** If the LLM produces a malformed tool call, inject an error message into the conversation and let the LLM self-correct in the next iteration.
- **Unknown tool requested:** Return a friendly error message listing available tools, let the LLM pick a valid one.
- **Sandbox dies mid-run:** Attempt to restart the container once with the workspace intact. If it fails again, mark run as `FAILED`.

---

## 8. Security Model

### 8.1 Threat Model

| Threat                       | Mitigation                                                      |
| ---------------------------- | --------------------------------------------------------------- |
| **Host escape from sandbox** | No `--privileged`, `no-new-privileges`, read-only root FS       |
| **Resource exhaustion**      | CPU/memory/PID limits on containers                             |
| **Malicious LLM output**     | Command blocklist in `run_shell`, path validation in file tools |
| **Data exfiltration**        | `--network none` by default; network only enabled when explicit |
| **API key leakage**          | Keys only in env vars, never logged, never passed to sandbox    |
| **Path traversal**           | All file operations validated to stay within `/workspace`       |
| **Fork bomb**                | `--pids-limit 256`                                              |
| **Disk exhaustion**          | Tmpfs size limits, workspace quota via Docker storage driver    |

### 8.2 API Key Management

- API keys are **never** passed to the sandbox container.
- API keys are read from environment variables on the host.
- Structured logs **redact** any string matching an API key pattern before writing.
- The `.env` file (if used) must be in `.gitignore`.

---

## 9. Testing Strategy

### 9.1 Test Layers

| Layer           | Framework | Target                          | Coverage Goal |
| --------------- | --------- | ------------------------------- | ------------- |
| **Unit tests**  | `pytest`  | Individual modules in isolation | 80%+          |
| **Integration** | `pytest`  | Module interactions, Docker     | Key flows     |
| **End-to-end**  | `pytest`  | Full agent run on sample repo   | 3+ scenarios  |

### 9.2 Unit Tests

- **LLM Client:** Mock HTTP responses; verify message serialization, error handling, retry logic.
- **Tools:** Mock sandbox; verify argument validation, output parsing, error formatting.
- **Agent Core:** Mock LLM + tools; verify ReAct loop terminates correctly, token budget enforced.
- **Tool Registry:** Verify registration, lookup, duplicate rejection.
- **Config:** Verify precedence (CLI > env > file > defaults).

### 9.3 Integration Tests

- **Sandbox + Tools:** Spin up a real Docker container, execute real commands, verify file I/O.
- **Agent + LLM (recorded):** Use recorded/cached LLM responses (VCR pattern) to test full loops deterministically.

### 9.4 End-to-End Tests

Use a sample repository (e.g., a small Flask app) and recorded LLM responses:

1. **"Add input validation"** — Verify agent reads the endpoint, adds validation code, runs tests.
2. **"Fix the bug in calculate_total"** — Verify agent identifies and fixes a planted bug.
3. **"Add type hints to utils.py"** — Verify agent adds type annotations without breaking tests.

### 9.5 Test Running

```bash
# Unit tests only (fast, no Docker needed)
pytest tests/unit -v

# Integration tests (requires Docker)
pytest tests/integration -v

# E2E tests (requires Docker + API key or recorded responses)
pytest tests/e2e -v

# All tests with coverage
pytest --cov=agent_forge --cov-report=html
```

---

## 10. Project Structure

```
agent-forge/
├── pyproject.toml                   # Project metadata, dependencies
├── agent-forge.toml                 # Default configuration
├── Dockerfile                       # Sandbox base image
├── docker-compose.yml               # Full stack (app + Redis)
├── Makefile                         # Common commands
├── README.md                        # Project documentation
├── spec.md                          # This specification
│
├── agent_forge/
│   ├── __init__.py
│   ├── cli.py                       # Click CLI entrypoint
│   ├── config.py                    # Configuration loading & merging
│   │
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py                  # LLMProvider ABC, data classes
│   │   ├── gemini.py                # Gemini adapter
│   │   ├── openai.py                # OpenAI adapter
│   │   ├── anthropic.py             # Anthropic adapter
│   │   └── factory.py               # Provider factory
│   │
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py                  # Tool ABC, ToolResult, ToolRegistry
│   │   ├── read_file.py
│   │   ├── write_file.py
│   │   ├── edit_file.py
│   │   ├── run_shell.py
│   │   ├── search_codebase.py
│   │   └── list_directory.py
│   │
│   ├── sandbox/
│   │   ├── __init__.py
│   │   ├── base.py                  # Sandbox ABC
│   │   ├── docker.py                # Docker implementation
│   │   └── Dockerfile               # Sandbox image definition
│   │
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── core.py                  # ReAct loop implementation
│   │   ├── models.py                # AgentRun, RunState, data classes
│   │   ├── prompts.py               # System prompt templates
│   │   └── state.py                 # State machine
│   │
│   ├── orchestration/
│   │   ├── __init__.py
│   │   ├── queue.py                 # TaskQueue ABC + implementations
│   │   ├── events.py                # EventBus
│   │   └── worker.py                # Queue worker process
│   │
│   └── observability/
│       ├── __init__.py
│       ├── logger.py                # Structured logging setup
│       ├── tracing.py               # Trace context (run_id propagation)
│       └── cost.py                  # Token/cost tracking
│
├── tests/
│   ├── unit/
│   │   ├── test_llm_base.py
│   │   ├── test_llm_gemini.py
│   │   ├── test_tools.py
│   │   ├── test_agent_core.py
│   │   ├── test_config.py
│   │   └── test_cost_tracker.py
│   ├── integration/
│   │   ├── test_sandbox.py
│   │   └── test_tools_in_sandbox.py
│   ├── e2e/
│   │   ├── test_add_validation.py
│   │   ├── test_fix_bug.py
│   │   └── test_add_type_hints.py
│   ├── fixtures/
│   │   └── sample_repo/             # Small Flask app for E2E tests
│   └── conftest.py
│
└── scripts/
    ├── build-sandbox.sh             # Build the sandbox Docker image
    └── demo.sh                      # Record an asciinema demo
```

---

## 11. Deployment & Infrastructure

### 11.1 Dependencies

```toml
# pyproject.toml [project.dependencies]
[project]
name = "agent-forge"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    "click>=8.0",                    # CLI framework
    "httpx>=0.27",                   # Async HTTP client (LLM APIs)
    "docker>=7.0",                   # Docker SDK for Python
    "pydantic>=2.0",                 # Data validation
    "tomli>=2.0; python_version<'3.11'",  # TOML parsing (stdlib in 3.11+)
    "rich>=13.0",                    # Pretty console output
    "structlog>=24.0",               # Structured logging
]

[project.optional-dependencies]
redis = ["redis>=5.0", "hiredis>=3.0"]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "ruff>=0.4",
    "mypy>=1.10",
    "respx>=0.21",                   # Mock httpx (for LLM tests)
]
```

### 11.2 Docker Compose (Development)

```yaml
# docker-compose.yml
version: "3.9"

services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

volumes:
  redis_data:
```

### 11.3 Makefile

```makefile
.PHONY: setup build test lint run clean

setup:                     ## Install dependencies
	pip install -e ".[dev,redis]"

build-sandbox:             ## Build the sandbox Docker image
	docker build -t agent-forge-sandbox:latest -f agent_forge/sandbox/Dockerfile .

test:                      ## Run all tests
	pytest --cov=agent_forge --cov-report=term-missing

test-unit:                 ## Run unit tests only
	pytest tests/unit -v

test-integration:          ## Run integration tests (requires Docker)
	pytest tests/integration -v

lint:                      ## Run linters
	ruff check agent_forge tests
	mypy agent_forge

format:                    ## Auto-format code
	ruff format agent_forge tests

run:                       ## Run a demo task
	python -m agent_forge.cli run --task "Add input validation" --repo ./tests/fixtures/sample_repo

clean:                     ## Clean up containers and build artifacts
	docker rm -f $$(docker ps -aq --filter "ancestor=agent-forge-sandbox") 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .mypy_cache
```

---

## 12. Roadmap

### Phase 1 — Core Agent MVP

> **Goal:** A working end-to-end agent that can modify code inside a Docker sandbox.

- [ ] Project scaffolding (`pyproject.toml`, directory structure, Makefile)
- [ ] Configuration system (TOML loading, env var override, CLI flags)
- [ ] LLM client base classes + Gemini adapter (with streaming)
- [ ] Tool system base classes + `read_file`, `write_file`, `list_directory`
- [ ] Sandbox Docker image + `DockerSandbox` implementation
- [ ] `run_shell`, `search_codebase`, `edit_file` tools
- [ ] ReAct loop implementation with termination conditions
- [ ] State machine for run lifecycle
- [ ] Click CLI with `run`, `status`, `list`, `config` commands
- [ ] Unit + integration tests
- [ ] README with architecture diagram and quick start

### Phase 2 — Production Hardening

> **Goal:** Reliability, observability, and multi-provider support ready for real-world use.

- [ ] Structured logging with `structlog` (JSON + colored console)
- [ ] Token/cost tracking and run summary reports
- [ ] OpenAI + Anthropic LLM adapters
- [ ] In-memory task queue + worker
- [ ] Event bus for run lifecycle events
- [ ] Exponential backoff, timeout handling, graceful degradation
- [ ] Redis task queue backend for concurrent agent runs
- [ ] End-to-end tests with sample repos + recorded LLM responses
- [ ] asciinema demo recording

### Phase 3 — Git-Aware Agent & Plugin System

> **Goal:** The agent understands git workflows and users can extend it with custom tools.

- [ ] Git-aware tools: `git_diff`, `git_commit`, `git_create_branch`, `create_pr`
- [ ] Plugin system: load custom tools from external Python packages
- [ ] Tool dependency resolution (e.g., a tool that requires another tool's output)
- [ ] Custom system prompt templates (per-project `.agent-forge/prompts/`)
- [ ] Agent memory: persist learnings across runs (file-based initially)

### Phase 4 — Web Dashboard & REST API

> **Goal:** A real-time web interface for monitoring and controlling agent runs.

- [ ] REST API layer (FastAPI) for agent run management
- [ ] WebSocket streaming for live run observation
- [ ] Web dashboard (Next.js): run list, live logs, tool invocation timeline
- [ ] Cost guardrails UI: budget alerts, auto-pause when cost exceeds threshold
- [ ] Run history browser with diff viewer for file changes

### Phase 5 — Multi-Agent Collaboration

> **Goal:** Agents can delegate sub-tasks to other agents and coordinate complex workflows.

- [ ] Agent-to-agent communication protocol
- [ ] Hierarchical task delegation: parent agent breaks task into sub-tasks
- [ ] Parallel agent execution with shared workspace coordination
- [ ] Conflict resolution when multiple agents modify the same files
- [ ] Orchestration DSL or YAML-based workflow definitions

### Phase 6 — Advanced Isolation & Scaling

> **Goal:** Enterprise-grade isolation and horizontal scaling.

- [ ] Firecracker microVM sandbox backend (replacing Docker for stronger isolation)
- [ ] Kubernetes-native sandbox provisioning (pod-per-task)
- [ ] Distributed task queue with worker auto-scaling
- [ ] Sandbox image caching and warm pool for sub-second startup
- [ ] Resource usage analytics and capacity planning dashboard

### Phase 7 — Platform & Ecosystem

> **Goal:** Agent Forge becomes a platform others can build on.

- [ ] MCP (Model Context Protocol) tool server: expose tools for interop with external agents
- [ ] Multi-tenant auth + RBAC (team workspaces, API keys, usage quotas)
- [ ] Custom LLM routing: cost/latency-based model selection per tool call
- [ ] Human-in-the-loop approval gates for destructive operations
- [ ] Persistent vector-backed memory for cross-run RAG context
- [ ] Community tool marketplace
- [ ] Self-hosted model support (Ollama, vLLM) as LLM backend
- [ ] IDE plugins (VS Code, JetBrains) for in-editor agent invocation
