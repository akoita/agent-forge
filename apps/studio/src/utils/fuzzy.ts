import type { GitStatusEntry, TreeEntry } from "../types/workspace";

export interface FuzzyMatch {
  entry: TreeEntry;
  score: number;
}

export interface FileTreeNode {
  entry: TreeEntry;
  children: FileTreeNode[];
}

const EXTENSION_LANGUAGE_MAP: Record<string, string> = {
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  py: "python",
  json: "json",
  md: "markdown",
  markdown: "markdown",
  css: "css",
  scss: "scss",
  html: "html",
  htm: "html",
  yaml: "yaml",
  yml: "yaml",
  toml: "ini",
  sh: "shell",
  bash: "shell",
  rs: "rust",
  go: "go",
  sql: "sql",
  sol: "sol",
};

export function detectLanguage(filePath: string): string {
  const base = filePath.split("/").pop()?.toLowerCase() ?? "";
  if (base === "dockerfile") {
    return "dockerfile";
  }
  const dotIdx = base.lastIndexOf(".");
  if (dotIdx <= 0) {
    return "plaintext";
  }
  const ext = base.slice(dotIdx + 1);
  return EXTENSION_LANGUAGE_MAP[ext] ?? "plaintext";
}

export function fuzzyMatchScore(query: string, candidate: string): number {
  const q = query.trim().toLowerCase();
  const target = candidate.toLowerCase();
  if (!q) {
    return 1;
  }
  if (target === q) {
    return 1000;
  }
  let qIdx = 0;
  let score = 0;
  let consecutive = 0;
  for (let i = 0; i < target.length && qIdx < q.length; i += 1) {
    if (target[i] === q[qIdx]) {
      consecutive += 1;
      const prev = i > 0 ? target[i - 1] : "/";
      const boundaryBonus = prev === "/" || prev === "_" || prev === "-" || prev === "." ? 8 : 0;
      score += 10 + consecutive * 4 + boundaryBonus;
      qIdx += 1;
    } else {
      consecutive = 0;
    }
  }
  if (qIdx !== q.length) {
    return -1;
  }
  const basename = target.split("/").pop() ?? target;
  const baseBonus = basename.includes(q) ? 25 : 0;
  return score + baseBonus - Math.min(target.length, 50) * 0.1;
}

export function fuzzyFilterFiles(entries: TreeEntry[], query: string, limit = 50): TreeEntry[] {
  const files = entries.filter((entry) => entry.kind === "file");
  if (!query.trim()) {
    return files.slice(0, limit);
  }
  const matches: FuzzyMatch[] = [];
  for (const entry of files) {
    const score = fuzzyMatchScore(query, entry.path);
    if (score >= 0) {
      matches.push({ entry, score });
    }
  }
  matches.sort((a, b) => b.score - a.score || a.entry.path.localeCompare(b.entry.path));
  return matches.slice(0, limit).map((item) => item.entry);
}

export function buildFileTree(entries: TreeEntry[]): FileTreeNode[] {
  const sorted = [...entries].sort((a, b) => {
    const aDepth = a.path.split("/").length;
    const bDepth = b.path.split("/").length;
    if (aDepth !== bDepth) {
      return aDepth - bDepth;
    }
    if (a.kind !== b.kind) {
      return a.kind === "directory" ? -1 : 1;
    }
    return a.path.localeCompare(b.path);
  });

  const nodesByPath = new Map<string, FileTreeNode>();
  const roots: FileTreeNode[] = [];

  for (const entry of sorted) {
    const node: FileTreeNode = { entry, children: [] };
    nodesByPath.set(entry.path, node);
    const lastSlash = entry.path.lastIndexOf("/");
    const parentPath = lastSlash >= 0 ? entry.path.slice(0, lastSlash) : "";
    const parent = parentPath ? nodesByPath.get(parentPath) : undefined;
    if (parent) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }

  return roots;
}

export function formatGitBadge(entry: GitStatusEntry | undefined): string {
  if (!entry) {
    return "";
  }
  const code = `${entry.index}${entry.worktree}`.trim();
  if (code === "??") {
    return "U";
  }
  if (code.includes("A")) {
    return "A";
  }
  if (code.includes("D")) {
    return "D";
  }
  if (code.includes("R")) {
    return "R";
  }
  if (code.includes("M")) {
    return "M";
  }
  return code[0] ?? "";
}
