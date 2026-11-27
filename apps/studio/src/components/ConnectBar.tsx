import { useState, type FormEvent } from "react";

interface ConnectBarProps {
  connected: boolean;
  connecting: boolean;
  branch: string;
  onConnect: (token: string) => Promise<void>;
  onDisconnect: () => Promise<void>;
  onRefresh: () => Promise<void>;
  onOpenQuickSearch: () => void;
  agentPanelOpen: boolean;
  onToggleAgentPanel: () => void;
}

export function ConnectBar({
  connected,
  connecting,
  branch,
  onConnect,
  onDisconnect,
  onRefresh,
  onOpenQuickSearch,
  agentPanelOpen,
  onToggleAgentPanel,
}: ConnectBarProps) {
  const [tokenInput, setTokenInput] = useState("");

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const submitted = tokenInput;
    setTokenInput("");
    await onConnect(submitted);
  }

  return (
    <header className="studio-topbar">
      <div className="studio-brand">
        <span className="studio-brand-title">Agent Forge Studio</span>
        {branch ? <span className="studio-branch-pill">{branch}</span> : null}
      </div>

      <div className="studio-topbar-actions">
        {connected ? (
          <>
            <button type="button" className="btn-secondary" onClick={onOpenQuickSearch}>
              Quick Open (Ctrl+P)
            </button>
            <button type="button" className="btn-secondary" onClick={() => void onRefresh()}>
              Refresh
            </button>
            <button type="button" className="btn-secondary" onClick={onToggleAgentPanel}>
              {agentPanelOpen ? "Hide Agent" : "Show Agent"}
            </button>
            <button type="button" className="btn-danger" onClick={() => void onDisconnect()}>
              Disconnect
            </button>
          </>
        ) : (
          <form className="studio-connect-form" onSubmit={(ev) => void handleSubmit(ev)}>
            <label htmlFor="bridge-token-input" className="sr-only">
              ACP Bridge Token
            </label>
            <input
              id="bridge-token-input"
              type="password"
              value={tokenInput}
              onChange={(ev) => setTokenInput(ev.target.value)}
              placeholder="AGENT_FORGE_ACP_TOKEN"
              autoComplete="off"
              className="token-input"
            />
            <button type="submit" className="btn-primary" disabled={connecting}>
              {connecting ? "Connecting..." : "Connect"}
            </button>
          </form>
        )}
      </div>
    </header>
  );
}
