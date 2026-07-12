import { describe, expect, test } from "@rstest/core";

import {
  CAPABILITIES,
  createProjectInvitationSchema,
  INVITABLE_PROJECT_ROLES,
  PROJECT_ROLES,
  projectPageSchema,
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
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "trace-1",
} as const;

describe("project contracts", () => {
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
