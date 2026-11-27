import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MemoryPanel } from "../components/MemoryPanel";
import type { MemoryEntry } from "../types/workspace";

describe("MemoryPanel", () => {
  it("lists, filters, adds, removes, and confirms Clear All without native modals", async () => {
    const user = userEvent.setup();
    const entries: MemoryEntry[] = [
      {
        id: "mem-11112222",
        category: "testing",
        text: "Run pytest before PR",
        created_at: "2026-11-27",
      },
      {
        id: "mem-33334444",
        category: "architecture",
        text: "Keep core domain-agnostic",
        created_at: "2026-11-27",
      },
    ];
    const onAdd = vi.fn();
    const onRemove = vi.fn();
    const onClear = vi.fn();
    const onOpenRawFile = vi.fn();

    render(
      <MemoryPanel
        open
        connected
        memoryPath=".agent-forge/memory.md"
        entries={entries}
        loading={false}
        onAdd={onAdd}
        onRemove={onRemove}
        onClear={onClear}
        onRefresh={vi.fn()}
        onOpenRawFile={onOpenRawFile}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Run pytest before PR")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Filter repository memory"), "pytest");
    expect(screen.queryByText("Keep core domain-agnostic")).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("Filter repository memory"));

    await user.selectOptions(screen.getByLabelText("Memory category"), "security");
    await user.type(screen.getByLabelText("Memory learning note"), "Use HttpOnly cookies");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(onAdd).toHaveBeenCalledWith("Use HttpOnly cookies", "security");

    await user.click(screen.getByRole("button", { name: "Remove mem-11112222" }));
    expect(onRemove).toHaveBeenCalledWith("mem-11112222");

    await user.click(screen.getByRole("button", { name: "Clear All" }));
    expect(onClear).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Confirm Clear" }));
    expect(onClear).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Open File" }));
    expect(onOpenRawFile).toHaveBeenCalledWith(".agent-forge/memory.md");
  });
});
