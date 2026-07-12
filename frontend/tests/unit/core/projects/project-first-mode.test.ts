import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

import {
  PROJECT_FIRST_MODE,
  workspaceLandingPath,
} from "@/core/projects/features";

describe("project-first routing", () => {
  test("routes normal workspace to projects and preserves static demo", () => {
    expect(PROJECT_FIRST_MODE).toBe(true);
    expect(workspaceLandingPath(false, null)).toBe("/workspace/projects");
    expect(workspaceLandingPath(true, "demo-thread")).toBe(
      "/workspace/chats/demo-thread",
    );
    expect(workspaceLandingPath(true, null)).toBe("/workspace/chats/new");
  });

  test("project home uses an independent authenticated shell", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/app/projects/layout.tsx"),
      "utf8",
    );
    expect(source).toContain("getServerSideUser");
    expect(source).toContain("<QueryClientProvider>");
    expect(source).toContain("<AuthProvider");
    expect(source).not.toContain("WorkspaceContent");
  });
});
