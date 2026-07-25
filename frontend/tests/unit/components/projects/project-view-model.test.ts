import { describe, expect, test } from "@rstest/core";

import {
  canUpdateProject,
  filterAndSortProjects,
  formatProjectQuota,
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
  quota_summary: {
    members: { used: 1, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
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
    expect(
      filterAndSortProjects(projects, "", "pinned").map((item) => item.slug),
    ).toEqual(["research-lab"]);
  });

  test("formats the complete project quota summary for workspace cards", () => {
    expect(
      formatProjectQuota({
        members: { used: 3, reserved: 1, limit: 20 },
        storage_bytes: {
          used: 1_610_612_736,
          reserved: 0,
          limit: 5_368_709_120,
        },
        concurrent_runs: { used: 1, reserved: 1, limit: 3 },
        mcp_calls_daily: { used: 25, reserved: 5, limit: 10_000 },
      }),
    ).toEqual({
      members: "成员 4 / 20",
      storage: "存储 1.5 GiB / 5 GiB",
      runs: "运行 2 / 3",
      mcp: "MCP 30 / 10,000",
    });
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

  test("maps recoverable governance conflicts without backend details", () => {
    expect(
      projectErrorMessage(
        new ProjectApiError(409, "PROJECT_LAST_ADMIN", "unsafe raw"),
      ),
    ).toContain("最后一名 Admin");
    expect(
      projectErrorMessage(
        new ProjectApiError(
          429,
          "PROJECT_MEMBER_QUOTA_EXCEEDED",
          "unsafe capacity detail",
        ),
      ),
    ).toBe(
      "项目成员容量已满，请联系项目管理员调整成员上限后，重新打开邀请链接。",
    );
    expect(
      projectErrorMessage(
        new ProjectApiError(
          409,
          "PROJECT_MEMBERSHIP_VERSION_CONFLICT",
          "unsafe raw",
        ),
      ),
    ).toContain("成员信息已更新");
    expect(
      projectErrorMessage(
        new ProjectApiError(
          409,
          "PROJECT_QUOTA_STATE_CONFLICT",
          "unsafe quota ledger detail",
        ),
      ),
    ).toContain("成员配额状态不一致");
    expect(
      projectErrorMessage(
        new ProjectApiError(409, "PROJECT_INVITATION_INVALID", "unsafe raw"),
      ),
    ).toContain("邀请已失效");
    expect(
      projectErrorMessage(
        new ProjectApiError(
          409,
          "PROJECT_DELETION_STATE_CONFLICT",
          "unsafe raw",
        ),
      ),
    ).toContain("项目状态已变化");
  });

  test("keeps revoke invitation failures in stable Chinese", () => {
    expect(
      projectErrorMessage(
        new ProjectApiError(
          409,
          "PROJECT_INVITATION_CONFLICT",
          "unsafe revoke backend detail",
        ),
      ),
    ).toBe("该邀请已存在或刚刚被处理，请刷新后重试。");
  });
});
