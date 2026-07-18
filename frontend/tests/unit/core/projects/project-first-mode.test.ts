import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

import {
  PROJECT_FIRST_MODE,
  workspaceLandingPath,
} from "@/core/projects/features";

describe("project-first routing", () => {
  test("routes every session to the project workbench", () => {
    expect(PROJECT_FIRST_MODE).toBe(true);
    expect(workspaceLandingPath(false, null)).toBe("/workspace");
    expect(workspaceLandingPath(true, "demo-thread")).toBe("/workspace");
    expect(workspaceLandingPath(true, null)).toBe("/workspace");
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

  test("the old workbench alias is absent and static rejection precedes auth", () => {
    const projectLayout = readFileSync(
      resolve(process.cwd(), "src/app/projects/layout.tsx"),
      "utf8",
    );
    expect(
      existsSync(resolve(process.cwd(), "src/app/workspace/projects/page.tsx")),
    ).toBe(false);
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

  test("project navigation returns only to the canonical workbench", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-nav.tsx"),
      "utf8",
    );
    expect(source).toContain('href="/workspace"');
    expect(source).toContain("返回工作空间");
    expect(source).not.toContain('"/workspace/projects"');
    expect(source).not.toContain("项目工作台");
  });
});
