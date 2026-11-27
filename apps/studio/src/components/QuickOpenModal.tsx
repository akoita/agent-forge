import { useEffect, useMemo, useState, type KeyboardEvent } from "react";
import type { TreeEntry } from "../types/workspace";
import { fuzzyFilterFiles } from "../utils/fuzzy";

interface QuickOpenModalProps {
  open: boolean;
  entries: TreeEntry[];
  onClose: () => void;
  onSelectFile: (path: string) => void;
}

export function QuickOpenModal({ open, entries, onClose, onSelectFile }: QuickOpenModalProps) {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);

  const matches = useMemo(() => fuzzyFilterFiles(entries, query, 40), [entries, query]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSelectedIndex(0);
    }
  }, [open]);

  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  if (!open) {
    return null;
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>): void {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelectedIndex((idx) => (matches.length === 0 ? 0 : (idx + 1) % matches.length));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelectedIndex((idx) =>
        matches.length === 0 ? 0 : (idx - 1 + matches.length) % matches.length,
      );
      return;
    }
    if (event.key === "Enter" && matches[selectedIndex]) {
      event.preventDefault();
      onSelectFile(matches[selectedIndex].path);
      onClose();
    }
  }

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Quick Open"
      onClick={onClose}
    >
      <div className="quick-open-card" onClick={(ev) => ev.stopPropagation()}>
        <input
          type="text"
          className="quick-open-input"
          autoFocus
          value={query}
          onChange={(ev) => setQuery(ev.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type file name or path to open..."
          aria-label="Search files"
        />
        <div className="quick-open-results" role="listbox">
          {matches.length === 0 ? (
            <div className="empty-hint">No matching files</div>
          ) : (
            matches.map((entry, idx) => (
              <button
                key={entry.path}
                type="button"
                role="option"
                aria-selected={idx === selectedIndex}
                className={`quick-open-item${idx === selectedIndex ? " is-selected" : ""}`}
                onClick={() => {
                  onSelectFile(entry.path);
                  onClose();
                }}
              >
                <span className="quick-open-name">{entry.name}</span>
                <span className="quick-open-path">{entry.path}</span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
