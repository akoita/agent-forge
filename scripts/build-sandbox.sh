#!/usr/bin/env bash
# Build the Agent Forge sandbox Docker image.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Building sandbox image..."
docker build \
    -t agent-forge-sandbox:latest \
    -f "$PROJECT_ROOT/agent_forge/sandbox/Dockerfile" \
    "$PROJECT_ROOT"

echo "✅ Sandbox image built: agent-forge-sandbox:latest"
