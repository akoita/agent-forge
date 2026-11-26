# Connect Editors and Clients via ACP

Use `agent-forge acp` to drive Agent Forge from any [Agent Client Protocol (ACP)](https://agentclientprotocol.com/get-started/introduction) editor over `stdio`, or from browser clients over the authenticated WebSocket companion bridge.

## Prerequisites

- **Runtime:** Python 3.11+ with `uv` (`uvx`) or `pip install agent-forge` (add `[service]` for `--transport ws`).
- **Provider credentials:** Export at least one LLM key (for example `GEMINI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`), or configure a local `openai-compatible` server.
- **Sandbox:** Docker running locally (optional if configuring `--sandbox-backend bubblewrap`).

## Editor Setup

### Zed

Add Agent Forge under `agent_servers` in `~/.config/zed/settings.json`:

```json
{
  "agent_servers": {
    "Agent Forge": {
      "command": "uvx",
      "args": ["agent-forge==0.8.0", "acp", "--approval-mode", "prompt"],
      "env": {}
    }
  }
}
```

### JetBrains IDEs

Register Agent Forge in JetBrains AI Chat / ACP custom agent settings using the bundled [agent.json](../agent.json) manifest or the `uvx` launch command directly:

```json
{
  "id": "agent-forge",
  "command": "uvx",
  "args": ["agent-forge==0.8.0", "acp", "--approval-mode", "prompt"]
}
```

### Neovim

Point any Neovim ACP plugin (`codecompanion.nvim`, `avante.nvim` ACP adapter, or custom RPC client) to launch `agent-forge acp`:

```lua
acp = {
  command = { "uvx", "agent-forge==0.8.0", "acp", "--approval-mode", "prompt" },
}
```

### Browser / Remote Studio Bridge

Install `agent-forge[service]`, export a bearer token (`AGENT_FORGE_ACP_TOKEN`), and start the loopback WebSocket bridge scoped to your repository:

```bash
export AGENT_FORGE_ACP_TOKEN="replace-with-strong-token"
agent-forge acp --transport ws --host 127.0.0.1 --port 8765 --workspace .
```

The bridge serves `WS /ws/acp`, `POST /auth/session`, `GET /workspace/tree`, `GET|POST /workspace/file`, `GET /workspace/git/status|diff`, `WS /ws/workspace/watch`, and `WS /ws/terminal`. Never pass tokens in URL query parameters; send `Authorization: Bearer <token>` or exchange it for a `HttpOnly` cookie session via `POST /auth/session`.

## Verify `stdio` Interop

Run a quick JSON-RPC handshake and session creation check from the repository root:

```bash
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}' \
  '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"."}}' \
  | agent-forge acp --sessions-dir /tmp/af-acp-test
```

Expect three newline-delimited JSON objects on `stdout`: the `initialize` response (`protocolVersion`, `agentCapabilities`), the `session/new` response (`sessionId`, `modes`, `configOptions`), and an `available_commands_update` notification.

## Troubleshooting

- **Missing API key error during `session/prompt`:** Export the key configured for your active provider (or switch provider/model via `session/set_config_option`).
- **Non-JSON output in custom wrappers:** `agent-forge acp` writes logs and `sys.stdout` redirects to `stderr`; keep `stdout` untouched.
- **Sessions lost after editor restart:** Pass `--sessions-dir <path>` or rely on the default `~/.agent-forge/sessions` directory, then reconnect using `session/list` and `session/load`.
- **Missing WebSocket dependencies:** Install `pip install 'agent-forge[service]'` before running `--transport ws`.
