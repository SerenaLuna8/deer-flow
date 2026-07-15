import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { projectNavigationItems } from "@/components/projects/project-nav";
import { ProjectShell } from "@/components/projects/project-shell";
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
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace",
};

function renderShell(project: Project) {
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <ProjectShell
        project={project}
        accountEmail="member@example.com"
        onLogout={() => undefined}
      >
        <p>Project content</p>
      </ProjectShell>
    </QueryClientProvider>,
  );
}

describe("project shell navigation", () => {
  test("gates project Memory and Connections with private-work readiness", () => {
    const readyItems = projectNavigationItems(adminProject, true, true);

    expect(readyItems.map((item) => item.label)).toEqual(
      expect.arrayContaining(["Chats", "Memory", "Connections"]),
    );
    expect(projectNavigationItems(adminProject, false, true)).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Memory" }),
        expect.objectContaining({ label: "Connections" }),
      ]),
    );
    expect(projectNavigationItems(adminProject, true, false)).not.toEqual(
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
