import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentPanel } from "./components/AgentPanel";
import { CodeEditorPane, type EditorRenderer } from "./components/CodeEditorPane";
import { ConnectBar } from "./components/ConnectBar";
import { DiagnosticsBar } from "./components/DiagnosticsBar";
import { DiffReviewPane, type DiffRenderer } from "./components/DiffReviewPane";
import { FileExplorer } from "./components/FileExplorer";
import { MemoryPanel } from "./components/MemoryPanel";
import { QuickOpenModal } from "./components/QuickOpenModal";
import { TabBar } from "./components/TabBar";
import { TerminalPanel } from "./components/TerminalPanel";
import type { ToolCallEntry } from "./components/ToolCallCard";
import { useAcpSession } from "./hooks/useAcpSession";
import { WebSocketACPClient, type WebSocketConstructor } from "./services/acpClient";
import { HttpWorkspaceClient } from "./services/workspaceClient";
import type { ACPTransport, DiffToolCallContent } from "./types/acp";
import type {
  EditorDiagnostic,
  GitStatusEntry,
  MemoryEntry,
  OpenTab,
  PendingDiffItem,
  TreeEntry,
  WorkspaceTransport,
} from "./types/workspace";
import { detectLanguage } from "./utils/fuzzy";

export interface StudioAppProps {
  transport?: WorkspaceTransport;
  acpTransport?: ACPTransport;
  initialDiagnostics?: EditorDiagnostic[];
  editorRenderer?: EditorRenderer;
  diffRenderer?: DiffRenderer;
  terminalWsFactory?: WebSocketConstructor;
}

function normalizeWorkspacePath(rawPath: string, workspaceRoot: string): string {
  const cleaned = rawPath.replaceAll("\\", "/").trim();
  if (cleaned.startsWith("/workspace/")) {
    return cleaned.slice("/workspace/".length);
  }
  const normalizedRoot = workspaceRoot.replaceAll("\\", "/").replace(/\/+$/, "");
  if (normalizedRoot && cleaned.startsWith(`${normalizedRoot}/`)) {
    return cleaned.slice(normalizedRoot.length + 1);
  }
  return cleaned.replace(/^\/+/, "");
}

export function StudioApp({
  transport,
  acpTransport,
  initialDiagnostics = [],
  editorRenderer,
  diffRenderer,
  terminalWsFactory,
}: StudioAppProps) {
  const client = useMemo(() => transport ?? new HttpWorkspaceClient(), [transport]);
  const acpClient = useMemo(() => acpTransport ?? new WebSocketACPClient(), [acpTransport]);

  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [applyingDiff, setApplyingDiff] = useState(false);
  const [errorBanner, setErrorBanner] = useState("");
  const [statusMessage, setStatusMessage] = useState("Disconnected");

  const [entries, setEntries] = useState<TreeEntry[]>([]);
  const [branch, setBranch] = useState("");
  const [gitEntries, setGitEntries] = useState<GitStatusEntry[]>([]);
  const [tabs, setTabs] = useState<OpenTab[]>([]);
  const [activePath, setActivePath] = useState<string | null>(null);
  const [focusLine, setFocusLine] = useState<number | null>(null);
  const [quickOpenVisible, setQuickOpenVisible] = useState(false);
  const [agentPanelOpen, setAgentPanelOpen] = useState(false);
  const [memoryPanelOpen, setMemoryPanelOpen] = useState(false);
  const [terminalOpen, setTerminalOpen] = useState(false);
  const [activeDiffId, setActiveDiffId] = useState<string | null>(null);
  const [reviewingDiff, setReviewingDiff] = useState(false);
  const [memoryPath, setMemoryPath] = useState(".agent-forge/memory.md");
  const [memoryEntries, setMemoryEntries] = useState<MemoryEntry[]>([]);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [diagnostics] = useState<EditorDiagnostic[]>(initialDiagnostics);

  const tabsRef = useRef<OpenTab[]>([]);
  useEffect(() => {
    tabsRef.current = tabs;
  }, [tabs]);

  const loadWorkspace = useCallback(async () => {
    const [treeEntries, gitStatus] = await Promise.all([
      client.listTree("", 6),
      client.getGitStatus(),
    ]);
    setEntries(treeEntries);
    setBranch(gitStatus.branch);
    setGitEntries(gitStatus.entries);
  }, [client]);

  const readFileBuffer = useCallback(
    (rawPath: string): string | null => {
      const root = client.getWorkspaceRoot?.() ?? "";
      const relPath = normalizeWorkspacePath(rawPath, root);
      const match = tabsRef.current.find((tab) => tab.path === relPath);
      return match ? match.content : null;
    },
    [client],
  );

  const acpSession = useAcpSession({
    acp: acpClient,
    onTurnCompleted: loadWorkspace,
    readFileBuffer,
  });

  const loadMemory = useCallback(async () => {
    if (!client.listMemory) {
      return;
    }
    setMemoryLoading(true);
    try {
      const resp = await client.listMemory();
      setMemoryPath(resp.path || ".agent-forge/memory.md");
      setMemoryEntries(resp.entries);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : "Failed to load repository memory.");
    } finally {
      setMemoryLoading(false);
    }
  }, [client]);

  const handleAddMemory = useCallback(
    async (text: string, category: string) => {
      if (!client.addMemory) {
        return;
      }
      setMemoryLoading(true);
      try {
        const resp = await client.addMemory(text, category);
        setMemoryEntries(resp.entries);
        await loadWorkspace();
      } catch (err) {
        setErrorBanner(err instanceof Error ? err.message : "Failed to add memory entry.");
      } finally {
        setMemoryLoading(false);
      }
    },
    [client, loadWorkspace],
  );

  const handleRemoveMemory = useCallback(
    async (entryId: string) => {
      if (!client.removeMemory) {
        return;
      }
      setMemoryLoading(true);
      try {
        const resp = await client.removeMemory(entryId);
        setMemoryEntries(resp.entries);
        await loadWorkspace();
      } catch (err) {
        setErrorBanner(err instanceof Error ? err.message : "Failed to remove memory entry.");
      } finally {
        setMemoryLoading(false);
      }
    },
    [client, loadWorkspace],
  );

  const handleClearMemory = useCallback(async () => {
    if (!client.clearMemory) {
      return;
    }
    setMemoryLoading(true);
    try {
      const resp = await client.clearMemory();
      setMemoryEntries(resp.entries);
      await loadWorkspace();
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : "Failed to clear memory.");
    } finally {
      setMemoryLoading(false);
    }
  }, [client, loadWorkspace]);

  const handleConnect = useCallback(
    async (bearerToken: string) => {
      setConnecting(true);
      setErrorBanner("");
      try {
        await client.connectWithToken(bearerToken);
        await loadWorkspace();
        setConnected(true);
        setAgentPanelOpen(true);
        setStatusMessage("Connected");
        void loadMemory();
        const workspaceRoot = client.getWorkspaceRoot?.() ?? "";
        try {
          await acpSession.startSessionFlow(workspaceRoot);
        } catch (acpErr) {
          acpSession.setAgentError(
            acpErr instanceof Error ? acpErr.message : "Failed to connect ACP session.",
          );
        }
      } catch (err) {
        setErrorBanner(err instanceof Error ? err.message : "Failed to connect.");
        setStatusMessage("Connection failed");
      } finally {
        setConnecting(false);
      }
    },
    [acpSession, client, loadMemory, loadWorkspace],
  );

  const handleDisconnect = useCallback(async () => {
    acpSession.reset();
    await client.logout();
    setConnected(false);
    setAgentPanelOpen(false);
    setMemoryPanelOpen(false);
    setTerminalOpen(false);
    setReviewingDiff(false);
    setActiveDiffId(null);
    setMemoryEntries([]);
    setEntries([]);
    setBranch("");
    setGitEntries([]);
    setTabs([]);
    setActivePath(null);
    setStatusMessage("Disconnected");
  }, [acpSession, client]);

  const handleRefresh = useCallback(async () => {
    setErrorBanner("");
    try {
      await loadWorkspace();
      setStatusMessage("Workspace refreshed");
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : "Failed to refresh workspace.");
    }
  }, [loadWorkspace]);

  const handleOpenFile = useCallback(
    async (filePath: string, lineNumber: number | null = null) => {
      setErrorBanner("");
      const existing = tabs.find((tab) => tab.path === filePath);
      if (existing) {
        setActivePath(filePath);
        setFocusLine(lineNumber);
        return;
      }
      try {
        const file = await client.readFile(filePath);
        const newTab: OpenTab = {
          path: file.path,
          language: detectLanguage(file.path),
          content: file.content,
          savedContent: file.content,
          dirty: false,
        };
        setTabs((prev) => {
          const filtered = prev.filter((item) => item.path !== file.path);
          return [...filtered, newTab];
        });
        setActivePath(file.path);
        setFocusLine(lineNumber);
        setStatusMessage(`Opened ${file.path}`);
      } catch (err) {
        setErrorBanner(err instanceof Error ? err.message : `Failed to open ${filePath}`);
      }
    },
    [client, tabs],
  );

  const handleOpenAgentLocation = useCallback(
    (rawPath: string, lineNumber: number | null) => {
      const root = client.getWorkspaceRoot?.() ?? "";
      const normalized = normalizeWorkspacePath(rawPath, root);
      if (normalized) {
        setReviewingDiff(false);
        void handleOpenFile(normalized, lineNumber);
      }
    },
    [client, handleOpenFile],
  );

  const handleReviewDiff = useCallback(
    (diffBlock: DiffToolCallContent, entry: ToolCallEntry) => {
      const root = client.getWorkspaceRoot?.() ?? "";
      const normalized = normalizeWorkspacePath(diffBlock.path, root);
      const targetId = `${entry.toolCallId}:${normalized}`;
      setActiveDiffId(targetId);
      setReviewingDiff(true);
    },
    [client],
  );

  const syncTabAfterDiffWrite = useCallback((filePath: string, nextContent: string) => {
    setTabs((prev) =>
      prev.map((tab) =>
        tab.path === filePath ? { ...tab, content: nextContent, savedContent: nextContent, dirty: false } : tab,
      ),
    );
  }, []);

  const handleApplyDiff = useCallback(
    async (diff: PendingDiffItem) => {
      setApplyingDiff(true);
      setErrorBanner("");
      try {
        await client.writeFile(diff.path, diff.newText);
        acpSession.markDiffApplied(diff.id, true);
        syncTabAfterDiffWrite(diff.path, diff.newText);
        await loadWorkspace();
        setStatusMessage(`Applied diff to ${diff.path}`);
      } catch (err) {
        setErrorBanner(err instanceof Error ? err.message : `Failed to apply diff to ${diff.path}`);
      } finally {
        setApplyingDiff(false);
      }
    },
    [acpSession, client, loadWorkspace, syncTabAfterDiffWrite],
  );

  const handleUndoDiff = useCallback(
    async (diff: PendingDiffItem) => {
      setApplyingDiff(true);
      setErrorBanner("");
      try {
        await client.writeFile(diff.path, diff.oldText);
        acpSession.markDiffApplied(diff.id, false);
        syncTabAfterDiffWrite(diff.path, diff.oldText);
        await loadWorkspace();
        setStatusMessage(`Reverted diff on ${diff.path}`);
      } catch (err) {
        setErrorBanner(err instanceof Error ? err.message : `Failed to undo diff on ${diff.path}`);
      } finally {
        setApplyingDiff(false);
      }
    },
    [acpSession, client, loadWorkspace, syncTabAfterDiffWrite],
  );

  const handleChangeActive = useCallback(
    (nextValue: string) => {
      if (!activePath) {
        return;
      }
      setTabs((prev) =>
        prev.map((tab) =>
          tab.path === activePath
            ? { ...tab, content: nextValue, dirty: nextValue !== tab.savedContent }
            : tab,
        ),
      );
    },
    [activePath],
  );

  const handleSaveActive = useCallback(async () => {
    const current = tabs.find((tab) => tab.path === activePath);
    if (!current || !current.dirty) {
      return;
    }
    setSaving(true);
    setErrorBanner("");
    try {
      await client.writeFile(current.path, current.content);
      setTabs((prev) =>
        prev.map((tab) =>
          tab.path === current.path
            ? { ...tab, savedContent: current.content, dirty: false }
            : tab,
        ),
      );
      const gitStatus = await client.getGitStatus();
      setBranch(gitStatus.branch);
      setGitEntries(gitStatus.entries);
      setStatusMessage(`Saved ${current.path}`);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : `Failed to save ${current.path}`);
    } finally {
      setSaving(false);
    }
  }, [activePath, client, tabs]);

  const handleRevertActive = useCallback(() => {
    if (!activePath) {
      return;
    }
    setTabs((prev) =>
      prev.map((tab) =>
        tab.path === activePath
          ? { ...tab, content: tab.savedContent, dirty: false }
          : tab,
      ),
    );
    setStatusMessage(`Reverted ${activePath}`);
  }, [activePath]);

  const handleCloseTab = useCallback(
    (targetPath: string) => {
      setTabs((prev) => {
        const next = prev.filter((tab) => tab.path !== targetPath);
        if (activePath === targetPath) {
          setActivePath(next.length > 0 ? next[next.length - 1].path : null);
        }
        return next;
      });
    },
    [activePath],
  );

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent): void {
      const mod = event.ctrlKey || event.metaKey;
      if (mod && event.key.toLowerCase() === "p" && connected) {
        event.preventDefault();
        setQuickOpenVisible(true);
      } else if (mod && event.key.toLowerCase() === "s" && connected) {
        event.preventDefault();
        void handleSaveActive();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [connected, handleSaveActive]);

  const activeTab = useMemo(
    () => tabs.find((tab) => tab.path === activePath) ?? null,
    [activePath, tabs],
  );
  const dirtyPaths = useMemo(
    () => new Set(tabs.filter((t) => t.dirty).map((t) => t.path)),
    [tabs],
  );
  const gitStatusMap = useMemo(() => {
    const map = new Map<string, GitStatusEntry>();
    for (const item of gitEntries) {
      map.set(item.path, item);
    }
    return map;
  }, [gitEntries]);
  const activeDiagnostics = useMemo(
    () => (activePath ? diagnostics.filter((diag) => diag.path === activePath) : []),
    [activePath, diagnostics],
  );

  return (
    <div className="studio-shell">
      <ConnectBar
        connected={connected}
        connecting={connecting}
        branch={branch}
        onConnect={handleConnect}
        onDisconnect={handleDisconnect}
        onRefresh={handleRefresh}
        onOpenQuickSearch={() => setQuickOpenVisible(true)}
        agentPanelOpen={agentPanelOpen}
        onToggleAgentPanel={() => setAgentPanelOpen((prev) => !prev)}
        memoryPanelOpen={memoryPanelOpen}
        onToggleMemoryPanel={() => {
          setMemoryPanelOpen((prev) => {
            const next = !prev;
            if (next) {
              void loadMemory();
            }
            return next;
          });
        }}
        terminalOpen={terminalOpen}
        onToggleTerminal={() => setTerminalOpen((prev) => !prev)}
      />

      {errorBanner ? (
        <div className="error-banner" role="alert">
          <span>{errorBanner}</span>
          <button type="button" className="btn-secondary" onClick={() => setErrorBanner("")}>
            Dismiss
          </button>
        </div>
      ) : null}

      <div className="studio-main">
        <FileExplorer
          entries={entries}
          activePath={activePath}
          dirtyPaths={dirtyPaths}
          gitStatusMap={gitStatusMap}
          onSelectFile={(filePath) => {
            setReviewingDiff(false);
            void handleOpenFile(filePath);
          }}
        />

        <main className="studio-editor-column">
          <TabBar
            tabs={tabs}
            activePath={activePath}
            saving={saving}
            onSelectTab={(filePath) => {
              setReviewingDiff(false);
              setActivePath(filePath);
              setFocusLine(null);
            }}
            onCloseTab={handleCloseTab}
            onSaveActive={() => void handleSaveActive()}
            onRevertActive={handleRevertActive}
          />
          {reviewingDiff && acpSession.diffs.length > 0 ? (
            <DiffReviewPane
              diffs={acpSession.diffs}
              activeDiffId={activeDiffId}
              applying={applyingDiff}
              onSelectDiff={(id) => setActiveDiffId(id)}
              onApplyDiff={handleApplyDiff}
              onUndoDiff={handleUndoDiff}
              onClose={() => setReviewingDiff(false)}
              diffRenderer={diffRenderer}
            />
          ) : (
            <CodeEditorPane
              path={activeTab?.path ?? null}
              value={activeTab?.content ?? ""}
              language={activeTab?.language ?? "plaintext"}
              diagnostics={activeDiagnostics}
              focusLine={focusLine}
              onChange={handleChangeActive}
              onSave={() => void handleSaveActive()}
              editorRenderer={editorRenderer}
            />
          )}
          <TerminalPanel
            open={terminalOpen}
            connected={connected}
            webSocketFactory={terminalWsFactory}
            onClose={() => setTerminalOpen(false)}
          />
        </main>

        <MemoryPanel
          open={memoryPanelOpen}
          connected={connected}
          memoryPath={memoryPath}
          entries={memoryEntries}
          loading={memoryLoading}
          onAdd={handleAddMemory}
          onRemove={handleRemoveMemory}
          onClear={handleClearMemory}
          onRefresh={loadMemory}
          onOpenRawFile={(filePath) => {
            setReviewingDiff(false);
            void handleOpenFile(filePath);
          }}
          onClose={() => setMemoryPanelOpen(false)}
        />

        {agentPanelOpen ? (
          <AgentPanel
            connected={connected}
            connectionState={acpSession.connectionState}
            sessions={acpSession.sessions}
            activeSessionId={acpSession.activeSessionId}
            configOptions={acpSession.configOptions}
            availableCommands={acpSession.availableCommands}
            planEntries={acpSession.planEntries}
            timeline={acpSession.timeline}
            pendingPermission={acpSession.pendingPermission}
            promptRunning={acpSession.promptRunning}
            agentError={acpSession.agentError}
            onDismissError={() => acpSession.setAgentError("")}
            onSelectSession={(sessionId) => void acpSession.selectSession(sessionId)}
            onNewSession={() => void acpSession.createNewSession()}
            onChangeConfig={(configId, value) =>
              void acpSession.changeConfigOption(configId, value)
            }
            onSendPrompt={(text) => void acpSession.submitPrompt(text)}
            onCancelTurn={acpSession.cancelTurn}
            onRespondPermission={acpSession.respondPermission}
            onOpenLocation={handleOpenAgentLocation}
            onReviewDiff={handleReviewDiff}
          />
        ) : null}
      </div>

      <DiagnosticsBar
        activePath={activeTab?.path ?? null}
        language={activeTab?.language ?? "plaintext"}
        dirty={activeTab?.dirty ?? false}
        diagnostics={diagnostics}
        statusMessage={statusMessage}
        verification={acpSession.verification}
        terminalOpen={terminalOpen}
        onToggleTerminal={() => setTerminalOpen((prev) => !prev)}
        onSelectDiagnostic={(diag) => {
          setReviewingDiff(false);
          void handleOpenFile(diag.path, diag.line);
        }}
      />

      <QuickOpenModal
        open={quickOpenVisible}
        entries={entries}
        onClose={() => setQuickOpenVisible(false)}
        onSelectFile={(filePath) => void handleOpenFile(filePath)}
      />
    </div>
  );
}

export default StudioApp;
