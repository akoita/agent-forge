import { useMemo, useState } from "react";
import type { GitStatusEntry, TreeEntry } from "../types/workspace";
import { buildFileTree, formatGitBadge, type FileTreeNode } from "../utils/fuzzy";

interface FileExplorerProps {
  entries: TreeEntry[];
  activePath: string | null;
  dirtyPaths: ReadonlySet<string>;
  gitStatusMap: ReadonlyMap<string, GitStatusEntry>;
  onSelectFile: (path: string) => void;
}

interface TreeBranchProps {
  node: FileTreeNode;
  depth: number;
  activePath: string | null;
  dirtyPaths: ReadonlySet<string>;
  gitStatusMap: ReadonlyMap<string, GitStatusEntry>;
  collapsedDirs: ReadonlySet<string>;
  onToggleDir: (path: string) => void;
  onSelectFile: (path: string) => void;
}

function TreeBranch({
  node,
  depth,
  activePath,
  dirtyPaths,
  gitStatusMap,
  collapsedDirs,
  onToggleDir,
  onSelectFile,
}: TreeBranchProps) {
  const { entry, children } = node;
  const paddingLeft = 10 + depth * 14;

  if (entry.kind === "directory") {
    const isCollapsed = collapsedDirs.has(entry.path);
    return (
      <div className="tree-branch">
        <button
          type="button"
          className="tree-item tree-dir"
          style={{ paddingLeft }}
          onClick={() => onToggleDir(entry.path)}
          aria-expanded={!isCollapsed}
        >
          <span className="tree-chevron">{isCollapsed ? "▸" : "▾"}</span>
          <span className="tree-name">{entry.name}</span>
        </button>
        {!isCollapsed
          ? children.map((child) => (
              <TreeBranch
                key={child.entry.path}
                node={child}
                depth={depth + 1}
                activePath={activePath}
                dirtyPaths={dirtyPaths}
                gitStatusMap={gitStatusMap}
                collapsedDirs={collapsedDirs}
                onToggleDir={onToggleDir}
                onSelectFile={onSelectFile}
              />
            ))
          : null}
      </div>
    );
  }

  const badge = formatGitBadge(gitStatusMap.get(entry.path));
  const isActive = activePath === entry.path;
  const isDirty = dirtyPaths.has(entry.path);

  return (
    <button
      type="button"
      className={`tree-item tree-file${isActive ? " is-active" : ""}`}
      style={{ paddingLeft }}
      onClick={() => onSelectFile(entry.path)}
      title={entry.path}
    >
      <span className="tree-name">{entry.name}</span>
      <span className="tree-badges">
        {isDirty ? (
          <span className="dirty-dot" aria-label="Unsaved changes">
            ●
          </span>
        ) : null}
        {badge ? <span className={`git-badge git-badge-${badge.toLowerCase()}`}>{badge}</span> : null}
      </span>
    </button>
  );
}

export function FileExplorer({
  entries,
  activePath,
  dirtyPaths,
  gitStatusMap,
  onSelectFile,
}: FileExplorerProps) {
  const [collapsedDirs, setCollapsedDirs] = useState<Set<string>>(() => new Set());
  const tree = useMemo(() => buildFileTree(entries), [entries]);

  function handleToggleDir(dirPath: string): void {
    setCollapsedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(dirPath)) {
        next.delete(dirPath);
      } else {
        next.add(dirPath);
      }
      return next;
    });
  }

  return (
    <aside className="studio-sidebar" aria-label="Workspace Explorer">
      <div className="panel-header">Explorer</div>
      <div className="tree-container" role="tree">
        {tree.length === 0 ? (
          <div className="empty-hint">No workspace files loaded.</div>
        ) : (
          tree.map((node) => (
            <TreeBranch
              key={node.entry.path}
              node={node}
              depth={0}
              activePath={activePath}
              dirtyPaths={dirtyPaths}
              gitStatusMap={gitStatusMap}
              collapsedDirs={collapsedDirs}
              onToggleDir={handleToggleDir}
              onSelectFile={onSelectFile}
            />
          ))
        )}
      </div>
    </aside>
  );
}
