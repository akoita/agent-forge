export interface TreeEntry {
  path: string;
  name: string;
  kind: "file" | "directory";
  size: number;
  mtime_ns: number;
}

export interface WorkspaceFile {
  path: string;
  content: string;
  size: number;
  mtime_ns: number;
  encoding: string;
}

export interface GitStatusEntry {
  index: string;
  worktree: string;
  path: string;
}

export interface GitStatusResponse {
  branch: string;
  entries: GitStatusEntry[];
  error?: string;
}

export interface GitDiffResponse {
  diff: string;
  truncated: boolean;
  error?: string;
}

export type DiagnosticSeverity = "error" | "warning" | "info";

export interface EditorDiagnostic {
  path: string;
  line: number;
  column?: number;
  endLine?: number;
  endColumn?: number;
  severity: DiagnosticSeverity;
  message: string;
  source?: string;
}

export interface OpenTab {
  path: string;
  language: string;
  content: string;
  savedContent: string;
  dirty: boolean;
  loading?: boolean;
}

export interface WorkspaceTransport {
  connectWithToken(bearerToken: string): Promise<void>;
  logout(): Promise<void>;
  getWorkspaceRoot?(): string;
  listTree(path?: string, maxDepth?: number): Promise<TreeEntry[]>;
  readFile(path: string): Promise<WorkspaceFile>;
  writeFile(path: string, content: string): Promise<{ path: string; size: number; mtime_ns: number }>;
  getGitStatus(): Promise<GitStatusResponse>;
  getGitDiff(path?: string, staged?: boolean): Promise<GitDiffResponse>;
}
