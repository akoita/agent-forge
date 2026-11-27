import type {
  ACPConnectionListener,
  ACPConnectionState,
  ACPPermissionListener,
  ACPReadFileHandler,
  ACPTransport,
  ACPUpdateListener,
  InitializeResult,
  PermissionOption,
  PermissionOutcome,
  SessionConfigOption,
  SessionListResult,
  SessionLoadResult,
  SessionNewResult,
  SessionPromptResult,
  SessionUpdate,
} from "../types/acp";

const ACP_PROTOCOL_VERSION = 1;
const DEFAULT_BATCH_INTERVAL_MS = 16;
const FORBIDDEN_QUERY_KEY_PATTERN = /(^|[?&])(token|api_key|access_token|auth)=/i;

export type WebSocketLike = {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  addEventListener(type: "open", listener: () => void): void;
  addEventListener(type: "message", listener: (ev: { data: unknown }) => void): void;
  addEventListener(type: "close", listener: (ev: { code?: number; reason?: string }) => void): void;
  addEventListener(type: "error", listener: () => void): void;
};

export type WebSocketConstructor = new (url: string) => WebSocketLike;

export interface WebSocketACPClientOptions {
  url?: string;
  webSocketFactory?: WebSocketConstructor;
  batchIntervalMs?: number;
}

interface PendingRpc {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
}

interface BufferedDelta {
  sessionId: string;
  text: string;
  timer: ReturnType<typeof setTimeout> | null;
}

interface RpcErrorObject {
  code?: number;
  message?: string;
}

interface RpcFrame {
  jsonrpc?: string;
  id?: number | string;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: RpcErrorObject | null;
}

export function resolveDefaultAcpWsUrl(): string {
  if (typeof window === "undefined" || !window.location) {
    return "ws://127.0.0.1:8765/ws/acp";
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/acp`;
}

function assertSafeWsUrl(rawUrl: string): string {
  if (FORBIDDEN_QUERY_KEY_PATTERN.test(rawUrl)) {
    throw new Error("Credentials must never be passed in WebSocket URL query parameters.");
  }
  return rawUrl;
}

/**
 * Typed ACP JSON-RPC 2.0 WebSocket client with frame-coalesced delta streaming
 * and bidirectional server-request handling (`session/request_permission`, `fs/read_text_file`).
 */
export class WebSocketACPClient implements ACPTransport {
  private readonly url: string;
  private readonly createSocket: WebSocketConstructor;
  private readonly batchIntervalMs: number;

  private ws: WebSocketLike | null = null;
  private connectionState: ACPConnectionState = "disconnected";
  private nextRequestId = 1;
  private pendingRequests = new Map<number, PendingRpc>();
  private updateListeners = new Set<ACPUpdateListener>();
  private permissionListeners = new Set<ACPPermissionListener>();
  private connectionListeners = new Set<ACPConnectionListener>();
  private readFileHandler: ACPReadFileHandler | null = null;
  private deltaBuffer: BufferedDelta | null = null;

  constructor(options: WebSocketACPClientOptions = {}) {
    this.url = assertSafeWsUrl(options.url ?? resolveDefaultAcpWsUrl());
    this.createSocket =
      options.webSocketFactory ?? (globalThis.WebSocket as unknown as WebSocketConstructor);
    this.batchIntervalMs = Math.max(0, options.batchIntervalMs ?? DEFAULT_BATCH_INTERVAL_MS);
  }

  getConnectionState(): ACPConnectionState {
    return this.connectionState;
  }

  private setConnectionState(state: ACPConnectionState, detail?: string): void {
    this.connectionState = state;
    for (const listener of this.connectionListeners) {
      listener(state, detail);
    }
  }

  onSessionUpdate(listener: ACPUpdateListener): () => void {
    this.updateListeners.add(listener);
    return () => {
      this.updateListeners.delete(listener);
    };
  }

  onPermissionRequest(listener: ACPPermissionListener): () => void {
    this.permissionListeners.add(listener);
    return () => {
      this.permissionListeners.delete(listener);
    };
  }

  onConnectionChange(listener: ACPConnectionListener): () => void {
    this.connectionListeners.add(listener);
    return () => {
      this.connectionListeners.delete(listener);
    };
  }

  setReadFileHandler(handler: ACPReadFileHandler | null): void {
    this.readFileHandler = handler;
  }

  async connect(): Promise<void> {
    if (this.ws && this.connectionState === "connected") {
      return;
    }
    this.setConnectionState("connecting");
    assertSafeWsUrl(this.url);

    await new Promise<void>((resolve, reject) => {
      const socket = new this.createSocket(this.url);
      this.ws = socket;
      let settled = false;

      socket.addEventListener("open", () => {
        if (settled) {
          return;
        }
        settled = true;
        this.setConnectionState("connected");
        resolve();
      });

      socket.addEventListener("message", (ev) => {
        this.handleRawMessage(ev.data);
      });

      socket.addEventListener("error", () => {
        if (!settled) {
          settled = true;
          this.setConnectionState("error", "ACP WebSocket connection error");
          reject(new Error("ACP WebSocket connection failed."));
          return;
        }
        this.setConnectionState("error", "ACP WebSocket error");
      });

      socket.addEventListener("close", (ev) => {
        this.flushBufferedDeltas();
        const reason = ev.reason || (ev.code ? `closed (${ev.code})` : "closed");
        this.ws = null;
        this.rejectAllPending(new Error(`ACP WebSocket ${reason}`));
        if (!settled) {
          settled = true;
          this.setConnectionState("error", reason);
          reject(new Error(`ACP WebSocket closed before connecting: ${reason}`));
          return;
        }
        this.setConnectionState("disconnected", reason);
      });
    });
  }

  disconnect(): void {
    this.flushBufferedDeltas();
    const socket = this.ws;
    this.ws = null;
    this.rejectAllPending(new Error("ACP client disconnected"));
    if (socket) {
      socket.close(1000, "client disconnect");
    }
    if (this.connectionState !== "disconnected") {
      this.setConnectionState("disconnected");
    }
  }

  private rejectAllPending(err: Error): void {
    for (const [reqId, pending] of this.pendingRequests.entries()) {
      this.pendingRequests.delete(reqId);
      pending.reject(err);
    }
  }

  private sendFrame(payload: Record<string, unknown>): void {
    if (!this.ws) {
      throw new Error("ACP WebSocket is not connected.");
    }
    this.ws.send(JSON.stringify(payload));
  }

  private call<T>(method: string, params: Record<string, unknown>): Promise<T> {
    const id = this.nextRequestId++;
    return new Promise<T>((resolve, reject) => {
      this.pendingRequests.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
      });
      try {
        this.sendFrame({
          jsonrpc: "2.0",
          id,
          method,
          params,
        });
      } catch (err) {
        this.pendingRequests.delete(id);
        reject(err instanceof Error ? err : new Error("Failed to send ACP request."));
      }
    });
  }

  async initialize(): Promise<InitializeResult> {
    return this.call<InitializeResult>("initialize", {
      protocolVersion: ACP_PROTOCOL_VERSION,
      clientCapabilities: {
        fs: { readTextFile: true, writeTextFile: false },
        terminal: false,
      },
      clientInfo: {
        name: "agent-forge-studio",
        version: "0.1.0",
        title: "Agent Forge Studio",
      },
    });
  }

  async listSessions(cwd?: string, cursor?: string): Promise<SessionListResult> {
    const params: Record<string, unknown> = {};
    if (cwd) {
      params.cwd = cwd;
    }
    if (cursor) {
      params.cursor = cursor;
    }
    return this.call<SessionListResult>("session/list", params);
  }

  async newSession(cwd: string): Promise<SessionNewResult> {
    return this.call<SessionNewResult>("session/new", { cwd, mcpServers: [] });
  }

  async loadSession(sessionId: string, cwd?: string): Promise<SessionLoadResult> {
    const params: Record<string, unknown> = { sessionId, mcpServers: [] };
    if (cwd) {
      params.cwd = cwd;
    }
    return this.call<SessionLoadResult>("session/load", params);
  }

  async sendPrompt(sessionId: string, text: string): Promise<SessionPromptResult> {
    return this.call<SessionPromptResult>("session/prompt", {
      sessionId,
      prompt: [{ type: "text", text }],
    });
  }

  cancelSession(sessionId: string): void {
    this.sendFrame({
      jsonrpc: "2.0",
      method: "session/cancel",
      params: { sessionId },
    });
  }

  async setConfigOption(
    sessionId: string,
    configId: string,
    value: string,
  ): Promise<{ configOptions: SessionConfigOption[] }> {
    return this.call<{ configOptions: SessionConfigOption[] }>("session/set_config_option", {
      sessionId,
      configId,
      value,
    });
  }

  async setMode(sessionId: string, modeId: string): Promise<void> {
    await this.call<Record<string, unknown>>("session/set_mode", { sessionId, modeId });
  }

  respondToPermission(requestId: number, outcome: PermissionOutcome): void {
    this.sendFrame({
      jsonrpc: "2.0",
      id: requestId,
      result: { outcome },
    });
  }

  private emitUpdate(sessionId: string, update: SessionUpdate): void {
    for (const listener of this.updateListeners) {
      listener(sessionId, update);
    }
  }

  private flushBufferedDeltas(): void {
    const buffered = this.deltaBuffer;
    if (!buffered) {
      return;
    }
    this.deltaBuffer = null;
    if (buffered.timer !== null) {
      clearTimeout(buffered.timer);
    }
    if (buffered.text) {
      this.emitUpdate(buffered.sessionId, {
        sessionUpdate: "agent_message_chunk",
        content: { type: "text", text: buffered.text },
      });
    }
  }

  private enqueueDelta(sessionId: string, text: string): void {
    if (this.batchIntervalMs <= 0) {
      this.flushBufferedDeltas();
      this.emitUpdate(sessionId, {
        sessionUpdate: "agent_message_chunk",
        content: { type: "text", text },
      });
      return;
    }
    if (this.deltaBuffer && this.deltaBuffer.sessionId !== sessionId) {
      this.flushBufferedDeltas();
    }
    if (!this.deltaBuffer) {
      const timer = setTimeout(() => this.flushBufferedDeltas(), this.batchIntervalMs);
      this.deltaBuffer = { sessionId, text, timer };
      return;
    }
    this.deltaBuffer.text += text;
  }

  private handleRawMessage(rawData: unknown): void {
    if (typeof rawData !== "string") {
      return;
    }
    for (const line of rawData.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed) {
        continue;
      }
      let frame: RpcFrame;
      try {
        frame = JSON.parse(trimmed) as RpcFrame;
      } catch {
        continue;
      }
      this.dispatchFrame(frame);
    }
  }

  private dispatchFrame(frame: RpcFrame): void {
    if (!frame.method) {
      this.handleResponseFrame(frame);
      return;
    }
    if (frame.id !== undefined && frame.id !== null) {
      void this.handleServerRequest(frame.id, frame.method, frame.params ?? {});
      return;
    }
    if (frame.method === "session/update") {
      const params = frame.params ?? {};
      const sessionId = typeof params.sessionId === "string" ? params.sessionId : "";
      const update = params.update as SessionUpdate | undefined;
      if (!sessionId || !update || typeof update.sessionUpdate !== "string") {
        return;
      }
      if (update.sessionUpdate === "agent_message_chunk") {
        const text = typeof update.content?.text === "string" ? update.content.text : "";
        if (text) {
          this.enqueueDelta(sessionId, text);
        }
        return;
      }
      this.flushBufferedDeltas();
      this.emitUpdate(sessionId, update);
    }
  }

  private handleResponseFrame(frame: RpcFrame): void {
    if (typeof frame.id !== "number") {
      return;
    }
    const pending = this.pendingRequests.get(frame.id);
    if (!pending) {
      return;
    }
    this.flushBufferedDeltas();
    this.pendingRequests.delete(frame.id);
    if (frame.error) {
      const msg = frame.error.message || "ACP request failed";
      pending.reject(new Error(msg));
      return;
    }
    pending.resolve(frame.result ?? {});
  }

  private async handleServerRequest(
    reqId: number | string,
    method: string,
    params: Record<string, unknown>,
  ): Promise<void> {
    if (method === "session/request_permission") {
      this.flushBufferedDeltas();
      if (typeof reqId !== "number") {
        return;
      }
      const sessionId = typeof params.sessionId === "string" ? params.sessionId : "";
      const toolCallRaw = (params.toolCall as Record<string, unknown> | undefined) ?? {};
      const optionsRaw = Array.isArray(params.options) ? (params.options as PermissionOption[]) : [];
      for (const listener of this.permissionListeners) {
        listener({
          requestId: reqId,
          sessionId,
          toolCall: {
            toolCallId:
              typeof toolCallRaw.toolCallId === "string" ? toolCallRaw.toolCallId : undefined,
            title: typeof toolCallRaw.title === "string" ? toolCallRaw.title : "Tool action",
            kind: typeof toolCallRaw.kind === "string" ? toolCallRaw.kind : "other",
            status: typeof toolCallRaw.status === "string" ? toolCallRaw.status : undefined,
            rawInput: toolCallRaw.rawInput,
          },
          options: optionsRaw,
        });
      }
      return;
    }

    if (method === "fs/read_text_file") {
      const path = typeof params.path === "string" ? params.path : "";
      const line = typeof params.line === "number" ? params.line : null;
      const limit = typeof params.limit === "number" ? params.limit : null;
      if (!this.readFileHandler || !path) {
        this.sendFrame({
          jsonrpc: "2.0",
          id: reqId,
          error: { code: -32602, message: "Buffer not open in editor" },
        });
        return;
      }
      try {
        const content = await this.readFileHandler(path, line, limit);
        if (content === null) {
          this.sendFrame({
            jsonrpc: "2.0",
            id: reqId,
            error: { code: -32602, message: "No unsaved buffer available" },
          });
          return;
        }
        this.sendFrame({
          jsonrpc: "2.0",
          id: reqId,
          result: { content },
        });
      } catch (err) {
        this.sendFrame({
          jsonrpc: "2.0",
          id: reqId,
          error: {
            code: -32603,
            message: err instanceof Error ? err.message : "Failed to read editor buffer",
          },
        });
      }
      return;
    }

    this.sendFrame({
      jsonrpc: "2.0",
      id: reqId,
      error: { code: -32601, message: `Unsupported client request method: ${method}` },
    });
  }
}
