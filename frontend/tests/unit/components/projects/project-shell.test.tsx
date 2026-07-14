import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

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
    <ProjectShell
      project={project}
      accountEmail="member@example.com"
      onLogout={() => undefined}
    >
      <p>Project content</p>
    </ProjectShell>,
  );
}

describe("project shell navigation", () => {
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
