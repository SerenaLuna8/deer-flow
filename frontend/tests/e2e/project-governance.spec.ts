import { expect, test, type Page, type Route } from "@playwright/test";

import type {
  Project,
  ProjectInvitation,
  ProjectMembership,
} from "@/core/projects/types";
import type { SystemNotification } from "@/core/system-notifications/types";

import { mockLangGraphAPI } from "./utils/mock-api";

const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const RECOVERABLE_ID = "10000000-0000-4000-8000-000000000002";
const MEMBER_ID = "20000000-0000-4000-8000-000000000001";
const INVITATION_ID = "30000000-0000-4000-8000-000000000001";
const NOTIFICATION_ID = "50000000-0000-4000-8000-000000000001";
const SECOND_NOTIFICATION_ID = "50000000-0000-4000-8000-000000000002";
const SECOND_PROJECT_ID = "10000000-0000-4000-8000-000000000003";

const project: Project = {
  id: PROJECT_ID,
  slug: "research-lab",
  display_name: "Research Lab",
  description: "Shared research workspace",
  icon: "folder",
  role: "viewer",
  capabilities: [
    "project.read",
    "project.enter",
    "project.members.manage",
    "project.lifecycle.manage",
  ],
  is_pinned: false,
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
  membership_version: 4,
  request_id: "request-project",
};

const recoverableProject: Project = {
  ...project,
  id: RECOVERABLE_ID,
  slug: "archive-lab",
  display_name: "待删除项目",
  status: "pending_deletion",
  deletion_effective_at: "2026-08-11T08:00:00+00:00",
};

const members: ProjectMembership[] = [
  {
    membership_id: MEMBER_ID,
    user_id: "40000000-0000-4000-8000-000000000001",
    account_email: "default@test.local",
    role: "viewer",
    status: "active",
    version: 4,
    joined_at: "2026-07-01T08:00:00+00:00",
  },
  {
    membership_id: "20000000-0000-4000-8000-000000000002",
    user_id: "40000000-0000-4000-8000-000000000002",
    account_email: "editor@example.com",
    role: "editor",
    status: "active",
    version: 2,
    joined_at: "2026-07-02T08:00:00+00:00",
  },
];

const invitation: ProjectInvitation = {
  id: INVITATION_ID,
  project_id: PROJECT_ID,
  invited_email: "invitee@example.com",
  role: "viewer",
  status: "pending",
  expires_at: "2026-07-19T08:00:00+00:00",
  version: 1,
  created_at: "2026-07-12T08:00:00+00:00",
};

const projectInvitationNotification: SystemNotification = {
  id: NOTIFICATION_ID,
  kind: "project_invitation",
  project: {
    id: PROJECT_ID,
    slug: "research-lab",
    display_name: "Research Lab",
  },
  actor: { email: "owner@example.com" },
  role: "viewer",
  status: "pending",
  is_read: false,
  created_at: "2026-07-12T08:00:00+00:00",
  expires_at: "2026-07-19T08:00:00+00:00",
  version: 1,
};

const secondProjectInvitationNotification: SystemNotification = {
  ...projectInvitationNotification,
  id: SECOND_NOTIFICATION_ID,
  project: {
    id: SECOND_PROJECT_ID,
    slug: "operations-lab",
    display_name: "Operations Lab",
  },
  actor: { email: "second-owner@example.com" },
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type GovernanceMock = {
  claimBodies: unknown[];
  redeemBodies: Array<string | null>;
  notificationAcceptBodies: unknown[];
  notificationAcceptIds: string[];
  notificationListQueries: string[];
  completeNotificationAcceptance: () => void;
  readAllCalls: () => number;
  failNextRedemption: () => void;
  exhaustMemberCapacityOnNextRedemption: () => void;
  failNextRevocation: () => void;
};

async function mockProjectGovernance(
  page: Page,
  currentProject: Project = project,
  initialProjectVisible = true,
): Promise<GovernanceMock> {
  let currentMembers = structuredClone(members);
  let projectInvitations = [structuredClone(invitation)];
  let notifications = [
    structuredClone(projectInvitationNotification),
    structuredClone(secondProjectInvitationNotification),
  ];
  let projectState = structuredClone(currentProject);
  let projectVisible = initialProjectVisible;
  let recoverableVisible = true;
  const claimBodies: unknown[] = [];
  const redeemBodies: Array<string | null> = [];
  const notificationAcceptBodies: unknown[] = [];
  const notificationAcceptIds: string[] = [];
  const notificationListQueries: string[] = [];
  let notificationReadAllCalls = 0;
  let releaseNotificationAcceptance: (() => void) | null = null;
  const notificationAcceptanceGate = new Promise<void>((resolve) => {
    releaseNotificationAcceptance = resolve;
  });
  let redemptionFailure = false;
  let memberCapacityExhausted = false;
  let revocationFailure = false;

  await page.route(
    /\/api\/(?:projects|project-invitations|notifications)(?:\/.*)?(?:\?.*)?$/,
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const method = request.method();

      if (path.endsWith("/api/notifications") && method === "GET") {
        notificationListQueries.push(url.search);
        const cursor = url.searchParams.get("cursor");
        const items =
          cursor === "page-2"
            ? notifications.slice(1)
            : notifications.slice(0, 1);
        await json(route, {
          items,
          next_cursor: cursor === "page-2" ? null : "page-2",
          unread_count: notifications.filter((item) => !item.is_read).length,
        });
        return;
      }
      if (path.endsWith("/api/notifications/read-all") && method === "POST") {
        const markedCount = notifications.filter(
          (item) => !item.is_read,
        ).length;
        notificationReadAllCalls += 1;
        notifications = notifications.map((item) => ({
          ...item,
          is_read: true,
        }));
        await json(route, { marked_count: markedCount });
        return;
      }
      const notificationAcceptMatch =
        /\/api\/notifications\/([^/]+)\/accept$/.exec(path);
      if (notificationAcceptMatch && method === "POST") {
        notificationAcceptIds.push(notificationAcceptMatch[1]!);
        notificationAcceptBodies.push(request.postDataJSON());
        await notificationAcceptanceGate;
        projectVisible = true;
        notifications = notifications.map((item) =>
          item.id === notificationAcceptMatch[1]
            ? {
                ...item,
                is_read: true,
                status: "redeemed",
                version: item.version + 1,
              }
            : item,
        );
        await json(route, {
          invitation_id: INVITATION_ID,
          project_id: PROJECT_ID,
          project_slug: "research-lab",
          membership_id: MEMBER_ID,
          role: "viewer",
        });
        return;
      }
      if (
        path.endsWith("/api/project-invitations/claim") &&
        method === "POST"
      ) {
        claimBodies.push(request.postDataJSON());
        await json(route, { message: "Invitation claim processed" });
        return;
      }
      if (
        path.endsWith("/api/project-invitations/redeem") &&
        method === "POST"
      ) {
        redeemBodies.push(request.postData());
        if (memberCapacityExhausted) {
          memberCapacityExhausted = false;
          await json(
            route,
            {
              detail: {
                code: "PROJECT_MEMBER_QUOTA_EXCEEDED",
                message: "unsafe internal capacity detail",
                request_id: "request-member-capacity",
              },
            },
            429,
          );
          return;
        }
        if (redemptionFailure) {
          redemptionFailure = false;
          await json(
            route,
            {
              detail: {
                code: "PROJECT_INVITATION_CONFLICT",
                message: "Invitation is no longer available",
                request_id: "request-invitation-conflict",
              },
            },
            409,
          );
          return;
        }
        await json(route, {
          invitation_id: INVITATION_ID,
          project_id: PROJECT_ID,
          project_slug: "research-lab",
          membership_id: MEMBER_ID,
          role: "viewer",
        });
        return;
      }
      if (path.endsWith("/api/project-invitations/mine") && method === "GET") {
        await json(route, [invitation]);
        return;
      }
      if (path.endsWith("/api/projects") && method === "GET") {
        const activeItems = projectVisible ? [currentProject] : [];
        const items = url.searchParams.has("include_recoverable")
          ? [
              ...activeItems,
              ...(recoverableVisible ? [recoverableProject] : []),
            ]
          : activeItems;
        await json(route, { items, next_cursor: null });
        return;
      }
      if (path.endsWith(`/api/projects/${PROJECT_ID}/enter`)) {
        await json(route, projectState);
        return;
      }
      if (path.endsWith(`/api/projects/${PROJECT_ID}/members`)) {
        if (method === "GET") {
          await json(route, currentMembers);
          return;
        }
      }
      const memberMatch = new RegExp(
        `/api/projects/${PROJECT_ID}/members/([^/]+)$`,
      ).exec(path);
      if (memberMatch && method === "PATCH") {
        const input = request.postDataJSON() as {
          role: ProjectMembership["role"];
        };
        currentMembers = currentMembers.map((member) =>
          member.membership_id === memberMatch[1]
            ? { ...member, role: input.role, version: member.version + 1 }
            : member,
        );
        await json(
          route,
          currentMembers.find(
            (member) => member.membership_id === memberMatch[1],
          ),
        );
        return;
      }
      if (memberMatch && method === "DELETE") {
        const removed = currentMembers.find(
          (member) => member.membership_id === memberMatch[1],
        )!;
        currentMembers = currentMembers.filter(
          (member) => member.membership_id !== memberMatch[1],
        );
        await json(route, { ...removed, status: "removed" });
        return;
      }
      if (
        path.endsWith(`/api/projects/${PROJECT_ID}/leave`) &&
        method === "POST"
      ) {
        projectVisible = false;
        await json(route, { ...currentMembers[0], status: "left", version: 5 });
        return;
      }
      if (path.endsWith(`/api/projects/${PROJECT_ID}/invitations`)) {
        if (method === "GET") {
          await json(route, projectInvitations);
          return;
        }
        if (method === "POST") {
          const input = request.postDataJSON() as {
            email: string;
            role: ProjectInvitation["role"];
          };
          const created: ProjectInvitation & { invite_url_fragment: string } = {
            ...invitation,
            id: "30000000-0000-4000-8000-000000000002",
            invited_email: input.email,
            role: input.role,
            invite_url_fragment: "/invite#token=new-secret-token",
          };
          const ordinaryInvitation: ProjectInvitation = {
            id: created.id,
            project_id: created.project_id,
            invited_email: created.invited_email,
            role: created.role,
            status: created.status,
            expires_at: created.expires_at,
            version: created.version,
            created_at: created.created_at,
          };
          projectInvitations = [...projectInvitations, ordinaryInvitation];
          await json(route, created, 201);
          return;
        }
      }
      const invitationMatch = new RegExp(
        `/api/projects/${PROJECT_ID}/invitations/([^/]+)$`,
      ).exec(path);
      if (invitationMatch && method === "DELETE") {
        if (revocationFailure) {
          revocationFailure = false;
          await json(
            route,
            {
              detail: {
                code: "PROJECT_INVITATION_CONFLICT",
                message: "unsafe backend detail",
              },
            },
            409,
          );
          return;
        }
        const revoked = projectInvitations.find(
          (item) => item.id === invitationMatch[1],
        )!;
        projectInvitations = projectInvitations.filter(
          (item) => item.id !== invitationMatch[1],
        );
        await json(route, { ...revoked, status: "revoked" });
        return;
      }
      if (
        path.endsWith(`/api/projects/${PROJECT_ID}/deletion`) &&
        method === "POST"
      ) {
        projectVisible = false;
        projectState = {
          ...projectState,
          status: "pending_deletion",
          deletion_effective_at: "2026-08-11T08:00:00+00:00",
        };
        await json(route, projectState);
        return;
      }
      if (
        path.endsWith(`/api/projects/${RECOVERABLE_ID}/restore`) &&
        method === "POST"
      ) {
        recoverableVisible = false;
        await json(route, {
          ...recoverableProject,
          status: "active",
          deletion_effective_at: null,
        });
        return;
      }
      await json(
        route,
        {
          detail: {
            code: "PROJECT_OR_MEMBER_NOT_FOUND",
            message: "not found",
            request_id: "request-404",
          },
        },
        404,
      );
    },
  );

  return {
    claimBodies,
    redeemBodies,
    notificationAcceptBodies,
    notificationAcceptIds,
    notificationListQueries,
    completeNotificationAcceptance: () => releaseNotificationAcceptance?.(),
    readAllCalls: () => notificationReadAllCalls,
    failNextRedemption: () => {
      redemptionFailure = true;
    },
    exhaustMemberCapacityOnNextRedemption: () => {
      memberCapacityExhausted = true;
    },
    failNextRevocation: () => {
      revocationFailure = true;
    },
  };
}

test("claims a fragment secret once, clears the URL, and redeems without a body", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectGovernance(page);

  await page.goto("/invite#token=plain-secret-token");

  await expect.poll(() => page.url()).not.toContain("plain-secret-token");
  expect(
    await page.evaluate(() => localStorage.getItem("invitation-token")),
  ).toBeNull();
  expect(
    await page.evaluate(() => sessionStorage.getItem("invitation-token")),
  ).toBeNull();
  await expect(page.getByRole("heading", { name: "邀请已接受" })).toBeVisible();
  await expect(page.getByRole("link", { name: "进入项目" })).toHaveAttribute(
    "href",
    "/projects/research-lab",
  );
  expect(api.claimBodies).toEqual([{ token: "plain-secret-token" }]);
  expect(api.redeemBodies).toEqual([null]);
});

test("reprocesses a replayed fragment and never reuses the previous success state", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectGovernance(page);

  await page.goto("/invite#token=plain-secret-token");
  await expect(page.getByRole("heading", { name: "邀请已接受" })).toBeVisible();

  api.failNextRedemption();
  await page.evaluate(() => {
    window.location.hash = "token=plain-secret-token";
  });

  await expect.poll(() => page.url()).not.toContain("plain-secret-token");
  await expect(
    page.getByRole("heading", { name: "无法接受邀请" }),
  ).toBeVisible();
  expect(api.claimBodies).toEqual([
    { token: "plain-secret-token" },
    { token: "plain-secret-token" },
  ]);
  expect(api.redeemBodies).toEqual([null, null]);
});

test("explains member capacity exhaustion without exposing backend detail", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectGovernance(page);
  api.exhaustMemberCapacityOnNextRedemption();

  await page.goto("/invite#token=plain-secret-token");

  await expect(
    page.getByRole("heading", { name: "无法接受邀请" }),
  ).toBeVisible();
  const redemptionAlert = page.locator("main").getByRole("alert");
  await expect(redemptionAlert).toContainText(
    "项目成员容量已满，请联系项目管理员调整成员上限后，重新打开邀请链接。",
  );
  await expect(redemptionAlert).not.toContainText(
    "unsafe internal capacity detail",
  );
});

test("authenticated SSO callback executes before returning to invite redemption", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectGovernance(page);
  let authChecks = 0;
  await page.route("**/api/v1/auth/me", async (route) => {
    authChecks += 1;
    await json(route, {
      id: "default",
      email: "default@test.local",
      system_role: "system_admin",
      needs_setup: false,
    });
  });

  await page.goto("/auth/callback?next=%2Finvite");

  await expect.poll(() => authChecks).toBe(1);
  await expect(page).toHaveURL(/\/invite$/);
  await expect(page.getByRole("heading", { name: "邀请已接受" })).toBeVisible();
  expect(api.redeemBodies).toEqual([null]);
});

test("local login and SSO callback reject escaping next paths but keep /invite", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectGovernance(page);
  await page.route("**/api/v1/auth/me", async (route) => {
    await json(route, {
      id: "default",
      email: "default@test.local",
      system_role: "system_admin",
      needs_setup: false,
    });
  });

  for (const next of ["/%5Cevil", "//evil.example", "https:"]) {
    await page.goto(`/login?next=${next}`);
    await expect(page).toHaveURL(/\/workspace$/);
    await page.goto(`/auth/callback?next=${next}`);
    await expect(page).toHaveURL(/\/workspace$/);
  }

  await page.goto("/login?next=/invite");
  await expect(page).toHaveURL(/\/invite$/);
});

test("workspace handles invitations in the notification center and keeps recoverable projects separate", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectGovernance(page, project, false);

  await page.goto("/workspace");

  await expect(page.getByText("Research Lab", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "通知，2 条未读" }).click();
  const notificationPanel = page.getByRole("dialog");
  await expect(notificationPanel).toContainText("Research Lab");
  await expect(notificationPanel).toContainText("owner@example.com");
  await expect.poll(api.readAllCalls).toBe(1);
  await notificationPanel.getByRole("button", { name: "加载更多" }).click();
  await expect(notificationPanel).toContainText("Operations Lab");
  expect(api.notificationListQueries[0]).toBe("?limit=50");
  expect(api.notificationListQueries).toContain("?cursor=page-2&limit=50");

  const acceptButtons = notificationPanel.getByRole("button", {
    name: "同意加入项目",
  });
  await acceptButtons.first().click();
  await expect(
    notificationPanel.getByRole("button", { name: "正在加入…" }),
  ).toBeDisabled();
  await expect(acceptButtons.last()).toBeDisabled();
  await acceptButtons.last().evaluate((element) => {
    (element as HTMLButtonElement).click();
  });
  await expect.poll(() => api.notificationAcceptBodies.length).toBe(1);
  expect(api.notificationAcceptBodies).toEqual([{ version: 1 }]);
  expect(api.notificationAcceptIds).toEqual([NOTIFICATION_ID]);
  expect(NOTIFICATION_ID).not.toBe(INVITATION_ID);
  api.completeNotificationAcceptance();
  const acceptedNotification = notificationPanel.getByTestId(
    `notification-${NOTIFICATION_ID}`,
  );
  await expect(acceptedNotification).toContainText("已加入");
  await expect(
    acceptedNotification.getByRole("button", { name: "同意加入项目" }),
  ).toHaveCount(0);
  await expect(
    notificationPanel.getByRole("button", { name: "同意加入项目" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Close" }).click();
  const activeGrid = page.getByTestId("project-grid");
  await expect(
    activeGrid.getByText("Research Lab", { exact: true }),
  ).toBeVisible();
  await expect(activeGrid.getByText("待删除项目", { exact: true })).toHaveCount(
    0,
  );
  const recovery = page.getByRole("region", { name: "可恢复项目" });
  await expect(recovery).toContainText("待删除项目");
  await recovery.getByRole("button", { name: "恢复项目" }).click();
  await expect(page.getByRole("region", { name: "可恢复项目" })).toHaveCount(0);
});

test("server capabilities expose governance actions even when the role label is viewer", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectGovernance(page);

  await page.goto("/projects/research-lab/members");

  await expect(page.getByRole("heading", { name: "成员与邀请" })).toBeVisible();
  await expect(page.getByRole("button", { name: "邀请成员" })).toBeVisible();
  const selfRow = page.getByRole("row", { name: /default@test\.local/ });
  const editorRow = page.getByRole("row", { name: /editor@example\.com/ });
  await expect(selfRow.getByRole("button", { name: "移除成员" })).toHaveCount(
    0,
  );
  await expect(
    editorRow.getByRole("button", { name: "移除成员" }),
  ).toBeVisible();
  await editorRow.getByRole("button", { name: "修改角色" }).click();
  await page.getByRole("radio", { name: "Admin" }).click();
  await page.getByRole("button", { name: "保存角色" }).click();
  await expect(editorRow).toContainText("admin");

  await page.getByRole("button", { name: "邀请成员" }).click();
  const invitationDialog = page.getByRole("dialog", { name: "邀请成员" });
  await invitationDialog.getByLabel("邮箱").fill("new@example.com");
  await invitationDialog.getByRole("button", { name: "创建邀请" }).click();
  await expect(invitationDialog.getByLabel("邀请链接")).toHaveValue(
    /\/invite#token=new-secret-token$/,
  );
  await invitationDialog.getByRole("button", { name: "完成" }).click();
  await expect(
    page.getByText("new@example.com", { exact: true }),
  ).toBeVisible();
});

test("revoke invitation failures show stable Chinese guidance", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectGovernance(page);
  api.failNextRevocation();

  await page.goto("/projects/research-lab/members");
  await page.getByRole("button", { name: "撤销邀请" }).click();
  await expect(
    page.getByText("该邀请已存在或刚刚被处理，请刷新后重试。", {
      exact: true,
    }),
  ).toBeVisible();
});

test("role labels never grant actions without server capabilities", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectGovernance(page, {
    ...project,
    role: "admin",
    capabilities: ["project.read", "project.enter"],
  });

  await page.goto("/projects/research-lab/members");

  await expect(page.getByRole("heading", { name: "成员与邀请" })).toBeVisible();
  await expect(page.getByRole("button", { name: "邀请成员" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "修改角色" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "移除成员" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "项目设置" })).toHaveCount(0);
});

test("leaving or requesting deletion replaces the current project with workspace", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectGovernance(page);

  await page.goto("/projects/research-lab/settings");
  await page.getByRole("button", { name: "请求删除项目" }).click();
  const dialog = page.getByRole("dialog", { name: "确认删除项目" });
  await dialog.getByRole("button", { name: "确认请求删除" }).click();
  await expect(page).toHaveURL(/\/workspace$/);
});

test("leaving the current project replaces history with workspace", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectGovernance(page);

  await page.goto("/projects/research-lab/members");
  await page.getByRole("button", { name: "退出项目" }).click();
  await expect(page).toHaveURL(/\/workspace$/);
  await page.goBack();
  await expect(page).not.toHaveURL(/\/projects\/research-lab\/members$/);
});
