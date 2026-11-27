import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HttpWorkspaceClient } from "../services/workspaceClient";

describe("HttpWorkspaceClient security and transport contract", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("exchanges Bearer token via header and keeps CSRF strictly in memory", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/auth/session") {
        expect(init?.method).toBe("POST");
        expect(init?.credentials).toBe("include");
        expect(init?.headers).toEqual({ Authorization: "Bearer super-secret" });
        return new Response(JSON.stringify({ csrfToken: "csrf-123", expiresIn: 3600 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/workspace/file" && init?.method === "POST") {
        expect(init?.credentials).toBe("include");
        expect(init?.headers).toEqual({
          "Content-Type": "application/json",
          "X-CSRF-Token": "csrf-123",
        });
        return new Response(JSON.stringify({ path: "app.py", size: 5, mtime_ns: 10 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/auth/logout") {
        expect(init?.headers).toEqual({ "X-CSRF-Token": "csrf-123" });
        return new Response(JSON.stringify({ status: "logged_out" }), { status: 200 });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });
    globalThis.fetch = fetchMock as typeof fetch;

    const client = new HttpWorkspaceClient();
    await client.connectWithToken("super-secret");

    expect(client.hasActiveCsrfToken()).toBe(true);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    for (const [calledUrl] of fetchMock.mock.calls) {
      expect(String(calledUrl)).not.toContain("super-secret");
      expect(String(calledUrl)).not.toContain("token=");
    }

    await client.writeFile("app.py", "x = 1");
    await client.logout();
    expect(client.hasActiveCsrfToken()).toBe(false);
  });
});
