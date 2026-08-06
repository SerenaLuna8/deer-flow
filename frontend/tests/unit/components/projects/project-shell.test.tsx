import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  usePathname: () => "/projects/alpha/agents",
}));

import {
  isProjectNavigationItemActive,
  ProjectDesktopNav,
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

function renderCollapsedDesktopNav(project: Project) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <QueryClientProvider client={new QueryClient()}>
        <ProjectPrivateWorkProvider
          accountId="22222222-2222-4222-8222-222222222222"
          projectId={project.id}
        >
          <ProjectDesktopNav project={project} collapsed footer={null} />
        </ProjectPrivateWorkProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

function renderExpandedDesktopNav(project: Project) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <QueryClientProvider client={new QueryClient()}>
        <ProjectPrivateWorkProvider
          accountId="22222222-2222-4222-8222-222222222222"
          projectId={project.id}
        >
          <ProjectDesktopNav project={project} footer={null} />
        </ProjectPrivateWorkProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

describe("project shell navigation", () => {
  test("keeps overview standalone and groups project governance destinations", () => {
    const items = projectNavigationItems(
      adminProject,
      true,
      true,
      false,
      false,
    );

    expect(items.find((item) => item.label === "项目概览")?.section).toBeNull();
    expect(items.find((item) => item.label === "Memory")?.section).toBe(
      "capabilities",
    );
    expect(
      items
        .filter((item) => item.section === "capabilities")
        .map((item) => item.label),
    ).toEqual(["Agent", "Skill", "MCP", "Memory"]);
    expect(items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          href: "/projects/alpha/credentials",
          label: "项目凭证",
          section: "management",
        }),
        expect.objectContaining({
          href: "/projects/alpha/members",
          label: "项目成员",
          section: "management",
        }),
      ]),
    );
  });

  test("gates all project private-work destinations with readiness", () => {
    const readyItems = projectNavigationItems(
      adminProject,
      true,
      true,
      false,
      false,
    );

    expect(readyItems.map((item) => item.label)).toEqual(
      expect.arrayContaining(["会话", "渠道连接", "Memory"]),
    );
    expect(readyItems).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          href: "/projects/alpha/connections",
          label: "渠道连接",
          section: "management",
        }),
      ]),
    );
    const notReadyLabels = projectNavigationItems(
      adminProject,
      false,
      true,
      false,
      false,
    ).map((item) => item.label);
    const featureDisabledLabels = projectNavigationItems(
      adminProject,
      true,
      false,
      false,
      false,
    ).map((item) => item.label);
    for (const label of ["会话", "渠道连接", "Memory"]) {
      expect(notReadyLabels).not.toContain(label);
      expect(featureDisabledLabels).not.toContain(label);
    }
  });

  test("renders implemented asset destinations from shared_assets.read", () => {
    const html = renderShell(adminProject);

    for (const label of [
      "项目概览",
      "项目成员",
      "项目设置",
      "Agent",
      "Skill",
      "MCP",
      "项目凭证",
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

    expect(html).toContain("ActWeave");
    expect(html).toContain("Alpha Project");
    expect(html).not.toMatch(/>工作<\/p>/u);
    expect(html).toContain("能力");
    expect(html).toContain("项目管理");
    expect(html).toContain('aria-label="收起菜单栏"');
    expect(html).toContain('href="/workspace"');
    expect(html).toContain("返回工作空间");
    expect(html).toMatch(
      /<a[^>]*aria-current="page"[^>]*href="\/projects\/alpha\/agents"/u,
    );
    expect(html).not.toMatch(
      /<a[^>]*aria-current="page"[^>]*href="\/projects\/alpha\/skills"/u,
    );
  });

  test("keeps the desktop divider aligned without repeating project identity", () => {
    const expanded = renderExpandedDesktopNav(adminProject);
    const collapsed = renderCollapsedDesktopNav(adminProject);

    expect(expanded).toContain("ActWeave");
    expect(expanded).not.toContain("Alpha Project");
    expect(expanded).toContain("h-[4.75rem]");
    expect(collapsed).toContain("h-[4.75rem]");
  });

  test("keeps authorized function icons available when the desktop menu is collapsed", () => {
    const html = renderCollapsedDesktopNav(adminProject);

    expect(html).toContain('data-state="collapsed"');
    expect(html).toContain('aria-label="展开菜单栏"');
    for (const label of [
      "项目概览",
      "Agent",
      "Skill",
      "MCP",
      "项目凭证",
      "项目成员",
      "项目设置",
      "返回工作空间",
    ]) {
      expect(html).toContain(`aria-label="${label}"`);
      expect(html).toContain(`title="${label}"`);
    }
    expect(html).toMatch(
      /<a[^>]*aria-label="Agent"[^>]*aria-current="page"[^>]*href="\/projects\/alpha\/agents"/u,
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

  test("gates governance destinations with their exact server capabilities", () => {
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
    const sharedAssetReader = {
      ...adminProject,
      role: "viewer" as const,
      capabilities: [
        "project.read",
        "shared_assets.read",
      ] as Project["capabilities"],
    };
    const memberManager = {
      ...adminProject,
      role: "viewer" as const,
      capabilities: [
        "project.read",
        "project.members.manage",
      ] as Project["capabilities"],
    };
    const credentialApprover = {
      ...sharedAssetReader,
      capabilities: [
        ...sharedAssetReader.capabilities,
        "mcp.credentials.approve",
      ] as Project["capabilities"],
    };

    expect(renderShell(roleOnlyAdmin)).not.toContain("项目成员");
    expect(renderShell(roleOnlyAdmin)).not.toContain("项目设置");
    expect(renderShell(capabilityViewer)).toContain("项目设置");
    expect(renderShell(roleOnlyAdmin)).not.toContain("Agent");
    expect(renderShell(capabilityViewer)).not.toContain("Agent");
    expect(renderShell(sharedAssetReader)).toContain("Agent");
    expect(renderShell(sharedAssetReader)).not.toContain("项目凭证");
    expect(renderShell(memberManager)).toContain("项目成员");
    expect(renderShell(credentialApprover)).toContain("项目凭证");
  });
});
