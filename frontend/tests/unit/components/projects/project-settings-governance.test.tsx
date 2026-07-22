import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

import { projectNavigationItems } from "@/components/projects/project-nav";
import {
  PROJECT_GENERAL_SETTINGS_FIELDS,
  projectGeneralSettingsInput,
} from "@/components/projects/settings/project-general-settings";
import { projectSettingsNavigationItems } from "@/components/projects/settings/project-settings-shell";
import type { Project } from "@/core/projects/types";

const project: Project = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  slug: "alpha",
  display_name: "Alpha",
  description: "Research workspace",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.update",
    "project.lifecycle.manage",
    "project.usage.read",
    "project.audit.read",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 2,
  agent_count: 1,
  skill_count: 1,
  mcp_count: 1,
  quota_summary: {
    members: { used: 2, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "settings-test",
};

describe("project settings governance shell", () => {
  test("keeps governance under one project navigation entry", () => {
    const topLevel = projectNavigationItems(
      project,
      false,
      false,
      false,
      false,
      false,
      true,
      true,
    );

    expect(topLevel).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          href: "/projects/alpha/settings",
          label: "项目设置",
        }),
      ]),
    );
    expect(topLevel.map((item) => item.href)).not.toEqual(
      expect.arrayContaining([
        "/projects/alpha/settings/usage",
        "/projects/alpha/settings/audit",
      ]),
    );
  });

  test("builds capability-scoped settings tabs without invented sections", () => {
    expect(projectSettingsNavigationItems(project, false)).toEqual([
      expect.objectContaining({
        href: "/projects/alpha/settings",
        label: "常规设置",
      }),
      expect.objectContaining({
        href: "/projects/alpha/settings/usage",
        label: "用量与限额",
      }),
      expect.objectContaining({
        href: "/projects/alpha/settings/audit",
        label: "审计日志",
      }),
    ]);

    expect(projectSettingsNavigationItems(project, true)).toEqual([
      expect.objectContaining({ label: "常规设置" }),
    ]);
    expect(
      projectSettingsNavigationItems(
        { ...project, capabilities: ["project.read"] },
        false,
      ),
    ).toEqual([]);
  });

  test("general settings only exposes supported project fields", () => {
    expect(PROJECT_GENERAL_SETTINGS_FIELDS).toEqual([
      "display_name",
      "icon",
      "description",
    ]);

    const form = new FormData();
    form.set("display_name", "  Alpha Team  ");
    form.set("icon", " folder ");
    form.set("description", "  Research workspace  ");
    form.set("default_model", "not-supported");
    expect(projectGeneralSettingsInput(form)).toEqual({
      display_name: "Alpha Team",
      icon: "folder",
      description: "Research workspace",
    });
  });

  test("settings layout owns the shared shell and lifecycle copy is product-facing", () => {
    const layout = readFileSync(
      resolve(
        process.cwd(),
        "src/app/projects/[project_slug]/settings/layout.tsx",
      ),
      "utf8",
    );
    const page = readFileSync(
      resolve(
        process.cwd(),
        "src/app/projects/[project_slug]/settings/page.tsx",
      ),
      "utf8",
    );
    const lifecycle = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/settings/project-lifecycle-panel.tsx",
      ),
      "utf8",
    );

    expect(layout).toContain("ProjectSettingsShell");
    expect(page).toContain("ProjectGeneralSettings");
    expect(page).toContain("ProjectLifecyclePanel");
    expect(lifecycle).toContain("30 天恢复窗口");
    expect(lifecycle).toContain("恢复窗口结束后将无法自助恢复");
    expect(lifecycle).not.toMatch(/M2|物理清除/u);
  });
});
