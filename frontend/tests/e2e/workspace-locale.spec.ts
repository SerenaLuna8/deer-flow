import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "project.pin",
    "project.update",
  ],
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
  request_id: "request-alpha",
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockWorkspace(page: Page) {
  const unexpectedRequests: string[] = [];

  await page.route("**/api/**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/auth/me" && method === "GET") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "admin@example.test",
        username: "admin",
        system_role: "system_admin",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status" && method === "GET") {
      return json(route, {
        needs_setup: false,
        registration_enabled: true,
      });
    }
    if (path === "/api/projects" && method === "GET") {
      return json(route, {
        items: url.searchParams.has("include_recoverable") ? [] : [project],
        next_cursor: null,
      });
    }
    if (path === "/api/notifications" && method === "GET") {
      return json(route, {
        items: [],
        next_cursor: null,
        unread_count: 0,
      });
    }

    unexpectedRequests.push(`${method} ${path}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return { unexpectedRequests };
}

test("workspace follows the selected locale immediately and after refresh", async ({
  page,
  baseURL,
}) => {
  await page.context().addCookies([
    {
      name: "locale",
      value: "zh-CN",
      url: baseURL ?? "http://localhost:3000",
    },
  ]);
  const { unexpectedRequests } = await mockWorkspace(page);

  await page.goto("/workspace");

  const workbench = page.getByTestId("project-workbench");
  await expect(workbench.getByText("工作空间", { exact: true })).toBeVisible();
  await expect(workbench.getByPlaceholder("搜索名称或项目标识")).toBeVisible();
  await expect(
    workbench.getByText("暂无项目描述", { exact: true }),
  ).toBeVisible();
  await expect(workbench.getByRole("button", { name: "通知" })).toBeVisible();

  await workbench.getByRole("button", { name: "账户" }).click();
  await page.getByRole("menuitem", { name: "系统设置" }).click();

  const settings = page.getByRole("dialog");
  await expect(settings).toHaveAccessibleName("设置");
  await settings.getByRole("combobox").click();
  await page.getByRole("option", { name: "English" }).click();

  await expect(settings).toHaveAccessibleName("Settings");
  await expect(workbench.getByText("Workspace", { exact: true })).toBeVisible();
  await expect(
    workbench.getByPlaceholder("Search by name or project slug"),
  ).toBeVisible();
  await expect(
    workbench.getByText("No project description", { exact: true }),
  ).toBeVisible();
  await expect(
    workbench.locator('button[aria-label="Notifications"]'),
  ).toBeVisible();

  await page.getByRole("button", { name: "Close" }).click();
  await page.reload();

  await expect(workbench.getByText("Workspace", { exact: true })).toBeVisible();
  await expect(
    workbench.getByPlaceholder("Search by name or project slug"),
  ).toBeVisible();
  await workbench.getByRole("button", { name: "Account" }).click();
  await page.getByRole("menuitem", { name: "System settings" }).click();
  await expect(page.getByRole("dialog")).toHaveAccessibleName("Settings");
  await expect(page.getByRole("combobox")).toHaveText("English");
  expect(unexpectedRequests).toEqual([]);
});
