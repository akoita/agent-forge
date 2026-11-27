import { useState } from "react";
import type {
  ToolCallContentBlock,
  ToolCallKind,
  ToolCallLocation,
  ToolCallStatus,
} from "../types/acp";

export interface ToolCallEntry {
  kind: "tool";
  id: string;
  toolCallId: string;
  title: string;
  toolKind: ToolCallKind;
  status: ToolCallStatus;
  rawInput?: unknown;
  content: ToolCallContentBlock[];
  locations: ToolCallLocation[];
  rawOutput?: unknown;
}

interface ToolCallCardProps {
  entry: ToolCallEntry;
  onOpenLocation: (path: string, line: number | null) => void;
}

function formatJson(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function detectBadge(title: string): "Code Mode" | "Subagent" | null {
  const lower = title.toLowerCase();
  if (lower.includes("[code-mode]") || title === "execute_code" || title === "search_tools") {
    return "Code Mode";
  }
  if (lower.includes("subagent") || title === "spawn_subagent") {
    return "Subagent";
  }
  return null;
}

export function ToolCallCard({ entry, onOpenLocation }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);
  const badge = detectBadge(entry.title);

  return (
    <div className={`tool-card tool-status-${entry.status}`}>
      <button
        type="button"
        className="tool-card-header"
        aria-expanded={expanded}
        onClick={() => setExpanded((prev) => !prev)}
      >
        <span className="tool-card-title-group">
          <span className="tool-chevron">{expanded ? "▾" : "▸"}</span>
          <span className="tool-title">{entry.title}</span>
          {badge ? <span className="tool-feature-badge">{badge}</span> : null}
        </span>
        <span className="tool-card-meta">
          <span className="tool-kind-pill">{entry.toolKind}</span>
          <span className={`tool-status-pill status-${entry.status}`}>{entry.status}</span>
        </span>
      </button>

      {entry.locations.length > 0 ? (
        <div className="tool-locations">
          {entry.locations.map((loc, idx) => (
            <button
              key={`${loc.path}-${loc.line ?? 0}-${idx}`}
              type="button"
              className="tool-location-link"
              onClick={() => onOpenLocation(loc.path, loc.line ?? null)}
            >
              {loc.path}
              {loc.line ? `:${loc.line}` : ""}
            </button>
          ))}
        </div>
      ) : null}

      {expanded ? (
        <div className="tool-card-body">
          {entry.rawInput !== undefined && entry.rawInput !== null ? (
            <div className="tool-section">
              <div className="tool-section-label">Input</div>
              <pre className="tool-pre">{formatJson(entry.rawInput)}</pre>
            </div>
          ) : null}

          {entry.content.map((block, idx) =>
            block.type === "diff" ? (
              <div key={`diff-${idx}`} className="tool-section">
                <div className="tool-section-label">Diff: {block.path}</div>
                <pre className="tool-pre">
                  {block.oldText ? `--- ${block.path}\n${block.oldText}\n` : ""}
                  {`+++ ${block.path}\n${block.newText}`}
                </pre>
              </div>
            ) : (
              <div key={`content-${idx}`} className="tool-section">
                <div className="tool-section-label">Output</div>
                <pre className="tool-pre">{block.content.text}</pre>
              </div>
            ),
          )}

          {entry.content.length === 0 &&
          entry.rawOutput !== undefined &&
          entry.rawOutput !== null ? (
            <div className="tool-section">
              <div className="tool-section-label">Output</div>
              <pre className="tool-pre">{formatJson(entry.rawOutput)}</pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
