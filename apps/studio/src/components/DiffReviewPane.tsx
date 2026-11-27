import { useEffect, useRef, type ReactNode } from "react";
import * as monaco from "monaco-editor";
import { ensureMonacoWorkers } from "../monacoWorkers";
import type { PendingDiffItem } from "../types/workspace";
import { detectLanguage } from "../utils/fuzzy";

export interface DiffRendererProps {
  diff: PendingDiffItem;
  onApply: (diff: PendingDiffItem) => void;
  onUndo: (diff: PendingDiffItem) => void;
}

export type DiffRenderer = (props: DiffRendererProps) => ReactNode;

interface DiffReviewPaneProps {
  diffs: PendingDiffItem[];
  activeDiffId: string | null;
  applying: boolean;
  onSelectDiff: (diffId: string) => void;
  onApplyDiff: (diff: PendingDiffItem) => Promise<void> | void;
  onUndoDiff: (diff: PendingDiffItem) => Promise<void> | void;
  onClose: () => void;
  diffRenderer?: DiffRenderer;
}

export function DiffReviewPane({
  diffs,
  activeDiffId,
  applying,
  onSelectDiff,
  onApplyDiff,
  onUndoDiff,
  onClose,
  diffRenderer,
}: DiffReviewPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const diffEditorRef = useRef<monaco.editor.IStandaloneDiffEditor | null>(null);
  const modelsRef = useRef<{
    original: monaco.editor.ITextModel;
    modified: monaco.editor.ITextModel;
  } | null>(null);

  const activeDiff =
    diffs.find((item) => item.id === activeDiffId) ?? diffs[diffs.length - 1] ?? null;

  useEffect(() => {
    if (diffRenderer || !containerRef.current || !activeDiff) {
      return;
    }
    ensureMonacoWorkers();
    const diffEditor = monaco.editor.createDiffEditor(containerRef.current, {
      readOnly: true,
      renderSideBySide: true,
      theme: "vs-dark",
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 13,
    });
    diffEditorRef.current = diffEditor;

    return () => {
      if (modelsRef.current) {
        diffEditor.setModel(null);
        modelsRef.current.original.dispose();
        modelsRef.current.modified.dispose();
        modelsRef.current = null;
      }
      diffEditor.dispose();
      diffEditorRef.current = null;
    };
  }, [diffRenderer, activeDiff !== null]);

  useEffect(() => {
    const diffEditor = diffEditorRef.current;
    if (diffRenderer || !diffEditor || !activeDiff) {
      return;
    }
    const language = detectLanguage(activeDiff.path);
    const original = monaco.editor.createModel(activeDiff.oldText, language);
    const modified = monaco.editor.createModel(activeDiff.newText, language);
    const previous = modelsRef.current;
    modelsRef.current = { original, modified };
    diffEditor.setModel({ original, modified });
    if (previous) {
      previous.original.dispose();
      previous.modified.dispose();
    }
  }, [diffRenderer, activeDiff]);

  if (!activeDiff) {
    return null;
  }

  return (
    <section className="diff-review-pane" aria-label="Diff Review">
      <div className="diff-review-bar">
        <div className="diff-review-meta">
          <span className="diff-badge">Diff Review</span>
          <span className="diff-path">{activeDiff.path}</span>
          {diffs.length > 1 ? (
            <select
              aria-label="Select pending diff"
              className="agent-select"
              value={activeDiff.id}
              onChange={(ev) => onSelectDiff(ev.target.value)}
            >
              {diffs.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.path} ({item.title})
                </option>
              ))}
            </select>
          ) : null}
        </div>
        <div className="diff-review-actions">
          <button
            type="button"
            className="btn-primary"
            disabled={applying || activeDiff.applied}
            onClick={() => void onApplyDiff(activeDiff)}
          >
            {activeDiff.applied ? "Applied" : "Apply Diff"}
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={applying}
            onClick={() => void onUndoDiff(activeDiff)}
          >
            Undo Diff
          </button>
          <button type="button" className="btn-secondary" onClick={onClose}>
            Close Diff
          </button>
        </div>
      </div>

      {diffRenderer ? (
        <div className="diff-host">
          {diffRenderer({
            diff: activeDiff,
            onApply: (d) => void onApplyDiff(d),
            onUndo: (d) => void onUndoDiff(d),
          })}
        </div>
      ) : (
        <div ref={containerRef} className="diff-host" data-testid="monaco-diff-container" />
      )}
    </section>
  );
}
