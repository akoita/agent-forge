import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { StudioApp } from "../App";
import type { EditorRendererProps } from "../components/CodeEditorPane";
import type { TreeEntry, WorkspaceTransport } from "../types/workspace";

function TestEditorWidget({ path: filePath, value, diagnostics, onChange, onSave }: EditorRendererProps) {
  return (
    <div data-testid="mock-monaco">
      <div data-testid="editor-meta">
        {filePath} (markers: {diagnostics.length})
      </div>
      <textarea
        aria-label="Code Editor"
        value={value}
        onChange={(ev) => onChange(ev.target.value)}
        onKeyDown={(ev) => {
          if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
            ev.preventDefault();
            onSave();
          }
        }}
      />
    </div>
  );
}

function createFakeTransport(): WorkspaceTransport & { files: Map<string, string> } {
  const files = new Map<string, string>([
    ["src/main.py", "print('hello')\n"],
    ["src/helper.py", "VALUE = 1\n"],
  ]);
  const tree: TreeEntry[] = [
    { path: "src", name: "src", kind: "directory", size: 0, mtime_ns: 1 },
    { path: "src/main.py", name: "main.py", kind: "file", size: 15, mtime_ns: 1 },
    { path: "src/helper.py", name: "helper.py", kind: "file", size: 10, mtime_ns: 1 },
  ];

  return {
    files,
    connectWithToken: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
    listTree: vi.fn(async () => tree),
    readFile: vi.fn(async (targetPath: string) => ({
      path: targetPath,
      content: files.get(targetPath) ?? "",
      size: (files.get(targetPath) ?? "").length,
      mtime_ns: 1,
      encoding: "utf-8",
    })),
    writeFile: vi.fn(async (targetPath: string, content: string) => {
      files.set(targetPath, content);
      return { path: targetPath, size: content.length, mtime_ns: 2 };
    }),
    getGitStatus: vi.fn(async () => ({
      branch: "feat/237-studio-web-shell",
      entries: [
        { index: " ", worktree: "M", path: "src/main.py" },
        { index: "?", worktree: "?", path: "src/helper.py" },
      ],
    })),
    getGitDiff: vi.fn(async () => ({ diff: "", truncated: false })),
  };
}

describe("StudioApp web code editor shell", () => {
  it("connects, renders tree/git badges, edits multi-tab files, supports Quick Open and diagnostics", async () => {
    const user = userEvent.setup();
    const transport = createFakeTransport();

    render(
      <StudioApp
        transport={transport}
        initialDiagnostics={[
          {
            path: "src/helper.py",
            line: 1,
            severity: "error",
            message: "Expected two blank lines",
          },
        ]}
        editorRenderer={(props) => <TestEditorWidget {...props} />}
      />,
    );

    const tokenBox = screen.getByLabelText("ACP Bridge Token");
    await user.type(tokenBox, "fixture-secret");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => {
      expect(screen.getByText("feat/237-studio-web-shell")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /main\.py/i })).toHaveTextContent("M");
    expect(screen.getByRole("button", { name: /helper\.py/i })).toHaveTextContent("U");

    // Open src/main.py and edit
    await user.click(screen.getByRole("button", { name: /main\.py/i }));
    const editor = await screen.findByLabelText("Code Editor");
    expect(editor).toHaveValue("print('hello')\n");

    await user.clear(editor);
    await user.type(editor, "print('updated')");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();

    // Revert restores saved content
    await user.click(screen.getByRole("button", { name: "Revert" }));
    expect(editor).toHaveValue("print('hello')\n");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    // Edit again and Save via button
    await user.type(editor, "# comment");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(transport.writeFile).toHaveBeenCalledWith("src/main.py", "print('hello')\n# comment");
    });

    // Ctrl+P Quick Open opens src/helper.py
    await user.keyboard("{Control>}p{/Control}");
    const searchInput = await screen.findByLabelText("Search files");
    await user.type(searchInput, "help{Enter}");

    await waitFor(() => {
      expect(screen.getByTestId("editor-meta")).toHaveTextContent("src/helper.py (markers: 1)");
    });

    // Diagnostics bar shows error count and expands drawer
    const diagBtn = screen.getByRole("button", { name: /Errors: 1/i });
    await user.click(diagBtn);
    expect(screen.getByText("Expected two blank lines")).toBeInTheDocument();
  });

  it("enforces zero browser token storage and zero raw HTML injection in source files", () => {
    const sources = import.meta.glob<string>(
      ["../**/*.ts", "../**/*.tsx", "!../__tests__/**"],
      { query: "?raw", import: "default", eager: true },
    );
    const entries = Object.entries(sources);
    expect(entries.length).toBeGreaterThan(5);
    for (const [filePath, text] of entries) {
      expect(filePath).toBeTruthy();
      expect(text).not.toContain("localStorage");
      expect(text).not.toContain("sessionStorage");
      expect(text).not.toContain("dangerouslySetInnerHTML");
      expect(text).not.toContain(".innerHTML");
    }
  });
});
