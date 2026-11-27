import "@xterm/xterm/css/xterm.css";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { useCallback, useEffect, useRef, useState } from "react";
import type { WebSocketConstructor, WebSocketLike } from "../services/acpClient";

const FORBIDDEN_QUERY_KEY_PATTERN = /(^|[?&])(token|api_key|access_token|auth)=/i;

export function resolveDefaultTerminalWsUrl(): string {
  if (typeof window === "undefined" || !window.location) {
    return "ws://127.0.0.1:8765/ws/terminal";
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/terminal`;
}

function assertSafeWsUrl(rawUrl: string): string {
  if (FORBIDDEN_QUERY_KEY_PATTERN.test(rawUrl)) {
    throw new Error("Credentials must never be passed in WebSocket URL query parameters.");
  }
  return rawUrl;
}

interface TerminalPanelProps {
  open: boolean;
  connected: boolean;
  wsUrl?: string;
  webSocketFactory?: WebSocketConstructor;
  onClose: () => void;
}

export function TerminalPanel({
  open,
  connected,
  wsUrl,
  webSocketFactory,
  onClose,
}: TerminalPanelProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocketLike | null>(null);

  const [status, setStatus] = useState("Disconnected");
  const [errorMsg, setErrorMsg] = useState("");
  const [reconnectSeq, setReconnectSeq] = useState(0);

  const sendResize = useCallback(() => {
    const term = termRef.current;
    const ws = wsRef.current;
    if (!term || !ws || ws.readyState !== 1) {
      return;
    }
    ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }, []);

  useEffect(() => {
    if (!open || !connected || !hostRef.current) {
      return;
    }

    let safeUrl: string;
    try {
      safeUrl = assertSafeWsUrl(wsUrl ?? resolveDefaultTerminalWsUrl());
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Invalid terminal WebSocket URL.");
      setStatus("Error");
      return;
    }

    const term = new Terminal({
      convertEol: true,
      fontSize: 12,
      theme: {
        background: "#0f1117",
        foreground: "#e6edf3",
      },
    });
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(hostRef.current);
    fitAddon.fit();
    termRef.current = term;
    fitRef.current = fitAddon;

    const Factory =
      webSocketFactory ?? (globalThis.WebSocket as unknown as WebSocketConstructor);
    const socket = new Factory(safeUrl);
    wsRef.current = socket;
    setErrorMsg("");
    setStatus("Connecting...");

    socket.addEventListener("open", () => {
      setStatus("Connected");
      fitAddon.fit();
      socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    });

    socket.addEventListener("message", (ev) => {
      if (typeof ev.data !== "string") {
        return;
      }
      try {
        const msg = JSON.parse(ev.data) as { type?: string; data?: unknown; code?: unknown };
        if (msg.type === "output" && typeof msg.data === "string") {
          term.write(msg.data);
        } else if (msg.type === "exit") {
          const exitCode = typeof msg.code === "number" ? msg.code : 0;
          setStatus(`Exited (${exitCode})`);
        }
      } catch {
        // Ignore malformed non-JSON messages
      }
    });

    socket.addEventListener("error", () => {
      setErrorMsg("Terminal WebSocket error.");
      setStatus("Error");
    });

    socket.addEventListener("close", (ev) => {
      setStatus(ev.reason ? `Closed (${ev.reason})` : "Disconnected");
    });

    const inputSub = term.onData((data) => {
      if (socket.readyState === 1) {
        socket.send(JSON.stringify({ type: "input", data }));
      }
    });

    function onWindowResize(): void {
      fitAddon.fit();
      sendResize();
    }
    window.addEventListener("resize", onWindowResize);

    return () => {
      window.removeEventListener("resize", onWindowResize);
      inputSub.dispose();
      socket.close(1000, "panel closed");
      term.dispose();
      wsRef.current = null;
      termRef.current = null;
      fitRef.current = null;
    };
  }, [connected, open, reconnectSeq, sendResize, webSocketFactory, wsUrl]);

  if (!open) {
    return null;
  }

  return (
    <section className="terminal-drawer" aria-label="Integrated Terminal">
      <div className="terminal-bar">
        <div className="terminal-meta">
          <span className="terminal-title">Terminal (PTY)</span>
          <span className="terminal-status">{status}</span>
        </div>
        <div className="terminal-actions">
          <button type="button" className="btn-secondary" onClick={() => termRef.current?.clear()}>
            Clear
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={!connected}
            onClick={() => setReconnectSeq((n) => n + 1)}
          >
            Reconnect
          </button>
          <button type="button" className="btn-secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      {errorMsg ? (
        <div className="agent-error-banner" role="alert">
          <span>{errorMsg}</span>
        </div>
      ) : null}
      <div ref={hostRef} className="terminal-host" data-testid="xterm-container" />
    </section>
  );
}
