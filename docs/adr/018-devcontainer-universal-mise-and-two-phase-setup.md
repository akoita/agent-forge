# ADR-018: Repo-Defined Devcontainers, Universal `mise` Image, and Two-Phase `setup_command`

- **Status:** Accepted
- **Date:** 2026-11-24
- **Issue:** `#203`

## Context

Per-language images cannot cover every language version matrix or project-specific OS library without combinatorial Dockerfile proliferation. Many repositories already declare their environment in `.devcontainer/devcontainer.json` or a repo Dockerfile, or pin mixed tool versions with `mise` / `.tool-versions`. Running agent turns with unrestricted internet access solely to install dependencies would also weaken the `--network none` security boundary.

## Decision

1. **Repo-defined devcontainers and Dockerfiles (`agent_forge/sandbox/devcontainer.py`):**
   - `discover_devcontainer` parses JSONC `.devcontainer/devcontainer.json` or `.devcontainer.json`, resolving `image` or `build` (`dockerfile`, `context`, `args`, `target`), `containerEnv` (sanitized via `sanitize_sandbox_env`), and `postCreateCommand`.
   - `resolve_dockerfile_spec` and `ensure_dockerfile_image` validate that repo Dockerfiles and build contexts do not escape the repo root, tag images deterministically (`agent-forge-repo:<sha256[:12]>`), and build on cache miss via the Docker SDK.
   - Precedence in `build_sandbox_config`: explicit `sandbox.dockerfile` > custom `sandbox.image` or explicit `sandbox.toolchain != "auto"` > `.devcontainer` > automatic ecosystem detection.
2. **Universal `mise` runtime (`agent_forge/sandbox/Dockerfile.universal`):**
   - Provide `agent-forge-sandbox:universal` with `debian:bookworm-slim`, `build-essential`, `mise`, and `/cache/mise` shims.
   - `detect_toolchain` selects `"universal"` when `mise.toml`, `.mise.toml`, or `.tool-versions` is present.
3. **Two-phase `sandbox.setup_command`:**
   - When `sandbox.setup_command` is set, both `DockerSandbox` and `BwrapSandbox` execute Phase 1 in an ephemeral online environment with an ephemeral host `/cache` directory mounted (`HOME=/cache/home`, `MISE_DATA_DIR=/cache/mise`, etc.), then tear down the Phase 1 process/container before starting Phase 2 strictly offline (`--network none` / `--unshare-net`) with that populated `/cache` mounted.
   - `VerificationWorkspace.verify` clears `setup_command` so independent patch verification remains offline and uncontaminated.

## Consequences

- Arbitrary languages and pinned versions work either via repo devcontainers/Dockerfiles or dynamic `mise` installation in `Dockerfile.universal` without rebuilding wrapper images.
- Agent execution remains offline whenever `network_enabled=False`, even when dependency/toolchain bootstrapping required internet access in Phase 1.
