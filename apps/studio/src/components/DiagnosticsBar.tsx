import { useState } from "react";
import type { EditorDiagnostic, VerificationSummary } from "../types/workspace";

interface DiagnosticsBarProps {
  activePath: string | null;
  language: string;
  dirty: boolean;
  diagnostics: EditorDiagnostic[];
  statusMessage: string;
  verification?: VerificationSummary;
  terminalOpen?: boolean;
  onToggleTerminal?: () => void;
  onSelectDiagnostic: (diag: EditorDiagnostic) => void;
}

export function DiagnosticsBar({
  activePath,
  language,
  dirty,
  diagnostics,
  statusMessage,
  verification,
  terminalOpen = false,
  onToggleTerminal,
  onSelectDiagnostic,
}: DiagnosticsBarProps) {
  const [expanded, setExpanded] = useState(false);
  const errors = diagnostics.filter((d) => d.severity === "error").length;
  const warnings = diagnostics.filter((d) => d.severity === "warning").length;

  return (
    <footer className="studio-footer">
      {expanded && diagnostics.length > 0 ? (
        <div className="diagnostics-drawer" role="region" aria-label="Problems">
          {diagnostics.map((diag, idx) => (
            <button
              key={`${diag.path}-${diag.line}-${idx}`}
              type="button"
              className={`diagnostic-row diagnostic-${diag.severity}`}
              onClick={() => onSelectDiagnostic(diag)}
            >
              <span className="diagnostic-sev">{diag.severity.toUpperCase()}</span>
              <span className="diagnostic-loc">
                {diag.path}:{diag.line}
              </span>
              <span className="diagnostic-msg">{diag.message}</span>
            </button>
          ))}
        </div>
      ) : null}

      <div className="statusbar">
        <div className="statusbar-left">
          <button
            type="button"
            className="statusbar-btn"
            onClick={() => setExpanded((prev) => !prev)}
            aria-expanded={expanded}
          >
            <span>Errors: {errors}</span>
            <span>Warnings: {warnings}</span>
          </button>
          {verification ? (
            <span
              className={`verification-pill verification-${verification.status}`}
              title={verification.detail}
            >
              Verify: {verification.status}
            </span>
          ) : null}
          {onToggleTerminal ? (
            <button type="button" className="statusbar-btn" onClick={onToggleTerminal}>
              {terminalOpen ? "Terminal: Open" : "Terminal"}
            </button>
          ) : null}
          {statusMessage ? <span className="statusbar-note">{statusMessage}</span> : null}
        </div>
        <div className="statusbar-right">
          {activePath ? (
            <>
              <span>{activePath}</span>
              <span>{language}</span>
              <span>{dirty ? "Modified" : "Saved"}</span>
            </>
          ) : (
            <span>No active editor</span>
          )}
        </div>
      </div>
    </footer>
  );
}
