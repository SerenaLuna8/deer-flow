import { describe, expect, test } from "@rstest/core";

import {
  canUpdateProject,
  filterAndSortProjects,
  projectErrorMessage,
} from "@/components/projects/project-view-model";
import { ProjectApiError } from "@/core/projects/api";
import { CAPABILITIES, type Project } from "@/core/projects/types";

const makeProject = (overrides: Partial<Project>): Project => ({
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha",
  display_name: "Alpha",
  description: "Project description",
  icon: "folder",
  role: "viewer",
  capabilities: ["project.read"],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace",
  ...overrides,
});

describe("project workbench view model", () => {
  test("orders pinned first and filters by display name or slug", () => {
    const projects = [
      makeProject({ slug: "zeta", display_name: "Zeta" }),
      makeProject({
        id: "22222222-2222-4222-8222-222222222222",
        slug: "research-lab",
        display_name: "Research Lab",
        is_pinned: true,
      }),
    ];
    expect(
      filterAndSortProjects(projects, "").map((item) => item.slug),
    ).toEqual(["research-lab", "zeta"]);
    expect(
      filterAndSortProjects(projects, " research ").map((item) => item.slug),
    ).toEqual(["research-lab"]);
  });

  test("uses the server capability instead of role to gate updates", () => {
    expect(
      canUpdateProject(makeProject({ role: "admin", capabilities: [] })),
    ).toBe(false);
    expect(
      canUpdateProject(
        makeProject({ role: "viewer", capabilities: [...CAPABILITIES] }),
      ),
    ).toBe(true);
  });

  test("maps conflicts and membership loss to safe Chinese guidance", () => {
    expect(
      projectErrorMessage(
        new ProjectApiError(409, "PROJECT_SLUG_CONFLICT", "unsafe raw"),
      ),
    ).toContain("标识已存在");
    expect(
      projectErrorMessage(
        new ProjectApiError(404, "PROJECT_NOT_FOUND", "unsafe raw"),
      ),
    ).toContain("返回工作空间");
    expect(projectErrorMessage(new Error("postgresql://secret"))).not.toContain(
      "secret",
    );
  });
});
