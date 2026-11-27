# Agent Forge Studio (`apps/studio/`)

Lightweight React + TypeScript + Monaco Editor shell connecting to Agent Forge's authenticated ACP WebSocket and workspace companion bridge (`agent-forge acp --transport ws`).

## Run Locally

1. Start the Python ACP/workspace bridge from the repository root:

```bash
export AGENT_FORGE_ACP_TOKEN="dev-token"
agent-forge acp --transport ws --host 127.0.0.1 --port 8765 --workspace .
```

2. Start Studio in `apps/studio/`:

```bash
npm install
npm run dev
```

Open `http://127.0.0.1:5173` and connect with `AGENT_FORGE_ACP_TOKEN`. Vite proxies `/auth`, `/workspace`, and `/ws` directly to `http://127.0.0.1:8765`.

## Checks

```bash
npm run typecheck
npm run test
npm run build
```
