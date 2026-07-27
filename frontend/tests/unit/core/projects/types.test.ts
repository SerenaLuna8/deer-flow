import { describe, expect, test } from "@rstest/core";

import { normalizeProjectSlug, projectSlugError } from "@/core/projects/slug";
import {
  CAPABILITIES,
  changeProjectMemberRoleSchema,
  createdProjectInvitationSchema,
  createProjectSchema,
  createProjectInvitationSchema,
  INVITABLE_PROJECT_ROLES,
  PROJECT_ROLES,
  projectInvitationSchema,
  projectMembershipSchema,
  projectPageSchema,
  redeemedProjectInvitationSchema,
  projectSchema,
} from "@/core/projects/types";

const project = {
  id: "11111111-1111-4111-8111-111111111111",
  slug: "alpha-project",
  display_name: "Alpha",
  description: "Shared project",
  icon: "folder",
  role: "admin",
  capabilities: [...CAPABILITIES],
  is_pinned: false,
  last_entered_at: "2026-07-12T10:30:00+08:00",
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
  request_id: "trace-1",
} as const;

const invitation = {
  id: "22222222-2222-4222-8222-222222222222",
  project_id: project.id,
  invited_email: "new@example.com",
  role: "editor",
  status: "pending",
  expires_at: "2026-07-19T08:00:00+00:00",
  version: 1,
  created_at: "2026-07-12T08:00:00+00:00",
} as const;

describe("project contracts", () => {
  test("normalizes valid project slugs and rejects values outside the gateway rule", () => {
    expect(normalizeProjectSlug("  Research-Lab  ")).toBe("research-lab");
    expect(projectSlugError("")).toBe("请输入项目标识。");
    expect(projectSlugError("ab")).toBe("项目标识至少需要 3 个字符。");
    expect(projectSlugError("a".repeat(64))).toBe(
      "项目标识不能超过 63 个字符。",
    );
    expect(projectSlugError("alpha_project")).toBe(
      "项目标识只能使用小写英文字母、数字和单个连字符（-），且不能以连字符开头或结尾。",
    );
    expect(projectSlugError("alpha-project")).toBeNull();

    expect(
      createProjectSchema.parse({
        slug: "  Research-Lab  ",
        display_name: "Research Lab",
      }).slug,
    ).toBe("research-lab");
    for (const slug of [
      "ab",
      "a".repeat(64),
      "-alpha",
      "alpha-",
      "alpha--project",
      "alpha_project",
      "中文项目",
    ]) {
      expect(
        createProjectSchema.safeParse({
          slug,
          display_name: "Research Lab",
        }).success,
      ).toBe(false);
    }
  });

  test("freezes the complete role and capability enums", () => {
    expect(PROJECT_ROLES).toEqual(["admin", "editor", "runner", "viewer"]);
    expect(CAPABILITIES).toEqual([
      "project.read",
      "project.update",
      "project.enter",
      "project.pin",
      "project.members.manage",
      "shared_assets.read",
      "shared_assets.execute",
      "shared_assets.edit",
      "shared_assets.manage_bindings",
      "mcp.credentials.approve",
      "private_work.create",
      "private_work.read_own",
      "automation.manage_own",
      "project.audit.read",
      "project.usage.read",
      "project.lifecycle.manage",
    ]);
  });

  test("allows Admin for memberships but never for invitations", () => {
    expect(INVITABLE_PROJECT_ROLES).toEqual(["editor", "runner", "viewer"]);
    expect(
      createProjectInvitationSchema.safeParse({
        email: "new@example.com",
        role: "admin",
      }).success,
    ).toBe(false);
    expect(
      createProjectInvitationSchema.safeParse({
        email: "new@example.com",
        role: "editor",
      }).success,
    ).toBe(true);

    expect(
      projectMembershipSchema.safeParse({
        membership_id: "33333333-3333-4333-8333-333333333333",
        user_id: "44444444-4444-4444-8444-444444444444",
        account_email: "admin@example.com",
        role: "admin",
        status: "active",
        version: 1,
        joined_at: "2026-07-12T08:00:00+00:00",
      }).success,
    ).toBe(true);
    expect(
      changeProjectMemberRoleSchema.safeParse({ role: "admin", version: 1 })
        .success,
    ).toBe(true);
  });

  test("keeps membership user and resource IDs as UUIDs", () => {
    const membership = {
      membership_id: "33333333-3333-4333-8333-333333333333",
      user_id: "44444444-4444-4444-8444-444444444444",
      account_email: "member@example.com",
      role: "viewer",
      status: "active",
      version: 1,
      joined_at: "2026-07-12T08:00:00+00:00",
    } as const;

    expect(projectMembershipSchema.safeParse(membership).success).toBe(true);
    expect(
      projectMembershipSchema.safeParse({ ...membership, user_id: "default" })
        .success,
    ).toBe(false);
    expect(
      projectMembershipSchema.safeParse({
        ...membership,
        membership_id: "default",
      }).success,
    ).toBe(false);
  });

  test("fails closed when invitation responses contain the Admin role", () => {
    const contracts = [
      [projectInvitationSchema, invitation],
      [
        createdProjectInvitationSchema,
        { ...invitation, invite_url_fragment: "/invite#token=secret" },
      ],
      [
        redeemedProjectInvitationSchema,
        {
          invitation_id: invitation.id,
          project_id: project.id,
          project_slug: project.slug,
          membership_id: "33333333-3333-4333-8333-333333333333",
          role: "editor",
        },
      ],
    ] as const;

    for (const [schema, response] of contracts) {
      expect(schema.safeParse(response).success).toBe(true);
      expect(schema.safeParse({ ...response, role: "admin" }).success).toBe(
        false,
      );
    }
  });

  test("accepts the exact public response and rejects private or drifted fields", () => {
    expect(projectSchema.parse(project)).toEqual(project);
    expect(
      projectPageSchema.parse({ items: [project], next_cursor: null }),
    ).toEqual({
      items: [project],
      next_cursor: null,
    });
    expect(
      projectSchema.safeParse({ ...project, created_by_user_id: "private" })
        .success,
    ).toBe(false);
    expect(
      projectSchema.safeParse({
        ...project,
        capabilities: ["project.read", "unknown"],
      }).success,
    ).toBe(false);
    expect(
      projectSchema.safeParse({ ...project, id: "not-a-uuid" }).success,
    ).toBe(false);
    expect(
      projectSchema.safeParse({
        ...project,
        last_entered_at: "2026-07-12T10:30:00",
      }).success,
    ).toBe(false);
    expect(
      projectSchema.safeParse({ ...project, member_count: -1 }).success,
    ).toBe(false);
    const withoutQuotaSummary: Record<string, unknown> = { ...project };
    delete withoutQuotaSummary.quota_summary;
    expect(projectSchema.safeParse(withoutQuotaSummary).success).toBe(false);
    expect(
      projectSchema.safeParse({
        ...project,
        quota_summary: {
          ...project.quota_summary,
          storage_bytes: {
            ...project.quota_summary.storage_bytes,
            reserved: -1,
          },
        },
      }).success,
    ).toBe(false);
    expect(
      projectSchema.safeParse({ ...project, status: "inactive" }).success,
    ).toBe(false);
    expect(
      projectSchema.safeParse({ ...project, membership_version: 0 }).success,
    ).toBe(false);
    expect(
      projectSchema.safeParse({ ...project, request_id: "" }).success,
    ).toBe(false);
  });
});
