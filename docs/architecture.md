# Architecture

> How Agent Forge is structured — from CLI to sandbox.

## System Overview

Agent Forge implements the **ReAct** (Reasoning + Acting) pattern:

```mermaid
graph LR
    A["User Task"] --> B["CLI"]
    B --> C["Orchestration"]
    C --> D["ReAct Loop"]
    D --> E["LLM ↔ Tools ↔ Sandbox"]
    E --> F["Result"]
```

### System Architecture

```mermaid
graph TD
    CLI["CLI / API Layer<br/>(Click commands)"]
    CLI --> Orch

    subgraph Orch["Orchestration Layer"]
        Queue["Task Queue<br/>(Redis / in-memory)"]
        SM["State Machine"]
        EB["Event Bus"]
    end

    Orch --> Core

    subgraph Core["Agent Core"]
        Loop["ReAct Loop<br/>Observe → Reason → Act"]
        Loop --> LLM
        Loop --> Tools
        Loop --> Sandbox
    end

    subgraph LLM["LLM Client"]
        Gemini["Gemini API"]
        OpenAI["OpenAI API"]
        Anthropic["Anthropic API"]
    end

    subgraph Tools["Tool Registry"]
        read_file
        write_file
        edit_file
        list_directory
        run_shell
        search_codebase
    end

    subgraph Sandbox["Sandbox Runtime"]
        Docker["Docker Container<br/>(ephemeral, per-run)"]
    end
```

## Layer Responsibilities

### CLI Layer (`agent_forge/cli.py`)

- Click-based commands: `run`, `status`, `list`, `config`
- Two execution modes for `run`:
  - **Direct mode** (default) — CLI creates an `EventBus` and calls `react_loop()` directly
  - **Queue mode** (`--queue memory|redis`) — CLI enqueues a `Task`, a `Worker` dequeues and runs it
- Wires configuration, API keys, LLM providers, and sandbox
- Rich terminal output (tables, panels, syntax-highlighted JSON)

### Agent Core (`agent_forge/agent/`)

| Module           | Purpose                                                                             |
| ---------------- | ----------------------------------------------------------------------------------- |
| `core.py`        | ReAct loop — the main Observe → Reason → Act cycle                                  |
| `models.py`      | Data classes: `AgentRun`, `AgentConfig`, `RunState`, `ToolInvocation`               |
| `state.py`       | State machine with valid transitions (PENDING → RUNNING → COMPLETED/FAILED/TIMEOUT) |
| `persistence.py` | Save/load runs to `~/.agent-forge/runs/<id>/` as JSON + JSONL                       |
| `prompts.py`     | System prompt builder with tool descriptions                                        |

### LLM Client Layer (`agent_forge/llm/`)

Unified interface `LLMProvider` with adapters for:

- **Gemini** (primary) — REST API via httpx, retry with exponential backoff
- **OpenAI** — chat completions API
- **Anthropic** — messages API with tool_use blocks

All adapters implement:

```python
class LLMProvider(ABC):
    async def complete(messages, tools, config) -> LLMResponse
    async def stream(messages, tools, config) -> AsyncIterator[LLMResponse]
```

### Tool System (`agent_forge/tools/`)

Ten built-in tools, each implementing the `Tool` ABC:

| Tool              | Description                     |
| ----------------- | ------------------------------- |
| `read_file`       | Read file contents from sandbox |
| `write_file`      | Create/overwrite files          |
| `edit_file`       | Surgical line-range edits       |
| `list_directory`  | List files and directories      |
| `run_shell`       | Execute shell commands          |
| `search_codebase` | Grep/ripgrep code search        |
| `git_diff`        | Inspect staged, unstaged, or ref-based diffs |
| `git_commit`      | Commit staged changes           |
| `git_create_branch` | Create and check out a branch |
| `create_pr`       | Open a GitHub pull request      |

Tools are registered in `ToolRegistry` and their schemas are passed to the LLM as function declarations.

### Sandbox Runtime (`agent_forge/sandbox/`)

- Every tool invocation runs inside an **ephemeral Docker container**
- Workspace is bind-mounted read/write
- Configurable: CPU/memory limits, network access, timeout
- Container is created per-run and destroyed after

### Orchestration (`agent_forge/orchestration/`)

| Module           | Purpose                                                           |
| ---------------- | ----------------------------------------------------------------- |
| `queue.py`       | Task queue ABC + in-memory priority queue implementation          |
| `redis_queue.py` | Redis-backed task queue (requires `redis` extra)                  |
| `worker.py`      | Polls the queue, invokes the task runner, emits lifecycle events  |
| `events.py`      | In-process async pub/sub event bus (run started/completed/failed) |

The CLI wires these together: `CLI → TaskQueue.enqueue() → Worker.dequeue() → react_loop()`.

## Hosted Service Mode

Hosted mode reuses the same queue, worker, sandbox, and persistence layers, but
adds a FastAPI edge for machine clients plus client auth/policy enforcement.

- `agent_forge/service/app.py` exposes the hosted `/v1/runs` contract
- `agent_forge/service/security.py` loads client policy from the hosted client registry
- `agent_forge/service/client.py` provides the Proof-of-Audit compatibility harness

For deployment topology, trust boundaries, and rollout guidance, see the
[Hosted Service Guide](hosted-service.md).

## ReAct Loop Sequence

```mermaid
flowchart TD
    A["Build system prompt<br/>(task + tool defs)"] --> B["Send user message to LLM"]
    B --> C["LLM returns response"]
    C --> D{"Tool calls?"}
    D -- No --> E["COMPLETED ✅"]
    D -- Yes --> F{"Max iterations?"}
    F -- Yes --> G["TIMEOUT ⏱️"]
    F -- No --> H{"Token budget<br/>exceeded?"}
    H -- Yes --> G
    H -- No --> I["Execute tool calls<br/>in sandbox"]
    I --> J["Append results<br/>to conversation"]
    J --> C
```

## State Transitions

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> RUNNING : start
    RUNNING --> COMPLETED : task done
    RUNNING --> TIMEOUT : max iterations / tokens
    RUNNING --> FAILED : unrecoverable error
    RUNNING --> CANCELLED : user cancel

For the full technical specification, see [spec.md](spec.md).
```
