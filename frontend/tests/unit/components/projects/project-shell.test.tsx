import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  usePathname: () => "/projects/alpha/agents",
}));

import { ProjectHome } from "@/components/projects/project-home";
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

const runnerProject: Project = {
  ...adminProject,
  role: "runner",
  capabilities: [
    "project.read",
    "project.enter",
    "project.pin",
    "private_work.create",
    "private_work.read_own",
    "automation.manage_own",
    "shared_assets.read",
    "shared_assets.execute",
  ],
};

const editorProject: Project = {
  ...runnerProject,
  role: "editor",
  capabilities: [...runnerProject.capabilities, "shared_assets.edit"],
};

const viewerProject: Project = {
  ...runnerProject,
  role: "viewer",
  capabilities: [
    "project.read",
    "project.enter",
    "project.pin",
    "private_work.read_own",
    "shared_assets.read",
  ],
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
            accountUsername="member"
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
  test("exposes project menu sections by the member's effective authority", () => {
    const runnerItems = projectNavigationItems(
      runnerProject,
      true,
      true,
      true,
      true,
    );
    const editorItems = projectNavigationItems(
      editorProject,
      true,
      true,
      true,
      true,
    );
    const adminItems = projectNavigationItems(
      adminProject,
      true,
      true,
      true,
      true,
    );
    const viewerItems = projectNavigationItems(
      viewerProject,
      true,
      true,
      true,
      true,
    );

    expect(new Set(runnerItems.map((item) => item.section))).toEqual(
      new Set([null, "work"]),
    );
    expect(
      runnerItems
        .filter((item) => item.section === "work")
        .map((item) => item.label),
    ).toEqual(["会话", "Automations", "Memory"]);
    expect(new Set(viewerItems.map((item) => item.section))).toEqual(
      new Set([null, "work"]),
    );
    expect(new Set(editorItems.map((item) => item.section))).toEqual(
      new Set([null, "work", "capabilities"]),
    );
    expect(
      editorItems
        .filter((item) => item.section === "capabilities")
        .map((item) => item.label),
    ).toEqual(["Agent", "Skill", "MCP"]);
    expect(new Set(adminItems.map((item) => item.section))).toEqual(
      new Set([null, "work", "capabilities", "management"]),
    );
    expect(
      adminItems
        .filter((item) => item.section === "management")
        .map((item) => item.label),
    ).toContain("渠道连接");
    for (const items of [runnerItems, editorItems, viewerItems]) {
      expect(items.map((item) => item.label)).not.toContain("渠道连接");
    }
  });

  test("keeps capability management cards out of runner and viewer overviews", () => {
    expect(
      renderToStaticMarkup(<ProjectHome project={runnerProject} />),
    ).not.toContain("共享资产");
    expect(
      renderToStaticMarkup(<ProjectHome project={viewerProject} />),
    ).not.toContain("共享资产");
    expect(
      renderToStaticMarkup(<ProjectHome project={editorProject} />),
    ).toContain("共享资产");
  });

  test("keeps overview standalone and groups project governance destinations", () => {
    const items = projectNavigationItems(
      adminProject,
      true,
      true,
      false,
      false,
    );

    expect(items.find((item) => item.label === "项目概览")?.section).toBeNull();
    expect(items.find((item) => item.label === "Memory")?.section).toBe("work");
    expect(
      items
        .filter((item) => item.section === "capabilities")
        .map((item) => item.label),
    ).toEqual(["Agent", "Skill", "MCP"]);
    expect(items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          href: "/projects/alpha/connections",
          label: "渠道连接",
          section: "management",
        }),
        expect.objectContaining({
          href: "/projects/alpha/credentials",
          label: "项目凭证",
          section: "management",
        }),
        expect.objectContaining({
          href: "/projects/alpha/audit",
          label: "审计日志",
          section: "management",
        }),
        expect.objectContaining({
          href: "/projects/alpha/settings",
          label: "项目设置",
          section: "management",
        }),
      ]),
    );
    expect(items.map((item) => item.label)).not.toContain("项目成员");
  });

  test("gates member work with readiness without hiding channel governance", () => {
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
    for (const label of ["会话", "Memory"]) {
      expect(notReadyLabels).not.toContain(label);
      expect(featureDisabledLabels).not.toContain(label);
    }
    expect(notReadyLabels).toContain("渠道连接");
    expect(featureDisabledLabels).not.toContain("渠道连接");
  });

  test("renders implemented asset destinations from authoring authority", () => {
    const html = renderShell(adminProject);

    expect(html).toContain("md:grid-cols-[15rem_minmax(0,1fr)]");
    expect(html).toContain("bg-blue-50 text-blue-600 before:bg-blue-600");
    expect(html).toContain("hover:bg-blue-50");
    expect(html).not.toContain("hover:text-blue-600");
    expect(html).toContain('data-slot="project-brand-logo"');
    expect(html).not.toContain("项目空间");
    for (const label of [
      "项目概览",
      "审计日志",
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
    expect(html).not.toContain("项目成员");
    for (const unavailable of ["私有工作", "自动化"]) {
      expect(html).not.toContain(unavailable);
    }
    expect(html).toContain("Project content");
  });

  test("announces the current route", () => {
    const html = renderShell(adminProject);

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
        "/projects/alpha/settings/members",
      ),
    ).toBe(false);
    expect(
      isProjectNavigationItemActive(
        "/projects/alpha/settings",
        "/projects/alpha/settings/members",
      ),
    ).toBe(true);
    expect(
      isProjectNavigationItemActive(
        "/projects/alpha/audit",
        "/projects/alpha/audit",
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
    const sharedAssetEditor = {
      ...sharedAssetReader,
      capabilities: [
        ...sharedAssetReader.capabilities,
        "shared_assets.edit",
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
    const channelManager = {
      ...roleOnlyAdmin,
      role: "viewer" as const,
      capabilities: [
        ...roleOnlyAdmin.capabilities,
        "project.channels.manage",
      ] as Project["capabilities"],
    };

    expect(renderShell(roleOnlyAdmin)).not.toContain("项目设置");
    expect(renderShell(capabilityViewer)).toContain("项目设置");
    expect(renderShell(roleOnlyAdmin)).not.toContain("Agent");
    expect(renderShell(roleOnlyAdmin)).not.toContain("渠道连接");
    expect(renderShell(channelManager)).toContain("渠道连接");
    expect(renderShell(capabilityViewer)).not.toContain("Agent");
    expect(renderShell(sharedAssetReader)).not.toContain("Agent");
    expect(renderShell(sharedAssetEditor)).toContain("Agent");
    expect(renderShell(sharedAssetReader)).not.toContain("项目凭证");
    expect(renderShell(memberManager)).toContain("项目设置");
    expect(renderShell(memberManager)).not.toContain("项目成员");
    expect(
      projectNavigationItems(memberManager, false, true, false, false).find(
        (item) => item.label === "项目设置",
      )?.href,
    ).toBe("/projects/alpha/settings/members");
    expect(renderShell(credentialApprover)).toContain("项目凭证");
  });
});
