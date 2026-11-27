import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TerminalPanel } from "../components/TerminalPanel";
import type { WebSocketLike } from "../services/acpClient";

class MockTermSocket implements WebSocketLike {
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

  close(): void {}

  emitOpen(): void {
    for (const cb of this.listeners.get("open") ?? []) cb({});
  }

  emitJson(payload: Record<string, unknown>): void {
    for (const cb of this.listeners.get("message") ?? []) {
      cb({ data: JSON.stringify(payload) });
    }
  }
}

describe("TerminalPanel", () => {
  it("connects without URL tokens, sends resize frame on open, and handles exit events", () => {
    let sock: MockTermSocket | null = null;
    render(
      <TerminalPanel
        open
        connected
        wsUrl="ws://127.0.0.1:8765/ws/terminal"
        webSocketFactory={function TermFactory(url: string) {
          sock = new MockTermSocket(url);
          return sock;
        } as unknown as new (url: string) => WebSocketLike}
        onClose={() => {}}
      />,
    );

    expect(sock).not.toBeNull();
    act(() => {
      sock!.emitOpen();
    });
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(JSON.parse(sock!.sent[0])).toEqual({ type: "resize", cols: 80, rows: 24 });

    act(() => {
      sock!.emitJson({ type: "output", data: "hello\n" });
      sock!.emitJson({ type: "exit", code: 0 });
    });
    expect(screen.getByText("Exited (0)")).toBeInTheDocument();
  });

  it("rejects WebSocket URLs carrying query tokens", () => {
    render(
      <TerminalPanel
        open
        connected
        wsUrl="ws://127.0.0.1:8765/ws/terminal?token=bad"
        onClose={() => {}}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/query parameters/i);
  });
});
