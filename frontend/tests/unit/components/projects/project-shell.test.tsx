import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  usePathname: () => "/projects/alpha/agents",
}));

import {
  isProjectNavigationItemActive,
  projectNavigationItems,
} from "@/components/projects/project-nav";
import { ProjectShell } from "@/components/projects/project-shell";
import { I18nProvider } from "@/core/i18n/context";
import { ProjectPrivateWorkProvider } from "@/core/private-work/provider";
import { CAPABILITIES, type Project } from "@/core/projects/types";

const adminProject: Project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Shared research",
  icon: "folder",
  role: "admin",
  capabilities: [...CAPABILITIES],
  is_pinned: true,
  last_entered_at: "2026-07-12T10:30:00+08:00",
  member_count: 3,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 3, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace",
};

function renderShell(project: Project) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
      <QueryClientProvider client={new QueryClient()}>
        <ProjectPrivateWorkProvider
          accountId="22222222-2222-4222-8222-222222222222"
          projectId={project.id}
        >
          <ProjectShell
            project={project}
            accountEmail="member@example.com"
            systemRole="user"
            onLogout={() => undefined}
          >
            <p>Project content</p>
          </ProjectShell>
        </ProjectPrivateWorkProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

describe("project shell navigation", () => {
  test("gates project Memory and Connections with private-work readiness", () => {
    const readyItems = projectNavigationItems(
      adminProject,
      true,
      true,
      false,
      false,
    );

    expect(readyItems.map((item) => item.label)).toEqual(
      expect.arrayContaining(["会话", "Memory", "Connections"]),
    );
    expect(
      projectNavigationItems(adminProject, false, true, false, false),
    ).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Memory" }),
        expect.objectContaining({ label: "Connections" }),
      ]),
    );
    expect(
      projectNavigationItems(adminProject, true, false, false, false),
    ).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Memory" }),
        expect.objectContaining({ label: "Connections" }),
      ]),
    );
  });

  test("renders implemented M3 asset destinations from shared_assets.read", () => {
    const html = renderShell(adminProject);

    for (const label of [
      "项目概览",
      "成员与邀请",
      "项目设置",
      "Agent",
      "Skill",
      "MCP",
      "Credential",
      "返回工作空间",
      "账户",
    ]) {
      expect(html).toContain(label);
    }
    for (const unavailable of ["私有工作", "自动化"]) {
      expect(html).not.toContain(unavailable);
    }
    expect(html).toContain("Project content");
  });

  test("renders a branded, grouped navigation with the current route announced", () => {
    const html = renderShell(adminProject);

    expect(html).toContain("DeerFlow");
    expect(html).toContain("Alpha Project");
    expect(html).toContain("工作");
    expect(html).toContain("能力");
    expect(html).toContain("项目管理");
    expect(html).toContain('href="/workspace"');
    expect(html).toContain("返回工作空间");
    expect(html).toMatch(
      /<a[^>]*aria-current="page"[^>]*href="\/projects\/alpha\/agents"/u,
    );
    expect(html).not.toMatch(
      /<a[^>]*aria-current="page"[^>]*href="\/projects\/alpha\/skills"/u,
    );
  });

  test("keeps the overview exact while selecting nested destination routes", () => {
    expect(
      isProjectNavigationItemActive(
        "/projects/alpha",
        "/projects/alpha/settings/usage",
      ),
    ).toBe(false);
    expect(
      isProjectNavigationItemActive(
        "/projects/alpha/settings",
        "/projects/alpha/settings/usage",
      ),
    ).toBe(true);
  });

  test("shows members to an active member and settings only from capabilities", () => {
    const roleOnlyAdmin = {
      ...adminProject,
      capabilities: [
        "project.read",
        "project.enter",
      ] as Project["capabilities"],
    };
    const capabilityViewer = {
      ...adminProject,
      role: "viewer" as const,
      capabilities: [
        "project.read",
        "project.update",
      ] as Project["capabilities"],
    };

    expect(renderShell(roleOnlyAdmin)).toContain("成员与邀请");
    expect(renderShell(roleOnlyAdmin)).not.toContain("项目设置");
    expect(renderShell(capabilityViewer)).toContain("项目设置");
    expect(renderShell(roleOnlyAdmin)).not.toContain("Agent");
    expect(renderShell(capabilityViewer)).not.toContain("Agent");
  });
});
