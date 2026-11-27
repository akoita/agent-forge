import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { StudioApp } from "../App";
import type { EditorRendererProps } from "../components/CodeEditorPane";
import type { DiffRendererProps } from "../components/DiffReviewPane";
import type { WebSocketLike } from "../services/acpClient";
import type {
  ACPConnectionListener,
  ACPPermissionListener,
  ACPTransport,
  ACPUpdateListener,
} from "../types/acp";
import type { MemoryEntry, TreeEntry, WorkspaceTransport } from "../types/workspace";

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

function TestDiffWidget({ diff }: DiffRendererProps) {
  return (
    <div data-testid="mock-monaco-diff">
      {diff.oldText} -&gt; {diff.newText}
    </div>
  );
}

class FakeTerminalSocket implements WebSocketLike {
  readyState = 1;
  sent: string[] = [];
  addEventListener(type: "open" | "message" | "close" | "error", cb: (...args: never[]) => void): void {
    if (type === "open") {
      setTimeout(() => (cb as () => void)(), 0);
    }
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {}
}

function createFakeAcpTransport(): ACPTransport {
  const updateListeners = new Set<ACPUpdateListener>();
  const permListeners = new Set<ACPPermissionListener>();
  const connListeners = new Set<ACPConnectionListener>();
  return {
    connect: vi.fn(async () => {
      for (const cb of connListeners) cb("connected");
    }),
    disconnect: vi.fn(() => {
      for (const cb of connListeners) cb("disconnected");
    }),
    getConnectionState: () => "disconnected",
    initialize: vi.fn(async () => ({
      protocolVersion: 1,
      agentCapabilities: { loadSession: true },
    })),
    listSessions: vi.fn(async () => ({ sessions: [] })),
    newSession: vi.fn(async () => ({ sessionId: "sess_test", configOptions: [] })),
    loadSession: vi.fn(async () => ({ configOptions: [] })),
    sendPrompt: vi.fn(async (sessionId: string, text: string) => {
      for (const cb of updateListeners) {
        cb(sessionId, {
          sessionUpdate: "tool_call",
          toolCallId: "call-edit-1",
          title: "edit_file",
          kind: "edit",
          status: "completed",
        });
        cb(sessionId, {
          sessionUpdate: "tool_call_update",
          toolCallId: "call-edit-1",
          status: "completed",
          content: [
            {
              type: "diff",
              path: "/workspace/src/main.py",
              oldText: "print('hello')\n",
              newText: "print('from diff')\n",
            },
          ],
        });
        cb(sessionId, {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text: `Reply to: ${text}` },
        });
      }
      return { stopReason: "end_turn" as const };
    }),
    cancelSession: vi.fn(),
    setConfigOption: vi.fn(async () => ({ configOptions: [] })),
    setMode: vi.fn(async () => {}),
    respondToPermission: vi.fn(),
    onSessionUpdate: (cb) => {
      updateListeners.add(cb);
      return () => updateListeners.delete(cb);
    },
    onPermissionRequest: (cb) => {
      permListeners.add(cb);
      return () => permListeners.delete(cb);
    },
    onConnectionChange: (cb) => {
      connListeners.add(cb);
      return () => connListeners.delete(cb);
    },
    setReadFileHandler: vi.fn(),
  };
}

function createFakeTransport(): WorkspaceTransport & { files: Map<string, string> } {
  const files = new Map<string, string>([
    ["src/main.py", "print('hello')\n"],
    ["src/helper.py", "VALUE = 1\n"],
  ]);
  const memory: MemoryEntry[] = [];
  const tree: TreeEntry[] = [
    { path: "src", name: "src", kind: "directory", size: 0, mtime_ns: 1 },
    { path: "src/main.py", name: "main.py", kind: "file", size: 15, mtime_ns: 1 },
    { path: "src/helper.py", name: "helper.py", kind: "file", size: 10, mtime_ns: 1 },
  ];

  return {
    files,
    connectWithToken: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
    getWorkspaceRoot: () => "/tmp/repo",
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
    listMemory: vi.fn(async () => ({ path: ".agent-forge/memory.md", entries: [...memory], raw: "" })),
    addMemory: vi.fn(async (text: string, category = "general") => {
      const item: MemoryEntry = {
        id: `mem-0000000${memory.length + 1}`,
        category,
        text,
        created_at: "2026-11-27",
      };
      memory.push(item);
      return { path: ".agent-forge/memory.md", entries: [...memory], raw: "", added: true };
    }),
    removeMemory: vi.fn(async (entryId: string) => {
      const idx = memory.findIndex((m) => m.id === entryId);
      if (idx >= 0) memory.splice(idx, 1);
      return { path: ".agent-forge/memory.md", entries: [...memory], raw: "" };
    }),
    clearMemory: vi.fn(async () => {
      const count = memory.length;
      memory.length = 0;
      return { path: ".agent-forge/memory.md", entries: [], raw: "", cleared: count };
    }),
  };
}

describe("StudioApp web code editor shell", () => {
  it("connects, edits files, reviews/applies/undoes diffs, opens terminal and memory, and tracks verification", async () => {
    const user = userEvent.setup();
    const transport = createFakeTransport();
    const acpTransport = createFakeAcpTransport();

    render(
      <StudioApp
        transport={transport}
        acpTransport={acpTransport}
        initialDiagnostics={[
          {
            path: "src/helper.py",
            line: 1,
            severity: "error",
            message: "Expected two blank lines",
          },
        ]}
        editorRenderer={(props) => <TestEditorWidget {...props} />}
        diffRenderer={(props) => <TestDiffWidget {...props} />}
        terminalWsFactory={FakeTerminalSocket as unknown as new (url: string) => WebSocketLike}
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

    // Agent Panel connected and responds to a prompt with diff + passed verification
    const composer = await screen.findByLabelText("Agent prompt");
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "Explain main.py{Enter}");
    expect(await screen.findByText("Reply to: Explain main.py")).toBeInTheDocument();
    expect(screen.getByText("Verify: passed")).toBeInTheDocument();

    // Open inline Diff Review, apply diff, undo diff, then close diff
    await user.click(screen.getByRole("button", { name: "Review Diff" }));
    expect(await screen.findByTestId("mock-monaco-diff")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply Diff" }));
    await waitFor(() => {
      expect(transport.writeFile).toHaveBeenCalledWith("src/main.py", "print('from diff')\n");
    });
    await user.click(screen.getByRole("button", { name: "Undo Diff" }));
    await waitFor(() => {
      expect(transport.writeFile).toHaveBeenCalledWith("src/main.py", "print('hello')\n");
    });
    await user.click(screen.getByRole("button", { name: "Close Diff" }));

    // Open Memory Panel and add a note
    await user.click(screen.getByRole("button", { name: "Memory" }));
    await user.type(screen.getByLabelText("Memory learning note"), "Always review diffs");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText("Always review diffs")).toBeInTheDocument();

    // Open Terminal Drawer
    await user.click(screen.getAllByRole("button", { name: "Terminal" })[0]);
    expect(await screen.findByText("Terminal (PTY)")).toBeInTheDocument();

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

  it("enforces zero browser token storage, native modals, or raw HTML injection in source files", () => {
    const sources = import.meta.glob<string>(
      ["../**/*.ts", "../**/*.tsx", "!../__tests__/**"],
      { query: "?raw", import: "default", eager: true },
    );
    const entries = Object.entries(sources);
    expect(entries.length).toBeGreaterThan(8);
    for (const [filePath, text] of entries) {
      expect(filePath).toBeTruthy();
      expect(text).not.toContain("localStorage");
      expect(text).not.toContain("sessionStorage");
      expect(text).not.toContain("dangerouslySetInnerHTML");
      expect(text).not.toContain(".innerHTML");
      expect(text).not.toContain(".outerHTML");
      expect(text).not.toContain("document.write");
      expect(text).not.toContain("window.alert");
      expect(text).not.toContain("window.confirm");
      expect(text).not.toContain("window.prompt");
    }
  });
});
