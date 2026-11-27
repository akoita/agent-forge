import { useCallback, useEffect, useMemo, useState } from "react";
import { CodeEditorPane, type EditorRenderer } from "./components/CodeEditorPane";
import { ConnectBar } from "./components/ConnectBar";
import { DiagnosticsBar } from "./components/DiagnosticsBar";
import { FileExplorer } from "./components/FileExplorer";
import { QuickOpenModal } from "./components/QuickOpenModal";
import { TabBar } from "./components/TabBar";
import { HttpWorkspaceClient } from "./services/workspaceClient";
import type {
  EditorDiagnostic,
  GitStatusEntry,
  OpenTab,
  TreeEntry,
  WorkspaceTransport,
} from "./types/workspace";
import { detectLanguage } from "./utils/fuzzy";

export interface StudioAppProps {
  transport?: WorkspaceTransport;
  initialDiagnostics?: EditorDiagnostic[];
  editorRenderer?: EditorRenderer;
}

export function StudioApp({
  transport,
  initialDiagnostics = [],
  editorRenderer,
}: StudioAppProps) {
  const client = useMemo(() => transport ?? new HttpWorkspaceClient(), [transport]);

  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [saving, setSaving] = useState(false);
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
  const [diagnostics] = useState<EditorDiagnostic[]>(initialDiagnostics);

  const loadWorkspace = useCallback(async () => {
    const [treeEntries, gitStatus] = await Promise.all([
      client.listTree("", 6),
      client.getGitStatus(),
    ]);
    setEntries(treeEntries);
    setBranch(gitStatus.branch);
    setGitEntries(gitStatus.entries);
  }, [client]);

  const handleConnect = useCallback(
    async (bearerToken: string) => {
      setConnecting(true);
      setErrorBanner("");
      try {
        await client.connectWithToken(bearerToken);
        await loadWorkspace();
        setConnected(true);
        setStatusMessage("Connected");
      } catch (err) {
        setErrorBanner(err instanceof Error ? err.message : "Failed to connect.");
        setStatusMessage("Connection failed");
      } finally {
        setConnecting(false);
      }
    },
    [client, loadWorkspace],
  );

  const handleDisconnect = useCallback(async () => {
    await client.logout();
    setConnected(false);
    setEntries([]);
    setBranch("");
    setGitEntries([]);
    setTabs([]);
    setActivePath(null);
    setStatusMessage("Disconnected");
  }, [client]);

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
          onSelectFile={(filePath) => void handleOpenFile(filePath)}
        />

        <main className="studio-editor-column">
          <TabBar
            tabs={tabs}
            activePath={activePath}
            saving={saving}
            onSelectTab={(filePath) => {
              setActivePath(filePath);
              setFocusLine(null);
            }}
            onCloseTab={handleCloseTab}
            onSaveActive={() => void handleSaveActive()}
            onRevertActive={handleRevertActive}
          />
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
        </main>

        {agentPanelOpen ? (
          <aside className="studio-agent-slot" aria-label="ACP Agent Panel">
            <div className="panel-header">Agent (ACP)</div>
            <div className="empty-hint">
              ACP streaming chat and interactive approvals connect here in Issue #238.
            </div>
          </aside>
        ) : null}
      </div>

      <DiagnosticsBar
        activePath={activeTab?.path ?? null}
        language={activeTab?.language ?? "plaintext"}
        dirty={activeTab?.dirty ?? false}
        diagnostics={diagnostics}
        statusMessage={statusMessage}
        onSelectDiagnostic={(diag) => void handleOpenFile(diag.path, diag.line)}
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
