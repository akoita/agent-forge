import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AgentPanel } from "../components/AgentPanel";
import type { TimelineEntry } from "../hooks/useAcpSession";
import type { AvailableCommand, PermissionRequest, SessionConfigOption } from "../types/acp";

describe("AgentPanel streaming markdown, tool cards, slash commands, and approvals", () => {
  it("renders streaming markdown safely, expands code-mode/subagent cards, autocompletes slash commands, and handles approval choices", async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();
    const onChangeConfig = vi.fn();
    const onRespondPermission = vi.fn();
    const onOpenLocation = vi.fn();
    const onCancelTurn = vi.fn();

    const configOptions: SessionConfigOption[] = [
      {
        id: "mode",
        name: "Mode",
        type: "select",
        currentValue: "code",
        options: [
          { value: "code", name: "Code (Prompted)" },
          { value: "code_mode", name: "Code Mode" },
        ],
      },
    ];

    const availableCommands: AvailableCommand[] = [
      { name: "diff", description: "Inspect workspace Git status and diff." },
      { name: "memory", description: "Manage repo memory.", input: { hint: "list | clear" } },
    ];

    const timeline: TimelineEntry[] = [
      { kind: "user", id: "u-1", text: "Fix src/main.py" },
      {
        kind: "assistant",
        id: "a-1",
        text: "Updated [safe link](https://example.com) and blocked [xss](javascript:alert(1)).\n```python\nprint(42)\n```",
      },
      {
        kind: "tool",
        id: "t-1",
        toolCallId: "call-1",
        title: "execute_code [code-mode]",
        toolKind: "execute",
        status: "completed",
        rawInput: { code: "print('ok')" },
        content: [{ type: "diff", path: "src/main.py", oldText: "a=1", newText: "a=2" }],
        locations: [{ path: "src/main.py", line: 10 }],
      },
    ];

    const pendingPermission: PermissionRequest = {
      requestId: 99,
      sessionId: "sess_1",
      toolCall: {
        toolCallId: "perm_1",
        title: "run_shell",
        kind: "execute",
        rawInput: { command: "rm -f tmp.txt" },
      },
      options: [
        { optionId: "allow_once", name: "Allow once", kind: "allow_once" },
        { optionId: "allow_always", name: "Always allow category", kind: "allow_always" },
        { optionId: "reject_once", name: "Reject", kind: "reject_once" },
      ],
    };

    render(
      <AgentPanel
        connected
        connectionState="connected"
        sessions={[{ sessionId: "sess_1", cwd: "/tmp/repo", title: "Session 1" }]}
        activeSessionId="sess_1"
        configOptions={configOptions}
        availableCommands={availableCommands}
        planEntries={[{ content: "Patch bug", priority: "high", status: "in_progress" }]}
        timeline={timeline}
        pendingPermission={pendingPermission}
        promptRunning={false}
        agentError=""
        onDismissError={vi.fn()}
        onSelectSession={vi.fn()}
        onNewSession={vi.fn()}
        onChangeConfig={onChangeConfig}
        onSendPrompt={onSendPrompt}
        onCancelTurn={onCancelTurn}
        onRespondPermission={onRespondPermission}
        onOpenLocation={onOpenLocation}
      />,
    );

    // Safe markdown renders valid https link and neutralizes javascript: scheme
    expect(screen.getByRole("link", { name: "safe link" })).toHaveAttribute(
      "href",
      "https://example.com",
    );
    expect(screen.queryByRole("link", { name: "xss" })).not.toBeInTheDocument();
    expect(screen.getByText("print(42)")).toBeInTheDocument();

    // Tool card shows Code Mode badge, expands details, and jumps to file location
    expect(screen.getByText("Code Mode", { selector: ".tool-feature-badge" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /src\/main\.py:10/i }));
    expect(onOpenLocation).toHaveBeenCalledWith("src/main.py", 10);

    await user.click(screen.getByRole("button", { name: /execute_code \[code-mode\]/i }));
    expect(screen.getByText(/Diff: src\/main\.py/i)).toBeInTheDocument();

    // Config selector change triggers onChangeConfig
    await user.selectOptions(screen.getByLabelText("Mode"), "code_mode");
    expect(onChangeConfig).toHaveBeenCalledWith("mode", "code_mode");

    // Approval dialog responds with optionId
    expect(screen.getByRole("dialog", { name: "Tool permission request" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Allow once" }));
    expect(onRespondPermission).toHaveBeenCalledWith(99, "allow_once");

    // Slash command autocomplete inserts /diff and submits on Enter
    const composer = screen.getByLabelText("Agent prompt");
    await user.type(composer, "/di");
    expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
    await user.keyboard("{Tab}");
    await waitFor(() => {
      expect(composer).toHaveValue("/diff ");
    });
    await user.type(composer, "{Enter}");
    expect(onSendPrompt).toHaveBeenCalledWith("/diff");
  });
});
