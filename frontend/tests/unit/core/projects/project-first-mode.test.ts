import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

import {
  PROJECT_FIRST_MODE,
  workspaceLandingPath,
} from "@/core/projects/features";

describe("project-first routing", () => {
  test("routes normal sessions to the workspace and preserves static demo", () => {
    expect(PROJECT_FIRST_MODE).toBe(true);
    expect(workspaceLandingPath(false, null)).toBe("/workspace");
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
    expect(source).toContain("if (isStaticWebsiteOnly()) notFound()");
    expect(source).not.toContain("WorkspaceContent");
  });

  test("the old workbench alias redirects before mounting clients", () => {
    const workbenchPage = readFileSync(
      resolve(process.cwd(), "src/app/workspace/projects/page.tsx"),
      "utf8",
    );
    const projectLayout = readFileSync(
      resolve(process.cwd(), "src/app/projects/layout.tsx"),
      "utf8",
    );
    expect(workbenchPage).toContain('redirect("/workspace")');
    expect(workbenchPage).not.toContain("ProjectWorkbenchPage");
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

  test("direct workspace retains gateway-offline recovery", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/project-workbench-page.tsx",
      ),
      "utf8",
    );
    expect(source).toContain("<GatewayOfflineBanner gatewayUnavailable />");
  });

  test("project-first navigation uses the canonical workspace name and path", () => {
    for (const file of [
      "src/components/workspace/command-palette.tsx",
      "src/components/workspace/workspace-header.tsx",
      "src/components/workspace/workspace-nav-chat-list.tsx",
    ]) {
      const source = readFileSync(resolve(process.cwd(), file), "utf8");
      expect(source).toContain('"/workspace"');
      expect(source).toContain("工作空间");
      expect(source).not.toContain('"/workspace/projects"');
      expect(source).not.toContain("项目工作台");
    }
  });
});
