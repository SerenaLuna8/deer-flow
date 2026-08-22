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
      new Set([null, "work", "capabilities"]),
    );
    expect(
      runnerItems
        .filter((item) => item.section === "work")
        .map((item) => item.id),
    ).toEqual(["conversations", "automations", "memory"]);
    expect(new Set(viewerItems.map((item) => item.section))).toEqual(
      new Set([null, "work", "capabilities"]),
    );
    expect(
      runnerItems
        .filter((item) => item.section === "capabilities")
        .map((item) => item.id),
    ).toEqual(["agents"]);
    expect(
      viewerItems
        .filter((item) => item.section === "capabilities")
        .map((item) => item.id),
    ).toEqual(["agents"]);
    expect(new Set(editorItems.map((item) => item.section))).toEqual(
      new Set([null, "work", "capabilities"]),
    );
    expect(
      editorItems
        .filter((item) => item.section === "capabilities")
        .map((item) => item.id),
    ).toEqual(["agents", "skills", "mcp"]);
    expect(new Set(adminItems.map((item) => item.section))).toEqual(
      new Set([null, "work", "capabilities", "management"]),
    );
    expect(
      adminItems
        .filter((item) => item.section === "management")
        .map((item) => item.id),
    ).toContain("connections");
    for (const items of [runnerItems, editorItems, viewerItems]) {
      expect(items.map((item) => item.id)).not.toContain("connections");
    }
  });

  test("shows the readable Agent catalog without exposing Skill or MCP management", () => {
    const runnerHome = renderToStaticMarkup(
      <ProjectHome project={runnerProject} />,
    );
    const viewerHome = renderToStaticMarkup(
      <ProjectHome project={viewerProject} />,
    );
    expect(runnerHome).toContain("共享 Agent");
    expect(viewerHome).toContain("共享 Agent");
    expect(runnerHome).toContain("查看和维护项目可执行 Agent");
    expect(runnerHome).not.toContain("发布");
    expect(runnerHome).not.toContain("共享 Skill");
    expect(runnerHome).not.toContain("共享 MCP");
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

    expect(items.find((item) => item.id === "overview")?.section).toBeNull();
    expect(items.find((item) => item.id === "memory")?.section).toBe("work");
    expect(
      items
        .filter((item) => item.section === "capabilities")
        .map((item) => item.id),
    ).toEqual(["agents", "skills", "mcp"]);
    expect(items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          href: "/projects/alpha/connections",
          id: "connections",
          section: "management",
        }),
        expect.objectContaining({
          href: "/projects/alpha/audit",
          id: "audit",
          section: "management",
        }),
        expect.objectContaining({
          href: "/projects/alpha/settings",
          id: "settings",
          section: "management",
        }),
      ]),
    );
    expect(items.map((item) => item.id)).not.toContain("members");
  });

  test("gates member work with readiness without hiding channel governance", () => {
    const readyItems = projectNavigationItems(
      adminProject,
      true,
      true,
      false,
      false,
    );

    expect(readyItems.map((item) => item.id)).toEqual(
      expect.arrayContaining(["conversations", "connections", "memory"]),
    );
    expect(readyItems).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          href: "/projects/alpha/connections",
          id: "connections",
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
    ).map((item) => item.id);
    const featureDisabledLabels = projectNavigationItems(
      adminProject,
      true,
      false,
      false,
      false,
    ).map((item) => item.id);
    for (const id of ["conversations", "memory"]) {
      expect(notReadyLabels).not.toContain(id);
      expect(featureDisabledLabels).not.toContain(id);
    }
    expect(notReadyLabels).toContain("connections");
    expect(featureDisabledLabels).not.toContain("connections");
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
      "Overview",
      "Audit log",
      "Project settings",
      "Agent",
      "Skill",
      "MCP",
      "Back to workspace",
      "Account",
    ]) {
      expect(html).toContain(label);
    }
    expect(html).not.toContain("项目成员");
    for (const unavailable of ["私有工作", "自动化"]) {
      expect(html).not.toContain(unavailable);
    }
    expect(html).toContain("Project content");
  });

  test("renders an English-only shell with a keyboard skip target", () => {
    const html = renderShell(adminProject);

    expect(html).toContain('href="#project-main"');
    expect(html).toContain('id="project-main"');
    expect(html).toContain("Skip to main content");
    expect(html).not.toMatch(/\p{Script=Han}/u);
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
    const secretManager = {
      ...sharedAssetReader,
      capabilities: [
        ...sharedAssetReader.capabilities,
        "shared_assets.manage_bindings",
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

    expect(renderShell(roleOnlyAdmin)).not.toContain("Project settings");
    expect(renderShell(capabilityViewer)).toContain("Project settings");
    expect(renderShell(roleOnlyAdmin)).not.toContain("Agent");
    expect(renderShell(roleOnlyAdmin)).not.toContain("Connections");
    expect(renderShell(channelManager)).toContain("Connections");
    expect(renderShell(capabilityViewer)).not.toContain("Agent");
    expect(renderShell(sharedAssetReader)).toContain("Agent");
    expect(renderShell(sharedAssetEditor)).toContain("Agent");
    expect(renderShell(memberManager)).toContain("Project settings");
    expect(renderShell(memberManager)).not.toContain("项目成员");
    expect(
      projectNavigationItems(memberManager, false, true, false, false).find(
        (item) => item.id === "settings",
      )?.href,
    ).toBe("/projects/alpha/settings/members");
    expect(renderShell(secretManager)).not.toContain("项目秘密");
  });
});
