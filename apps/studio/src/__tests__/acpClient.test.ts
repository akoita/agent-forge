import { describe, expect, it, vi } from "vitest";
import { WebSocketACPClient, type WebSocketLike } from "../services/acpClient";
import type { SessionUpdate } from "../types/acp";

class MockWebSocket implements WebSocketLike {
  readyState = 1;
  sent: string[] = [];
  private listeners = new Map<string, Array<(ev: Record<string, unknown>) => void>>();

  constructor(public readonly url: string) {}

  addEventListener(type: "open", listener: () => void): void;
  addEventListener(type: "message", listener: (ev: { data: unknown }) => void): void;
  addEventListener(
    type: "close",
    listener: (ev: { code?: number; reason?: string }) => void,
  ): void;
  addEventListener(type: "error", listener: () => void): void;
  addEventListener(type: string, listener: (ev: never) => void): void {
    const existing = this.listeners.get(type) ?? [];
    existing.push(listener as (ev: Record<string, unknown>) => void);
    this.listeners.set(type, existing);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code = 1000, reason = "closed"): void {
    this.emit("close", { code, reason });
  }

  emitOpen(): void {
    this.emit("open", {});
  }

  emitMessage(frame: Record<string, unknown>): void {
    this.emit("message", { data: `${JSON.stringify(frame)}\n` });
  }

  private emit(type: string, ev: Record<string, unknown>): void {
    for (const cb of this.listeners.get(type) ?? []) {
      cb(ev);
    }
  }
}

function createHarness() {
  let latestSocket: MockWebSocket | null = null;
  const client = new WebSocketACPClient({
    url: "ws://127.0.0.1:8765/ws/acp",
    batchIntervalMs: 0,
    webSocketFactory: function MockFactory(url: string) {
      latestSocket = new MockWebSocket(url);
      return latestSocket;
    } as unknown as new (url: string) => WebSocketLike,
  });
  return {
    client,
    getSocket: () => {
      if (!latestSocket) {
        throw new Error("Socket not constructed yet");
      }
      return latestSocket;
    },
  };
}

describe("WebSocketACPClient JSON-RPC 2.0 contract", () => {
  it("forbids credentials in WebSocket URL query parameters", () => {
    expect(
      () => new WebSocketACPClient({ url: "ws://127.0.0.1:8765/ws/acp?token=leak" }),
    ).toThrow(/query parameters/i);
  });

  it("handles initialize, session lifecycle, load replay, prompt streaming, cancel, and config updates", async () => {
    const { client, getSocket } = createHarness();
    const updates: Array<{ sessionId: string; update: SessionUpdate }> = [];
    client.onSessionUpdate((sessionId, update) => updates.push({ sessionId, update }));

    const connPromise = client.connect();
    const sock = getSocket();
    sock.emitOpen();
    await connPromise;

    const initPromise = client.initialize();
    const initReq = JSON.parse(sock.sent[0]) as { id: number; method: string };
    expect(initReq.method).toBe("initialize");
    sock.emitMessage({
      jsonrpc: "2.0",
      id: initReq.id,
      result: {
        protocolVersion: 1,
        agentCapabilities: { loadSession: true },
        agentInfo: { name: "agent-forge", version: "0.8.0" },
      },
    });
    const initRes = await initPromise;
    expect(initRes.protocolVersion).toBe(1);

    const newPromise = client.newSession("/tmp/repo");
    const newReq = JSON.parse(sock.sent[1]) as { id: number; method: string };
    expect(newReq.method).toBe("session/new");
    sock.emitMessage({
      jsonrpc: "2.0",
      id: newReq.id,
      result: { sessionId: "sess_1", configOptions: [] },
    });
    const created = await newPromise;
    expect(created.sessionId).toBe("sess_1");

    const loadPromise = client.loadSession("sess_prev", "/tmp/repo");
    const loadReq = JSON.parse(sock.sent[2]) as { id: number; method: string };
    sock.emitMessage({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: "sess_prev",
        update: {
          sessionUpdate: "user_message_chunk",
          content: { type: "text", text: "Fix app.py" },
        },
      },
    });
    sock.emitMessage({
      jsonrpc: "2.0",
      id: loadReq.id,
      result: { configOptions: [] },
    });
    await loadPromise;
    expect(updates[0].update.sessionUpdate).toBe("user_message_chunk");

    const promptPromise = client.sendPrompt("sess_1", "Inspect files");
    const promptReq = JSON.parse(sock.sent[3]) as { id: number; method: string };
    sock.emitMessage({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: "sess_1",
        update: {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text: "Done." },
        },
      },
    });
    sock.emitMessage({
      jsonrpc: "2.0",
      id: promptReq.id,
      result: { stopReason: "end_turn" },
    });
    const promptRes = await promptPromise;
    expect(promptRes.stopReason).toBe("end_turn");

    client.cancelSession("sess_1");
    const cancelFrame = JSON.parse(sock.sent[4]) as { id?: number; method: string };
    expect(cancelFrame.method).toBe("session/cancel");
    expect(cancelFrame.id).toBeUndefined();

    const cfgPromise = client.setConfigOption("sess_1", "mode", "code_mode");
    const cfgReq = JSON.parse(sock.sent[5]) as { id: number; method: string };
    expect(cfgReq.method).toBe("session/set_config_option");
    sock.emitMessage({
      jsonrpc: "2.0",
      id: cfgReq.id,
      result: { configOptions: [] },
    });
    await cfgPromise;
  });

  it("handles bidirectional session/request_permission and fs/read_text_file server requests", async () => {
    const { client, getSocket } = createHarness();
    const permSpy = vi.fn();
    client.onPermissionRequest(permSpy);
    client.setReadFileHandler((filePath) =>
      filePath.endsWith("src/main.py") ? "UNSAVED_BUFFER" : null,
    );

    const connPromise = client.connect();
    const sock = getSocket();
    sock.emitOpen();
    await connPromise;

    sock.emitMessage({
      jsonrpc: "2.0",
      id: 42,
      method: "session/request_permission",
      params: {
        sessionId: "sess_1",
        toolCall: { toolCallId: "perm_1", title: "run_shell", kind: "execute", rawInput: {} },
        options: [{ optionId: "allow_once", name: "Allow once", kind: "allow_once" }],
      },
    });
    expect(permSpy).toHaveBeenCalledTimes(1);
    client.respondToPermission(42, { outcome: "selected", optionId: "allow_once" });
    expect(JSON.parse(sock.sent[0])).toEqual({
      jsonrpc: "2.0",
      id: 42,
      result: { outcome: { outcome: "selected", optionId: "allow_once" } },
    });

    sock.emitMessage({
      jsonrpc: "2.0",
      id: 43,
      method: "fs/read_text_file",
      params: { sessionId: "sess_1", path: "/workspace/src/main.py" },
    });
    await Promise.resolve();
    expect(JSON.parse(sock.sent[1])).toEqual({
      jsonrpc: "2.0",
      id: 43,
      result: { content: "UNSAVED_BUFFER" },
    });

    sock.emitMessage({
      jsonrpc: "2.0",
      id: 44,
      method: "fs/read_text_file",
      params: { sessionId: "sess_1", path: "/workspace/missing.py" },
    });
    await Promise.resolve();
    const missReply = JSON.parse(sock.sent[2]) as { error: { code: number } };
    expect(missReply.error.code).toBe(-32602);
  });
});
