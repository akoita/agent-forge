# Agent Forge — Sandbox Base Image
# Pre-built image with common development tools for sandboxed tool execution.

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
