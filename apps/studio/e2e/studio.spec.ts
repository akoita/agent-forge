import { expect, test } from "@playwright/test";

test("Studio E2E smoke: auth, Monaco editor, ACP streaming + approval, DiffEditor apply, memory, and xterm", async ({
  page,
}) => {
  const files: Record<string, string> = {
    "src/main.py": "print('hello')\n",
  };
  const memoryEntries: Array<{ id: string; category: string; text: string; created_at: string }> =
    [];
  let csrfObserved = "";

  await page.route("**/auth/session", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        csrfToken: "e2e-csrf-token",
        expiresIn: 3600,
        workspaceRoot: "/tmp/e2e-repo",
      }),
    });
  });

  await page.route("**/workspace/tree**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        entries: [
          { path: "src", name: "src", kind: "directory", size: 0, mtime_ns: 1 },
          { path: "src/main.py", name: "main.py", kind: "file", size: 15, mtime_ns: 1 },
        ],
      }),
    });
  });

  await page.route("**/workspace/git/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        branch: "main",
        entries: [{ index: " ", worktree: "M", path: "src/main.py" }],
      }),
    });
  });

  await page.route("**/workspace/file**", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      const target = new URL(req.url()).searchParams.get("path") ?? "src/main.py";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          path: target,
          content: files[target] ?? "",
          size: (files[target] ?? "").length,
          mtime_ns: 1,
          encoding: "utf-8",
        }),
      });
      return;
    }
    csrfObserved = (await req.headerValue("x-csrf-token")) ?? "";
    const body = req.postDataJSON() as { path: string; content: string };
    files[body.path] = body.content;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ path: body.path, size: body.content.length, mtime_ns: 2 }),
    });
  });

  await page.route("**/workspace/memory", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ path: ".agent-forge/memory.md", entries: memoryEntries, raw: "" }),
    });
  });

  await page.route("**/workspace/memory/add", async (route) => {
    const body = route.request().postDataJSON() as { text: string; category: string };
    const entry = {
      id: "mem-12345678",
      category: body.category || "general",
      text: body.text,
      created_at: "2026-11-27",
    };
    memoryEntries.push(entry);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        added: true,
        entry,
        path: ".agent-forge/memory.md",
        entries: memoryEntries,
        raw: "",
      }),
    });
  });

  await page.routeWebSocket("**/ws/acp", (ws) => {
    let promptId: number | null = null;
    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as {
        id?: number;
        method?: string;
        result?: unknown;
      };
      if (msg.method === "initialize") {
        ws.send(
          JSON.stringify({
            jsonrpc: "2.0",
            id: msg.id,
            result: { protocolVersion: 1, agentCapabilities: { loadSession: true } },
          }),
        );
      } else if (msg.method === "session/list") {
        ws.send(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result: { sessions: [] } }));
      } else if (msg.method === "session/new") {
        ws.send(
          JSON.stringify({
            jsonrpc: "2.0",
            id: msg.id,
            result: { sessionId: "sess_e2e", configOptions: [] },
          }),
        );
      } else if (msg.method === "session/prompt") {
        promptId = msg.id ?? null;
        ws.send(
          JSON.stringify({
            jsonrpc: "2.0",
            id: 500,
            method: "session/request_permission",
            params: {
              sessionId: "sess_e2e",
              toolCall: { toolCallId: "perm_e2e", title: "edit_file", kind: "edit", rawInput: {} },
              options: [
                { optionId: "allow_once", name: "Allow once", kind: "allow_once" },
                { optionId: "reject_once", name: "Reject", kind: "reject_once" },
              ],
            },
          }),
        );
      } else if (msg.id === 500) {
        ws.send(
          JSON.stringify({
            jsonrpc: "2.0",
            method: "session/update",
            params: {
              sessionId: "sess_e2e",
              update: {
                sessionUpdate: "tool_call",
                toolCallId: "call_e2e",
                title: "edit_file",
                kind: "edit",
                status: "completed",
              },
            },
          }),
        );
        ws.send(
          JSON.stringify({
            jsonrpc: "2.0",
            method: "session/update",
            params: {
              sessionId: "sess_e2e",
              update: {
                sessionUpdate: "tool_call_update",
                toolCallId: "call_e2e",
                status: "completed",
                content: [
                  {
                    type: "diff",
                    path: "src/main.py",
                    oldText: "print('hello')\n",
                    newText: "print('e2e updated')\n",
                  },
                ],
              },
            },
          }),
        );
        ws.send(
          JSON.stringify({
            jsonrpc: "2.0",
            method: "session/update",
            params: {
              sessionId: "sess_e2e",
              update: {
                sessionUpdate: "agent_message_chunk",
                content: { type: "text", text: "Prepared diff for main.py." },
              },
            },
          }),
        );
        ws.send(
          JSON.stringify({
            jsonrpc: "2.0",
            id: promptId,
            result: { stopReason: "end_turn" },
          }),
        );
      }
    });
  });

  await page.routeWebSocket("**/ws/terminal", (ws) => {
    ws.onMessage(() => {
      ws.send(JSON.stringify({ type: "output", data: "E2E_PTY_READY\r\n" }));
    });
  });

  await page.goto("/");
  await page.getByLabel("ACP Bridge Token").fill("e2e-token");
  await page.getByRole("button", { name: "Connect" }).click();

  await expect(page.getByText("main", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /main\.py/i }).click();
  await expect(page.getByTestId("monaco-container")).toBeVisible();

  const promptBox = page.getByLabel("Agent prompt");
  await expect(promptBox).toBeEnabled();
  await promptBox.fill("Update main.py");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.getByRole("dialog", { name: "Tool permission request" })).toBeVisible();
  await page.getByRole("button", { name: "Allow once" }).click();
  await expect(page.getByText("Prepared diff for main.py.")).toBeVisible();

  await page.getByRole("button", { name: "Review Diff" }).click();
  await expect(page.getByTestId("monaco-diff-container")).toBeVisible();
  await page.getByRole("button", { name: "Apply Diff" }).click();
  await expect(page.getByRole("button", { name: "Applied" })).toBeVisible();
  expect(csrfObserved).toBe("e2e-csrf-token");
  expect(files["src/main.py"]).toBe("print('e2e updated')\n");

  await page.getByRole("button", { name: "Memory" }).click();
  await page.getByLabel("Memory learning note").fill("Verified via Playwright");
  await page.getByRole("button", { name: "Add" }).click();
  await expect(page.getByText("Verified via Playwright")).toBeVisible();

  await page.getByRole("button", { name: "Terminal", exact: true }).first().click();
  await expect(page.getByTestId("xterm-container")).toBeVisible();
});
