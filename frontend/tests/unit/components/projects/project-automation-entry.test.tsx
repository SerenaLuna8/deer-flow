import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test, rs } from "@rstest/core";

rs.mock("next/navigation", () => ({
  notFound: rs.fn(() => {
    throw Object.assign(new Error("Not found"), { code: "NEXT_NOT_FOUND" });
  }),
  useSearchParams: () => new URLSearchParams(),
}));
rs.mock("@/core/static-mode", () => ({ isStaticWebsiteOnly: () => false }));

import ProjectAutomationsRoute from "@/app/projects/[project_slug]/automations/page";
import { projectNavigationItems } from "@/components/projects/project-nav";
import { enUS, zhCN } from "@/core/i18n/locales";
import {
  PROJECT_AUTOMATION,
  projectAutomationEntryEnabled,
} from "@/core/projects/features";
import type { Project } from "@/core/projects/types";

const project: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "viewer",
  capabilities: ["project.read", "private_work.read_own"],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

describe("project Automation entry", () => {
  test("enables the M5 compile-time entry only after the release gates", () => {
    expect(PROJECT_AUTOMATION).toBe(true);
  });

  test("opens the entry only when feature, static, capability and readiness gates agree", () => {
    expect(projectAutomationEntryEnabled(true, false, true, "ready")).toBe(
      true,
    );
    expect(projectAutomationEntryEnabled(false, false, true, "ready")).toBe(
      false,
    );
    expect(projectAutomationEntryEnabled(true, true, true, "ready")).toBe(
      false,
    );
    expect(projectAutomationEntryEnabled(true, false, false, "ready")).toBe(
      false,
    );
    expect(
      projectAutomationEntryEnabled(true, false, true, "migration_required"),
    ).toBe(false);
    expect(
      projectAutomationEntryEnabled(true, false, true, "unavailable"),
    ).toBe(false);
    expect(projectAutomationEntryEnabled(true, false, true, undefined)).toBe(
      false,
    );
  });

  test("shows Viewer navigation only when readiness and the feature gate are open", () => {
    expect(
      projectNavigationItems(project, true, true, true, true),
    ).toContainEqual(
      expect.objectContaining({
        href: "/projects/alpha/automations",
        label: "Automations",
      }),
    );
    expect(
      projectNavigationItems(project, true, true, false, true),
    ).not.toContainEqual(expect.objectContaining({ label: "Automations" }));
    expect(
      projectNavigationItems(project, true, true, true, false),
    ).not.toContainEqual(expect.objectContaining({ label: "Automations" }));
    expect(
      projectNavigationItems(project, true, true, true, true, true),
    ).not.toContainEqual(expect.objectContaining({ label: "Automations" }));
    expect(
      projectNavigationItems(
        { ...project, capabilities: ["project.read"] },
        true,
        true,
        true,
        true,
      ),
    ).not.toContainEqual(expect.objectContaining({ label: "Automations" }));
  });

  test("renders the enabled direct URL while retaining the static server gate", () => {
    expect(ProjectAutomationsRoute()).toBeDefined();
    const routeSource = readFileSync(
      resolve(
        process.cwd(),
        "src/app/projects/[project_slug]/automations/page.tsx",
      ),
      "utf8",
    );
    expect(routeSource).toContain(
      "if (!PROJECT_AUTOMATION || isStaticWebsiteOnly()) notFound();",
    );
  });

  test("runs the Playwright release suite without an environment bypass", () => {
    const source = readFileSync(
      resolve(process.cwd(), "tests/e2e/project-automations.spec.ts"),
      "utf8",
    );

    expect(source).toContain(
      'import { PROJECT_AUTOMATION } from "@/core/projects/features";',
    );
    expect(source).not.toContain("test.skip(");
    expect(source).not.toMatch(
      /PLAYWRIGHT_PROJECT_AUTOMATION|NEXT_PUBLIC_PROJECT_AUTOMATION|process\.env/u,
    );
  });

  test("defines every planned Automation label in both supported locales", () => {
    expect(enUS.project.automations).toBe("Automations");
    expect(zhCN.project.automations).toBe("自动化");
    expect(enUS.automation).toEqual({
      create: "Create automation",
      runNow: "Run now",
      schedulerDisabled: "Scheduling is disabled",
      migrationRequired: "Automation migration is required",
      retry: "Retry",
      history: "Run history",
    });
    expect(zhCN.automation).toEqual({
      create: "创建自动化",
      runNow: "立即运行",
      schedulerDisabled: "自动调度当前已关闭",
      migrationRequired: "需要完成自动化迁移",
      retry: "重试",
      history: "运行历史",
    });
  });
});
