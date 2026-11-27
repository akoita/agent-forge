import { useMemo, useState, type KeyboardEvent } from "react";
import type { TimelineEntry } from "../hooks/useAcpSession";
import type {
  ACPConnectionState,
  AvailableCommand,
  PermissionRequest,
  PlanEntry,
  SessionConfigOption,
  SessionInfo,
} from "../types/acp";
import { ApprovalDialog } from "./ApprovalDialog";
import { SafeMarkdown } from "./SafeMarkdown";
import { ToolCallCard } from "./ToolCallCard";

interface AgentPanelProps {
  connected: boolean;
  connectionState: ACPConnectionState;
  sessions: SessionInfo[];
  activeSessionId: string | null;
  configOptions: SessionConfigOption[];
  availableCommands: AvailableCommand[];
  planEntries: PlanEntry[];
  timeline: TimelineEntry[];
  pendingPermission: PermissionRequest | null;
  promptRunning: boolean;
  agentError: string;
  onDismissError: () => void;
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
  onChangeConfig: (configId: string, value: string) => void;
  onSendPrompt: (text: string) => void;
  onCancelTurn: () => void;
  onRespondPermission: (requestId: number, optionId: string) => void;
  onOpenLocation: (path: string, line: number | null) => void;
}

export function AgentPanel({
  connected,
  connectionState,
  sessions,
  activeSessionId,
  configOptions,
  availableCommands,
  planEntries,
  timeline,
  pendingPermission,
  promptRunning,
  agentError,
  onDismissError,
  onSelectSession,
  onNewSession,
  onChangeConfig,
  onSendPrompt,
  onCancelTurn,
  onRespondPermission,
  onOpenLocation,
}: AgentPanelProps) {
  const [draft, setDraft] = useState("");
  const [selectedCmdIndex, setSelectedCmdIndex] = useState(0);
  const [menuDismissed, setMenuDismissed] = useState(false);

  const slashPrefix = useMemo(() => {
    const match = /^\/([^\s]*)$/.exec(draft);
    return match ? match[1].toLowerCase() : null;
  }, [draft]);

  const matchingCommands = useMemo(() => {
    if (slashPrefix === null || menuDismissed) {
      return [];
    }
    return availableCommands.filter((cmd) => cmd.name.toLowerCase().startsWith(slashPrefix));
  }, [availableCommands, menuDismissed, slashPrefix]);

  function applyCommand(cmd: AvailableCommand): void {
    setDraft(`/${cmd.name} `);
    setSelectedCmdIndex(0);
    setMenuDismissed(false);
  }

  function handleSend(): void {
    const trimmed = draft.trim();
    if (!trimmed || !activeSessionId || promptRunning) {
      return;
    }
    setDraft("");
    setSelectedCmdIndex(0);
    setMenuDismissed(false);
    onSendPrompt(trimmed);
  }

  function handleComposerKeyDown(ev: KeyboardEvent<HTMLTextAreaElement>): void {
    if (matchingCommands.length > 0) {
      const clampedIndex = Math.min(selectedCmdIndex, matchingCommands.length - 1);
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        setSelectedCmdIndex((clampedIndex + 1) % matchingCommands.length);
        return;
      }
      if (ev.key === "ArrowUp") {
        ev.preventDefault();
        setSelectedCmdIndex(
          (clampedIndex - 1 + matchingCommands.length) % matchingCommands.length,
        );
        return;
      }
      if (ev.key === "Tab" || (ev.key === "Enter" && !ev.shiftKey && slashPrefix !== null)) {
        ev.preventDefault();
        applyCommand(matchingCommands[clampedIndex]);
        return;
      }
      if (ev.key === "Escape") {
        ev.preventDefault();
        setMenuDismissed(true);
        return;
      }
    }

    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      handleSend();
    }
  }

  return (
    <aside className="studio-agent-slot" aria-label="ACP Agent Panel">
      <div className="agent-panel-header">
        <span>Agent (ACP)</span>
        <span className={`agent-conn-pill state-${connectionState}`}>
          {promptRunning ? "running" : connectionState}
        </span>
      </div>

      {agentError ? (
        <div className="agent-error-banner" role="alert">
          <span>{agentError}</span>
          <button type="button" className="btn-secondary" onClick={onDismissError}>
            Dismiss
          </button>
        </div>
      ) : null}

      <div className="agent-session-bar">
        <label htmlFor="acp-session-select" className="sr-only">
          ACP Session
        </label>
        <select
          id="acp-session-select"
          className="agent-select"
          value={activeSessionId ?? ""}
          disabled={!connected || promptRunning}
          onChange={(ev) => onSelectSession(ev.target.value)}
        >
          {!activeSessionId ? <option value="">No active session</option> : null}
          {activeSessionId && !sessions.some((s) => s.sessionId === activeSessionId) ? (
            <option value={activeSessionId}>{activeSessionId}</option>
          ) : null}
          {sessions.map((sess) => (
            <option key={sess.sessionId} value={sess.sessionId}>
              {sess.title || sess.sessionId}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn-secondary"
          disabled={!connected || promptRunning}
          onClick={onNewSession}
        >
          New
        </button>
      </div>

      {configOptions.length > 0 ? (
        <div className="agent-config-grid">
          {configOptions.map((opt) => (
            <label key={opt.id} className="agent-config-item">
              <span className="agent-config-label">{opt.name}</span>
              <select
                aria-label={opt.name}
                className="agent-select"
                value={opt.currentValue}
                disabled={!activeSessionId || promptRunning}
                onChange={(ev) => onChangeConfig(opt.id, ev.target.value)}
              >
                {opt.options.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.name}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      ) : null}

      {planEntries.length > 0 ? (
        <div className="agent-plan-box" aria-label="Execution Plan">
          <div className="tool-section-label">Plan</div>
          <ul className="agent-plan-list">
            {planEntries.map((step, idx) => (
              <li key={`${step.content}-${idx}`} className={`plan-item plan-${step.status}`}>
                <span className="plan-status-tag">{step.status}</span>
                <span>{step.content}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="agent-timeline">
        {timeline.length === 0 ? (
          <div className="empty-hint">
            {connected
              ? "Ask the agent to inspect or edit files, or type / for commands."
              : "Connect to the ACP bridge to start a coding session."}
          </div>
        ) : (
          timeline.map((entry) => {
            if (entry.kind === "user") {
              return (
                <div key={entry.id} className="timeline-msg timeline-user">
                  <div className="timeline-role">You</div>
                  <div className="timeline-plain">{entry.text}</div>
                </div>
              );
            }
            if (entry.kind === "assistant") {
              return (
                <div key={entry.id} className="timeline-msg timeline-assistant">
                  <div className="timeline-role">Agent</div>
                  <SafeMarkdown text={entry.text} />
                </div>
              );
            }
            if (entry.kind === "thought") {
              return (
                <details key={entry.id} className="timeline-thought">
                  <summary>Agent reasoning note</summary>
                  <div className="timeline-plain">{entry.text}</div>
                </details>
              );
            }
            return (
              <ToolCallCard key={entry.id} entry={entry} onOpenLocation={onOpenLocation} />
            );
          })
        )}
      </div>

      {pendingPermission ? (
        <ApprovalDialog request={pendingPermission} onRespond={onRespondPermission} />
      ) : null}

      <div className="agent-composer-wrap">
        {matchingCommands.length > 0 ? (
          <div className="slash-menu" role="listbox" aria-label="Slash commands">
            {matchingCommands.map((cmd, idx) => {
              const selected = idx === Math.min(selectedCmdIndex, matchingCommands.length - 1);
              return (
                <button
                  key={cmd.name}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  className={`slash-item${selected ? " is-selected" : ""}`}
                  onClick={() => applyCommand(cmd)}
                >
                  <span className="slash-name">
                    /{cmd.name}
                    {cmd.input?.hint ? ` ${cmd.input.hint}` : ""}
                  </span>
                  <span className="slash-desc">{cmd.description}</span>
                </button>
              );
            })}
          </div>
        ) : null}

        <label htmlFor="agent-prompt-input" className="sr-only">
          Agent prompt
        </label>
        <textarea
          id="agent-prompt-input"
          className="agent-composer-input"
          rows={3}
          placeholder={
            activeSessionId
              ? "Ask Agent Forge... (Enter to send, / for commands)"
              : "Connect to start a session"
          }
          value={draft}
          disabled={!connected || !activeSessionId}
          onChange={(ev) => {
            setDraft(ev.target.value);
            setMenuDismissed(false);
            setSelectedCmdIndex(0);
          }}
          onKeyDown={handleComposerKeyDown}
        />

        <div className="agent-composer-actions">
          {promptRunning ? (
            <button type="button" className="btn-danger" onClick={onCancelTurn}>
              Stop
            </button>
          ) : null}
          <button
            type="button"
            className="btn-primary"
            disabled={!connected || !activeSessionId || promptRunning || !draft.trim()}
            onClick={handleSend}
          >
            Send
          </button>
        </div>
      </div>
    </aside>
  );
}
