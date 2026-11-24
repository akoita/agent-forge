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
  go)
    dockerfile="$PROJECT_ROOT/agent_forge/sandbox/Dockerfile.go"
    image_tag="agent-forge-sandbox:go"
    ;;
  rust)
    dockerfile="$PROJECT_ROOT/agent_forge/sandbox/Dockerfile.rust"
    image_tag="agent-forge-sandbox:rust"
    ;;
  jvm)
    dockerfile="$PROJECT_ROOT/agent_forge/sandbox/Dockerfile.jvm"
    image_tag="agent-forge-sandbox:jvm"
    ;;
  full)
    dockerfile="$PROJECT_ROOT/agent_forge/sandbox/Dockerfile.full"
    image_tag="agent-forge-sandbox:full"
    ;;
  universal)
    dockerfile="$PROJECT_ROOT/agent_forge/sandbox/Dockerfile.universal"
    image_tag="agent-forge-sandbox:universal"
    ;;
  *)
    echo "Unknown sandbox variant: $variant" >&2
    echo "Usage: $0 [python|node|go|rust|jvm|full|universal]" >&2
    exit 1
    ;;
esac

echo "Building sandbox image $image_tag..."
docker build \
    -t "$image_tag" \
    -f "$dockerfile" \
    "$PROJECT_ROOT"

echo "✅ Sandbox image built: $image_tag"
