import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker.js?worker";
import CssWorker from "monaco-editor/esm/vs/language/css/css.worker.js?worker";
import HtmlWorker from "monaco-editor/esm/vs/language/html/html.worker.js?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker.js?worker";
import TsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker.js?worker";

interface MonacoGlobalScope {
  MonacoEnvironment?: {
    getWorker(_moduleId: string, label: string): Worker;
  };
}

let configured = false;

export function ensureMonacoWorkers(): void {
  if (configured || typeof window === "undefined") {
    return;
  }
  configured = true;
  const globalObj = self as unknown as MonacoGlobalScope;
  globalObj.MonacoEnvironment = {
    getWorker(_moduleId: string, label: string): Worker {
      if (label === "json") {
        return new JsonWorker();
      }
      if (label === "css" || label === "scss" || label === "less") {
        return new CssWorker();
      }
      if (label === "html" || label === "handlebars" || label === "razor") {
        return new HtmlWorker();
      }
      if (label === "typescript" || label === "javascript") {
        return new TsWorker();
      }
      return new EditorWorker();
    },
  };
}
