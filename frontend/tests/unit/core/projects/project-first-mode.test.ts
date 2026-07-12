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

  test("direct project routes guard static mode before mounting clients", () => {
    const workbenchPage = readFileSync(
      resolve(process.cwd(), "src/app/workspace/projects/page.tsx"),
      "utf8",
    );
    const projectLayout = readFileSync(
      resolve(process.cwd(), "src/app/projects/layout.tsx"),
      "utf8",
    );
    expect(workbenchPage).toContain("isStaticWebsiteOnly");
    expect(workbenchPage).toContain('redirect("/workspace")');
    expect(workbenchPage).not.toContain('"use client"');
    expect(projectLayout.indexOf("isStaticWebsiteOnly")).toBeLessThan(
      projectLayout.indexOf("getServerSideUser()"),
    );
  });

  test("workbench client remounts when the authenticated account changes", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/project-workbench-page.tsx",
      ),
      "utf8",
    );
    expect(source).toContain("key={user.id}");
  });

  test("project-first shortcut description names the project workbench", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/workspace/command-palette.tsx"),
      "utf8",
    );
    expect(source).toContain(
      'showLegacyChats ? t.sidebar.newChat : "项目工作台"',
    );
  });
});
