import { expect, test, type Page, type Route } from "@playwright/test";

import type {
  Capability,
  Project,
  ProjectInvitation,
  ProjectMembership,
} from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const SELF_MEMBERSHIP_ID = "20000000-0000-4000-8000-000000000001";
const OTHER_MEMBERSHIP_ID = "20000000-0000-4000-8000-000000000002";
const OTHER_USER_ID = "30000000-0000-4000-8000-000000000002";
const INVITATION_ID = "40000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-22T00:00:00Z";

const capabilities: Capability[] = [
  "project.read",
  "project.update",
  "project.enter",
  "project.members.manage",
  "project.lifecycle.manage",
];

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Destructive confirmation coverage",
  icon: "folder",
  role: "admin",
  capabilities,
  is_pinned: false,
  created_at: "2026-07-01T00:00:00Z",
  last_entered_at: null,
  member_count: 2,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 2, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

const selfMembership: ProjectMembership = {
  membership_id: SELF_MEMBERSHIP_ID,
  user_id: ACCOUNT_ID,
  account_email: "default@test.local",
  role: "admin",
  status: "active",
  version: 1,
  joined_at: TIMESTAMP,
};

const otherMembership: ProjectMembership = {
  membership_id: OTHER_MEMBERSHIP_ID,
  user_id: OTHER_USER_ID,
  account_email: "member@example.test",
  role: "viewer",
  status: "active",
  version: 3,
  joined_at: TIMESTAMP,
};

const invitation: ProjectInvitation = {
  id: INVITATION_ID,
  project_id: PROJECT_ID,
  invited_email: "invited@example.test",
  role: "viewer",
  status: "pending",
  expires_at: "2026-09-22T00:00:00Z",
  version: 2,
  created_at: TIMESTAMP,
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockProjectGovernance(page: Page, baseURL: string) {
  const mutations: string[] = [];

  await page.context().addCookies([
    {
      name: "locale",
      value: "zh-CN",
      url: baseURL,
    },
  ]);

  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path === "/api/v1/auth/me") {
      return json(route, {
        id: ACCOUNT_ID,
        email: selfMembership.account_email,
        username: "owner",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status") {
      return json(route, {
        needs_setup: false,
        registration_enabled: true,
      });
    }
    if (path === "/api/projects" && method === "GET") {
      return json(route, { items: [project], next_cursor: null });
    }
    if (path === `/api/projects/${PROJECT_ID}/enter` && method === "POST") {
      return json(route, project);
    }
    if (path === `/api/projects/${PROJECT_ID}/members` && method === "GET") {
      return json(route, [selfMembership, otherMembership]);
    }
    if (
      path === `/api/projects/${PROJECT_ID}/invitations` &&
      method === "GET"
    ) {
      return json(route, [invitation]);
    }

    if (path === `/api/projects/${PROJECT_ID}/deletion` && method === "POST") {
      mutations.push("delete-project");
      return json(route, {
        ...project,
        status: "pending_deletion",
        deletion_effective_at: "2026-09-21T00:00:00Z",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/members/${OTHER_MEMBERSHIP_ID}` &&
      method === "DELETE"
    ) {
      mutations.push("remove-member");
      return json(route, {
        ...otherMembership,
        status: "removed",
        version: otherMembership.version + 1,
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/invitations/${INVITATION_ID}` &&
      method === "DELETE"
    ) {
      mutations.push("revoke-invitation");
      return json(route, {
        ...invitation,
        status: "revoked",
        version: invitation.version + 1,
      });
    }
    if (path === `/api/projects/${PROJECT_ID}/leave` && method === "POST") {
      mutations.push("leave-project");
      return json(route, {
        ...selfMembership,
        status: "left",
        version: selfMembership.version + 1,
      });
    }

    return json(route, { detail: "not mocked" }, 404);
  });

  return mutations;
}

test("project deletion defaults to the explicit cancel action", async ({
  page,
  baseURL,
}) => {
  const mutations = await mockProjectGovernance(
    page,
    baseURL ?? "http://localhost:3000",
  );
  await page.goto("/projects/alpha/settings");

  await page.getByRole("button", { name: "请求删除项目" }).click();
  const dialog = page.getByRole("dialog", { name: "确认删除项目" });
  await expect(dialog).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "取消", exact: true }),
  ).toBeFocused();
  await expect(
    dialog.getByRole("button", { name: "确认请求删除" }),
  ).toHaveAttribute("data-variant", "destructive");
  expect(mutations).toEqual([]);

  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(mutations).toEqual([]);

  await page.getByRole("button", { name: "请求删除项目" }).click();
  await dialog.getByRole("button", { name: "确认请求删除" }).click();
  await expect.poll(() => mutations).toEqual(["delete-project"]);
});

test("member removal, invitation revocation, and leaving wait for confirmation", async ({
  page,
  baseURL,
}) => {
  const mutations = await mockProjectGovernance(
    page,
    baseURL ?? "http://localhost:3000",
  );
  await page.goto("/projects/alpha/settings/members");

  await page.getByRole("button", { name: "移除成员" }).click();
  const removeDialog = page.getByRole("dialog", { name: "确认移除成员" });
  await expect(removeDialog).toBeVisible();
  expect(mutations).toEqual([]);
  await removeDialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(removeDialog).toBeHidden();
  expect(mutations).toEqual([]);

  await page.getByRole("button", { name: "移除成员" }).click();
  await removeDialog.getByRole("button", { name: "确认移除成员" }).click();
  await expect.poll(() => mutations).toEqual(["remove-member"]);

  await page.getByRole("button", { name: "撤销邀请" }).click();
  const revokeDialog = page.getByRole("dialog", { name: "确认撤销邀请" });
  await expect(revokeDialog).toBeVisible();
  expect(mutations).toEqual(["remove-member"]);
  await revokeDialog.getByRole("button", { name: "确认撤销邀请" }).click();
  await expect
    .poll(() => mutations)
    .toEqual(["remove-member", "revoke-invitation"]);

  await page.getByRole("button", { name: "退出项目" }).click();
  const leaveDialog = page.getByRole("dialog", { name: "确认退出项目" });
  await expect(leaveDialog).toBeVisible();
  expect(mutations).toEqual(["remove-member", "revoke-invitation"]);
  await leaveDialog.getByRole("button", { name: "确认退出项目" }).click();
  await expect
    .poll(() => mutations)
    .toEqual(["remove-member", "revoke-invitation", "leave-project"]);
});
