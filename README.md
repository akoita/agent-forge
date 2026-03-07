# Agent Forge

> A sandboxed AI coding agent runtime that autonomously modifies codebases through LLM-driven reasoning and isolated tool execution.

[![CI](https://github.com/akoita/agent-forge/actions/workflows/ci.yml/badge.svg)](https://github.com/akoita/agent-forge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

---

## Overview

Agent Forge implements the **ReAct** (Reasoning + Acting) pattern: an agent receives a coding task, iteratively reasons about what to do via an LLM, invokes tools inside **ephemeral Docker containers**, and loops until the task is complete.

```
┌─────────────────────────────────────────────────┐
│                   Agent Forge                    │
├─────────────────┬───────────────────────────────┤
│   Agent Core    │        Tool Registry          │
│  (ReAct loop)   │  (file ops, shell, search)    │
├─────────────────┼───────────────────────────────┤
│   LLM Client    │     Sandbox Runtime           │
│ (Gemini primary │  (Docker container per task)  │
│  + OpenAI/etc.) │                               │
├─────────────────┴───────────────────────────────┤
│              Orchestration Layer                 │
│  (task queue, state machine, event streaming)    │
├─────────────────────────────────────────────────┤
│              Observability                       │
│  (structured logs, trace IDs, token tracking)    │
└─────────────────────────────────────────────────┘
```

### Key Features

- **🔒 Sandboxed Execution** — Every tool invocation runs in an ephemeral Docker container with resource limits — never on the host.
- **🔌 Multi-Provider LLM** — Gemini (primary), OpenAI, and Anthropic via a unified adapter layer.
- **📊 Production Observability** — Structured JSON logs, trace IDs, token/cost tracking on every run.
- **⚡ Queue-Based Scaling** — Redis task queue for concurrent, isolated agent runs.
- **🧩 Extensible** — Add new tools or LLM providers by implementing a simple interface.

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker
- A Gemini API key (or OpenAI/Anthropic)

### Installation

```bash
# Clone the repository
git clone https://github.com/akoita/agent-forge.git
cd agent-forge

# Install in development mode
pip install -e ".[dev]"

# Build the sandbox Docker image
make build-sandbox

# Set your API key
export GEMINI_API_KEY="your-key-here"
```

### Usage

```bash
# Run an agent task
agent-forge run \
  --task "Add input validation to the /api/users endpoint" \
  --repo ./path/to/your/repo

# Check run status
agent-forge status <run-id>

# List recent runs
agent-forge list
```

---

## Configuration

Agent Forge uses a layered configuration system (CLI flags > env vars > project config > user config > defaults).

Create an `agent-forge.toml` in your project root:

```toml
[agent]
max_iterations = 25
default_provider = "gemini"
default_model = "gemini-3.1-flash-lite"

[sandbox]
memory_limit = "512m"
timeout_seconds = 300
network_enabled = false
```

See the [full specification](spec.md) for all configuration options.

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev,redis]"

# Run unit tests
make test-unit

# Run all tests (requires Docker)
make test

# Lint & format
make lint
make format
```

### Project Structure

```
agent_forge/
├── llm/           # LLM provider adapters (Gemini, OpenAI, Anthropic)
├── tools/         # Built-in tools (read_file, write_file, run_shell, etc.)
├── sandbox/       # Docker sandbox management
├── agent/         # ReAct loop, state machine, prompts
├── orchestration/ # Task queue, event bus, workers
└── observability/ # Structured logging, tracing, cost tracking
```

---

## Documentation

- **[Architecture](docs/architecture.md)** — System design, layer responsibilities, ReAct loop sequence.
- **[Configuration](docs/configuration.md)** — Full config reference (TOML, env vars, CLI flags, precedence).
- **[Testing](docs/testing.md)** — Running tests, writing new ones, CI workflows, coverage.
- **[Extending](docs/extending.md)** — Adding tools, LLM providers, custom sandbox configs.
- **[Technical Spec](spec.md)** — Full specification with interface contracts and data models.

---

## Roadmap

| Phase | Focus                                                             | Status         |
| ----- | ----------------------------------------------------------------- | -------------- |
| **1** | Core Agent MVP — ReAct loop + Docker sandbox + CLI                | 🚧 In Progress |
| **2** | Production Hardening — Observability, multi-provider, Redis queue | ⬜ Planned     |
| **3** | Git-Aware Agent & Plugin System                                   | ⬜ Planned     |
| **4** | Web Dashboard & REST API                                          | ⬜ Planned     |
| **5** | Multi-Agent Collaboration                                         | ⬜ Planned     |
| **6** | Advanced Isolation & Scaling (microVMs, K8s)                      | ⬜ Planned     |
| **7** | Platform & Ecosystem (MCP, marketplace, IDE plugins)              | ⬜ Planned     |

See [spec.md § Roadmap](spec.md#12-roadmap) for detailed milestones.

---

## Contributing

Contributions are welcome! Please read our [Contributing Guide](CONTRIBUTING.md) and [Code of Conduct](CODE_OF_CONDUCT.md) before submitting a pull request.

---

## Security

If you discover a security vulnerability, please follow our [Security Policy](SECURITY.md) for responsible disclosure.

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
