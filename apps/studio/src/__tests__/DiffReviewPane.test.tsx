import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DiffReviewPane } from "../components/DiffReviewPane";
import type { PendingDiffItem } from "../types/workspace";

describe("DiffReviewPane", () => {
  it("renders diff review, switches diffs, and triggers Apply/Undo/Close", async () => {
    const user = userEvent.setup();
    const diffs: PendingDiffItem[] = [
      {
        id: "call-1:src/main.py",
        toolCallId: "call-1",
        title: "edit_file",
        path: "src/main.py",
        oldText: "a = 1\n",
        newText: "a = 2\n",
        applied: false,
      },
      {
        id: "call-2:src/helper.py",
        toolCallId: "call-2",
        title: "edit_file",
        path: "src/helper.py",
        oldText: "b = 1\n",
        newText: "b = 3\n",
        applied: false,
      },
    ];

    const onSelectDiff = vi.fn();
    const onApplyDiff = vi.fn();
    const onUndoDiff = vi.fn();
    const onClose = vi.fn();

    render(
      <DiffReviewPane
        diffs={diffs}
        activeDiffId="call-1:src/main.py"
        applying={false}
        onSelectDiff={onSelectDiff}
        onApplyDiff={onApplyDiff}
        onUndoDiff={onUndoDiff}
        onClose={onClose}
        diffRenderer={({ diff }) => (
          <div data-testid="fake-diff">
            {diff.oldText} =&gt; {diff.newText}
          </div>
        )}
      />,
    );

    expect(screen.getByText("src/main.py")).toBeInTheDocument();
    expect(screen.getByTestId("fake-diff")).toHaveTextContent("a = 1 => a = 2");

    await user.click(screen.getByRole("button", { name: "Apply Diff" }));
    expect(onApplyDiff).toHaveBeenCalledWith(diffs[0]);

    await user.click(screen.getByRole("button", { name: "Undo Diff" }));
    expect(onUndoDiff).toHaveBeenCalledWith(diffs[0]);

    await user.selectOptions(screen.getByLabelText("Select pending diff"), "call-2:src/helper.py");
    expect(onSelectDiff).toHaveBeenCalledWith("call-2:src/helper.py");

    await user.click(screen.getByRole("button", { name: "Close Diff" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
