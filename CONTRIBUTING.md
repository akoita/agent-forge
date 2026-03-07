# Contributing to Agent Forge

Thank you for considering contributing to Agent Forge! This document provides guidelines and instructions for contributing.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [Style Guide](#style-guide)
- [Reporting Issues](#reporting-issues)

## Code of Conduct

This project adheres to the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Please report unacceptable behavior by opening an issue.

## Getting Started

1. **Fork** the repository on GitHub.
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/<your-username>/agent-forge.git
   cd agent-forge
   ```
3. **Create a branch** for your work:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Setup

### Prerequisites

- Python 3.11+
- Docker (for sandbox and integration tests)
- Redis (optional, for queue backend tests)

### Installation

```bash
# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in development mode with all extras
pip install -e ".[dev,redis]"

# Build the sandbox Docker image
make build-sandbox
```

### Running Tests

```bash
# Unit tests only (fast, no Docker needed)
make test-unit

# Integration tests (requires Docker)
make test-integration

# All tests with coverage report
make test

# Linting
make lint

# Auto-format
make format
```

## Making Changes

### Branch Naming

Use descriptive branch names with a prefix:

- `feature/` — New features (e.g., `feature/anthropic-adapter`)
- `fix/` — Bug fixes (e.g., `fix/sandbox-timeout-handling`)
- `docs/` — Documentation changes (e.g., `docs/update-readme`)
- `refactor/` — Code refactoring (e.g., `refactor/tool-registry`)
- `test/` — Test additions or fixes (e.g., `test/llm-retry-logic`)

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`

**Examples:**
```
feat(llm): add Anthropic provider adapter
fix(sandbox): handle container timeout gracefully
docs(readme): update installation instructions
test(agent): add ReAct loop termination tests
```

## Submitting a Pull Request

1. Ensure all tests pass: `make test`
2. Ensure linting passes: `make lint`
3. Update documentation if your change affects public interfaces.
4. Write a clear PR description explaining:
   - **What** the change does
   - **Why** it's needed
   - **How** it was tested
5. Link any related issues.

### PR Review Criteria

- [ ] Tests pass (unit + integration if applicable)
- [ ] Linting passes
- [ ] New code has appropriate test coverage
- [ ] Documentation is updated if needed
- [ ] Commit messages follow conventional commits

## Style Guide

### Python

- **Formatter:** [Ruff](https://github.com/astral-sh/ruff) (auto-format with `make format`)
- **Linter:** Ruff + [mypy](http://mypy-lang.org/) for type checking
- **Python version:** 3.11+ (use modern syntax: `X | Y` unions, `match` statements)
- **Docstrings:** Google style
- **Type hints:** Required on all public functions and methods

### Code Principles

- Prefer **explicit over implicit**.
- Use **async/await** for I/O-bound operations.
- All tools must implement the `Tool` ABC.
- All LLM providers must implement the `LLMProvider` ABC.
- Keep modules focused — one responsibility per file.

## Reporting Issues

Use the [GitHub Issues](https://github.com/akoita/agent-forge/issues) page. Please include:

1. **Environment:** Python version, OS, Docker version.
2. **Steps to reproduce** the issue.
3. **Expected behavior** vs. **actual behavior**.
4. **Logs or error output** (sanitize any API keys!).

Thank you for helping make Agent Forge better! 🛠️
