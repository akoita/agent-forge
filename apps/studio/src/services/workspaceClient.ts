import type {
  GitDiffResponse,
  GitStatusResponse,
  TreeEntry,
  WorkspaceFile,
  WorkspaceTransport,
} from "../types/workspace";

interface SessionResponse {
  csrfToken: string;
  expiresIn: number;
  workspaceRoot?: string;
}

async function parseErrorDetail(resp: Response): Promise<string> {
  try {
    const body = (await resp.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) {
      return body.detail;
    }
  } catch {
    // Fall back to HTTP status text when error body is not JSON
  }
  return `Request failed (${resp.status} ${resp.statusText || "error"})`;
}

/**
 * HTTP + Cookie/CSRF workspace companion client matching `agent_forge/acp/ws_transport.py`.
 * Never persists credentials in browser storage APIs or sends tokens in URL queries.
 */
export class HttpWorkspaceClient implements WorkspaceTransport {
  private readonly baseUrl: string;
  private csrfToken = "";
  private workspaceRoot = "";

  constructor(baseUrl = "") {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  hasActiveCsrfToken(): boolean {
    return this.csrfToken.length > 0;
  }

  getWorkspaceRoot(): string {
    return this.workspaceRoot;
  }

  private url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  async connectWithToken(bearerToken: string): Promise<void> {
    const trimmed = bearerToken.trim();
    if (!trimmed) {
      throw new Error("Bearer token is required to connect.");
    }
    const resp = await fetch(this.url("/auth/session"), {
      method: "POST",
      credentials: "include",
      headers: {
        Authorization: `Bearer ${trimmed}`,
      },
    });
    if (!resp.ok) {
      throw new Error(await parseErrorDetail(resp));
    }
    const data = (await resp.json()) as SessionResponse;
    if (!data.csrfToken) {
      throw new Error("Server response did not include a CSRF token.");
    }
    this.csrfToken = data.csrfToken;
    this.workspaceRoot = typeof data.workspaceRoot === "string" ? data.workspaceRoot : "";
  }

  async logout(): Promise<void> {
    try {
      const headers: Record<string, string> = {};
      if (this.csrfToken) {
        headers["X-CSRF-Token"] = this.csrfToken;
      }
      await fetch(this.url("/auth/logout"), {
        method: "POST",
        credentials: "include",
        headers,
      });
    } finally {
      this.csrfToken = "";
      this.workspaceRoot = "";
    }
  }

  async listTree(path = "", maxDepth = 6): Promise<TreeEntry[]> {
    const params = new URLSearchParams();
    if (path) {
      params.set("path", path);
    }
    params.set("max_depth", String(maxDepth));
    const resp = await fetch(this.url(`/workspace/tree?${params.toString()}`), {
      method: "GET",
      credentials: "include",
    });
    if (!resp.ok) {
      throw new Error(await parseErrorDetail(resp));
    }
    const data = (await resp.json()) as { entries: TreeEntry[] };
    return data.entries;
  }

  async readFile(path: string): Promise<WorkspaceFile> {
    const params = new URLSearchParams({ path });
    const resp = await fetch(this.url(`/workspace/file?${params.toString()}`), {
      method: "GET",
      credentials: "include",
    });
    if (!resp.ok) {
      throw new Error(await parseErrorDetail(resp));
    }
    return (await resp.json()) as WorkspaceFile;
  }

  async writeFile(
    path: string,
    content: string,
  ): Promise<{ path: string; size: number; mtime_ns: number }> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (this.csrfToken) {
      headers["X-CSRF-Token"] = this.csrfToken;
    }
    const resp = await fetch(this.url("/workspace/file"), {
      method: "POST",
      credentials: "include",
      headers,
      body: JSON.stringify({ path, content }),
    });
    if (!resp.ok) {
      throw new Error(await parseErrorDetail(resp));
    }
    return (await resp.json()) as { path: string; size: number; mtime_ns: number };
  }

  async getGitStatus(): Promise<GitStatusResponse> {
    const resp = await fetch(this.url("/workspace/git/status"), {
      method: "GET",
      credentials: "include",
    });
    if (!resp.ok) {
      throw new Error(await parseErrorDetail(resp));
    }
    return (await resp.json()) as GitStatusResponse;
  }

  async getGitDiff(path?: string, staged = false): Promise<GitDiffResponse> {
    const params = new URLSearchParams({ staged: String(staged) });
    if (path) {
      params.set("path", path);
    }
    const resp = await fetch(this.url(`/workspace/git/diff?${params.toString()}`), {
      method: "GET",
      credentials: "include",
    });
    if (!resp.ok) {
      throw new Error(await parseErrorDetail(resp));
    }
    return (await resp.json()) as GitDiffResponse;
  }
}
