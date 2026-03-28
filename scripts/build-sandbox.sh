#!/usr/bin/env bash
# Build the Agent Forge sandbox Docker image.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

variant="${1:-python}"

case "$variant" in
  python)
    dockerfile="$PROJECT_ROOT/agent_forge/sandbox/Dockerfile"
    image_tag="agent-forge-sandbox:latest"
    ;;
  node)
    dockerfile="$PROJECT_ROOT/agent_forge/sandbox/Dockerfile.node"
    image_tag="agent-forge-sandbox:node"
    ;;
  full)
    dockerfile="$PROJECT_ROOT/agent_forge/sandbox/Dockerfile.full"
    image_tag="agent-forge-sandbox:full"
    ;;
  *)
    echo "Unknown sandbox variant: $variant" >&2
    echo "Usage: $0 [python|node|full]" >&2
    exit 1
    ;;
esac

echo "Building sandbox image $image_tag..."
docker build \
    -t "$image_tag" \
    -f "$dockerfile" \
    "$PROJECT_ROOT"

echo "✅ Sandbox image built: $image_tag"
