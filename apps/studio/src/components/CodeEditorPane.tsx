import { useEffect, useRef, type ReactNode } from "react";
import * as monaco from "monaco-editor";
import { ensureMonacoWorkers } from "../monacoWorkers";
import type { DiagnosticSeverity, EditorDiagnostic } from "../types/workspace";

export interface EditorRendererProps {
  path: string;
  value: string;
  language: string;
  diagnostics: EditorDiagnostic[];
  focusLine: number | null;
  onChange: (nextValue: string) => void;
  onSave: () => void;
}

export type EditorRenderer = (props: EditorRendererProps) => ReactNode;

interface CodeEditorPaneProps {
  path: string | null;
  value: string;
  language: string;
  diagnostics: EditorDiagnostic[];
  focusLine: number | null;
  onChange: (nextValue: string) => void;
  onSave: () => void;
  editorRenderer?: EditorRenderer;
}

function mapSeverity(severity: DiagnosticSeverity): monaco.MarkerSeverity {
  if (severity === "error") {
    return monaco.MarkerSeverity.Error;
  }
  if (severity === "warning") {
    return monaco.MarkerSeverity.Warning;
  }
  return monaco.MarkerSeverity.Info;
}

export function CodeEditorPane({
  path,
  value,
  language,
  diagnostics,
  focusLine,
  onChange,
  onSave,
  editorRenderer,
}: CodeEditorPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const suppressingRef = useRef(false);
  const onChangeRef = useRef(onChange);
  const onSaveRef = useRef(onSave);

  onChangeRef.current = onChange;
  onSaveRef.current = onSave;

  useEffect(() => {
    if (editorRenderer || !containerRef.current || path === null) {
      return;
    }
    ensureMonacoWorkers();
    const editor = monaco.editor.create(containerRef.current, {
      value,
      language,
      theme: "vs-dark",
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 13,
      tabSize: 2,
    });
    editorRef.current = editor;

    const changeSub = editor.onDidChangeModelContent(() => {
      if (!suppressingRef.current) {
        onChangeRef.current(editor.getValue());
      }
    });
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      onSaveRef.current();
    });

    return () => {
      changeSub.dispose();
      editor.dispose();
      editorRef.current = null;
    };
  }, [editorRenderer, path !== null]);

  useEffect(() => {
    const editor = editorRef.current;
    if (editorRenderer || !editor || path === null) {
      return;
    }
    const model = editor.getModel();
    if (!model) {
      return;
    }
    if (model.getLanguageId() !== language) {
      monaco.editor.setModelLanguage(model, language);
    }
    if (editor.getValue() !== value) {
      suppressingRef.current = true;
      editor.setValue(value);
      suppressingRef.current = false;
    }
  }, [editorRenderer, path, value, language]);

  useEffect(() => {
    const editor = editorRef.current;
    if (editorRenderer || !editor || path === null) {
      return;
    }
    const model = editor.getModel();
    if (!model) {
      return;
    }
    const markers: monaco.editor.IMarkerData[] = diagnostics.map((diag) => ({
      severity: mapSeverity(diag.severity),
      message: diag.message,
      source: diag.source ?? "agent-forge",
      startLineNumber: Math.max(1, diag.line),
      startColumn: Math.max(1, diag.column ?? 1),
      endLineNumber: Math.max(1, diag.endLine ?? diag.line),
      endColumn: Math.max(1, diag.endColumn ?? 120),
    }));
    monaco.editor.setModelMarkers(model, "agent-forge", markers);
  }, [editorRenderer, path, diagnostics]);

  useEffect(() => {
    const editor = editorRef.current;
    if (editorRenderer || !editor || focusLine === null) {
      return;
    }
    editor.revealLineInCenter(focusLine);
    editor.setPosition({ lineNumber: focusLine, column: 1 });
    editor.focus();
  }, [editorRenderer, focusLine]);

  if (path === null) {
    return (
      <div className="editor-empty">
        <p>Select a file in the Explorer or press Ctrl+P to open a workspace file.</p>
      </div>
    );
  }

  if (editorRenderer) {
    return (
      <div className="editor-host">
        {editorRenderer({ path, value, language, diagnostics, focusLine, onChange, onSave })}
      </div>
    );
  }

  return <div ref={containerRef} className="editor-host" data-testid="monaco-container" />;
}
