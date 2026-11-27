# Agent Forge Studio (`apps/studio/`)

Lightweight React + TypeScript + Monaco Editor shell connecting to Agent Forge's authenticated ACP WebSocket and workspace companion bridge (`agent-forge acp --transport ws`).

## Run Locally

1. Build once (`npm run build`) and launch Studio + bridge in one command from the repository root:

```bash
export AGENT_FORGE_ACP_TOKEN="dev-token"
agent-forge studio --workspace .
```

Or run the Vite dev server alongside `agent-forge acp --transport ws --host 127.0.0.1 --port 8765 --workspace .` in `apps/studio/`:

```bash
npm install
npm run dev
```

Open `http://127.0.0.1:8765` (built launcher) or `http://127.0.0.1:5173` (Vite dev proxy) and connect with `AGENT_FORGE_ACP_TOKEN`. Vite proxies `/auth`, `/workspace`, and `/ws` directly to `http://127.0.0.1:8765`.

## Features

- **Workspace & Monaco Editor (`#237`):** Explorer tree with Git badges, multi-tab editing, Save/Revert, Quick Open (`Ctrl+P`), and diagnostics bar.
- **ACP Streaming Agent & Approvals (`#238`):** Typed WebSocket ACP client (`/ws/acp`), `session/list` + `session/load` replay, runtime config selectors (`Mode`, `Model`, `Provider`, `Approvals`, `Execution`), pure-JSX streaming markdown, collapsible `Code Mode` / `Subagent` tool cards with clickable workspace locations, slash-command autocomplete (`available_commands_update`), and interactive `Allow once` / `Always allow category` / `Reject` permission dialogs.
- **Diff Review, Terminal, Memory & Launcher (`#239`):** Side-by-side Monaco `DiffEditor` review with Apply/Undo, integrated `@xterm/xterm` PTY terminal (`/ws/terminal`), verification status indicator, `.agent-forge/memory.md` Memory panel, and `agent-forge studio` local launcher.

## Checks

```bash
npm audit --audit-level=high
npm run typecheck
npm run test
npm run build
npx playwright install chromium && npm run test:e2e
```
