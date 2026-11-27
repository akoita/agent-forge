import { describe, expect, it } from "vitest";
import type { TreeEntry } from "../types/workspace";
import {
  buildFileTree,
  detectLanguage,
  formatGitBadge,
  fuzzyFilterFiles,
  fuzzyMatchScore,
} from "../utils/fuzzy";

const SAMPLE_ENTRIES: TreeEntry[] = [
  { path: "src", name: "src", kind: "directory", size: 0, mtime_ns: 1 },
  { path: "src/App.tsx", name: "App.tsx", kind: "file", size: 120, mtime_ns: 1 },
  { path: "src/utils/fuzzy.ts", name: "fuzzy.ts", kind: "file", size: 90, mtime_ns: 1 },
  { path: "src/utils", name: "utils", kind: "directory", size: 0, mtime_ns: 1 },
  { path: "README.md", name: "README.md", kind: "file", size: 40, mtime_ns: 1 },
];

describe("fuzzy utilities", () => {
  it("scores direct basename matches above looser subsequence matches", () => {
    const direct = fuzzyMatchScore("fuzzy", "src/utils/fuzzy.ts");
    const loose = fuzzyMatchScore("fuzzy", "foo/u_z_z/y.py");
    expect(direct).toBeGreaterThan(loose);
    expect(fuzzyMatchScore("xyz", "src/App.tsx")).toBe(-1);
  });

  it("filters only files in ranked order", () => {
    const filtered = fuzzyFilterFiles(SAMPLE_ENTRIES, "app");
    expect(filtered.map((item) => item.path)).toEqual(["src/App.tsx"]);
  });

  it("builds a hierarchical tree with directories nested properly", () => {
    const roots = buildFileTree(SAMPLE_ENTRIES);
    expect(roots.map((n) => n.entry.path)).toEqual(["src", "README.md"]);
    const srcNode = roots[0];
    expect(srcNode.children.map((n) => n.entry.path)).toEqual(["src/utils", "src/App.tsx"]);
    expect(srcNode.children[0].children.map((n) => n.entry.path)).toEqual(["src/utils/fuzzy.ts"]);
  });

  it("detects Monaco language identifiers and Git badges", () => {
    expect(detectLanguage("agent_forge/cli.py")).toBe("python");
    expect(detectLanguage("apps/studio/src/App.tsx")).toBe("typescript");
    expect(detectLanguage("Dockerfile")).toBe("dockerfile");
    expect(detectLanguage("unknown.binxyz")).toBe("plaintext");

    expect(formatGitBadge({ index: "?", worktree: "?", path: "a" })).toBe("U");
    expect(formatGitBadge({ index: " ", worktree: "M", path: "a" })).toBe("M");
    expect(formatGitBadge({ index: "A", worktree: " ", path: "a" })).toBe("A");
    expect(formatGitBadge(undefined)).toBe("");
  });
});
