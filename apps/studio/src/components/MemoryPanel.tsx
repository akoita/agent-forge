import { useMemo, useState, type FormEvent } from "react";
import type { MemoryEntry } from "../types/workspace";

interface MemoryPanelProps {
  open: boolean;
  connected: boolean;
  memoryPath: string;
  entries: MemoryEntry[];
  loading: boolean;
  onAdd: (text: string, category: string) => Promise<void> | void;
  onRemove: (entryId: string) => Promise<void> | void;
  onClear: () => Promise<void> | void;
  onRefresh: () => Promise<void> | void;
  onOpenRawFile: (path: string) => void;
  onClose: () => void;
}

const CATEGORIES = ["general", "architecture", "testing", "conventions", "security"];

export function MemoryPanel({
  open,
  connected,
  memoryPath,
  entries,
  loading,
  onAdd,
  onRemove,
  onClear,
  onRefresh,
  onOpenRawFile,
  onClose,
}: MemoryPanelProps) {
  const [query, setQuery] = useState("");
  const [textInput, setTextInput] = useState("");
  const [categoryInput, setCategoryInput] = useState("general");
  const [confirmClear, setConfirmClear] = useState(false);

  const filtered = useMemo(() => {
    const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (terms.length === 0) {
      return entries;
    }
    return entries.filter((entry) => {
      const hay = `${entry.id} ${entry.category} ${entry.text}`.toLowerCase();
      return terms.every((term) => hay.includes(term));
    });
  }, [entries, query]);

  if (!open) {
    return null;
  }

  async function handleSubmit(ev: FormEvent<HTMLFormElement>): Promise<void> {
    ev.preventDefault();
    const cleaned = textInput.trim();
    if (!cleaned || loading) {
      return;
    }
    setTextInput("");
    await onAdd(cleaned, categoryInput);
  }

  return (
    <aside className="studio-memory-slot" aria-label="Repository Memory Panel">
      <div className="agent-panel-header">
        <span>Repo Memory</span>
        <button type="button" className="btn-secondary" onClick={onClose}>
          Close
        </button>
      </div>

      <div className="memory-toolbar">
        <button
          type="button"
          className="btn-secondary"
          disabled={!connected || loading}
          onClick={() => void onRefresh()}
        >
          Refresh
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={!connected}
          onClick={() => onOpenRawFile(memoryPath || ".agent-forge/memory.md")}
        >
          Open File
        </button>
        {entries.length > 0 ? (
          confirmClear ? (
            <>
              <button
                type="button"
                className="btn-danger"
                disabled={loading}
                onClick={() => {
                  setConfirmClear(false);
                  void onClear();
                }}
              >
                Confirm Clear
              </button>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setConfirmClear(false)}
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              className="btn-danger"
              disabled={!connected || loading}
              onClick={() => setConfirmClear(true)}
            >
              Clear All
            </button>
          )
        ) : null}
      </div>

      <form className="memory-add-form" onSubmit={(ev) => void handleSubmit(ev)}>
        <label htmlFor="memory-category-select" className="sr-only">
          Memory category
        </label>
        <select
          id="memory-category-select"
          className="agent-select"
          value={categoryInput}
          disabled={!connected || loading}
          onChange={(ev) => setCategoryInput(ev.target.value)}
        >
          {CATEGORIES.map((cat) => (
            <option key={cat} value={cat}>
              {cat}
            </option>
          ))}
        </select>

        <label htmlFor="memory-text-input" className="sr-only">
          Memory learning note
        </label>
        <input
          id="memory-text-input"
          type="text"
          className="token-input"
          placeholder="Add repo memory note..."
          value={textInput}
          disabled={!connected || loading}
          onChange={(ev) => setTextInput(ev.target.value)}
        />
        <button
          type="submit"
          className="btn-primary"
          disabled={!connected || loading || !textInput.trim()}
        >
          Add
        </button>
      </form>

      <div className="memory-search-wrap">
        <label htmlFor="memory-search-input" className="sr-only">
          Filter repository memory
        </label>
        <input
          id="memory-search-input"
          type="search"
          className="token-input"
          placeholder="Filter learnings..."
          value={query}
          onChange={(ev) => setQuery(ev.target.value)}
        />
      </div>

      <div className="memory-list">
        {filtered.length === 0 ? (
          <div className="empty-hint">No learnings stored in {memoryPath}.</div>
        ) : (
          filtered.map((entry) => (
            <div key={entry.id} className="memory-card">
              <div className="memory-card-top">
                <span className="tool-feature-badge">{entry.category}</span>
                <span className="memory-card-id">{entry.id}</span>
                <button
                  type="button"
                  className="btn-secondary memory-remove-btn"
                  disabled={loading}
                  aria-label={`Remove ${entry.id}`}
                  onClick={() => void onRemove(entry.id)}
                >
                  Remove
                </button>
              </div>
              <div className="memory-card-text">{entry.text}</div>
            </div>
          ))
        )}
      </div>
    </aside>
  );
}
