import { expect, test, type Page, type Route } from "@playwright/test";

import type { Capability, Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const capabilities: Capability[] = [
  "project.read",
  "project.enter",
  "private_work.read_own",
  "private_work.create",
];
const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Core project route",
  icon: "folder",
  role: "admin",
  capabilities,
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 1,
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

async function mockProjectMemoryRoute(page: Page) {
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/v1/auth/me") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "owner@example.test",
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
    if (path === "/api/projects" && request.method() === "GET") {
      return json(route, { items: [project], next_cursor: null });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/enter` &&
      request.method() === "POST"
    ) {
      return json(route, project);
    }
    if (
      path === `/api/projects/${PROJECT_ID}/memory` &&
      request.method() === "GET"
    ) {
      const updatedAt = "2026-07-16T00:00:00Z";
      return json(route, {
        namespace: "default",
        version: 1,
        memory: {
          version: "1.0",
          lastUpdated: updatedAt,
          user: {
            workContext: { summary: "", updatedAt },
            personalContext: { summary: "", updatedAt },
            topOfMind: { summary: "", updatedAt },
          },
          history: {
            recentMonths: { summary: "", updatedAt },
            earlierContext: { summary: "", updatedAt },
            longTermBackground: { summary: "", updatedAt },
          },
          facts: [],
        },
      });
    }
    return json(route, { detail: "not found" }, 404);
  });
}

test("a private surface renders inside the selected project shell", async ({
  page,
}) => {
  await mockProjectMemoryRoute(page);

  await page.goto("/projects/alpha/memory");

  await expect(page).toHaveURL(/\/projects\/alpha\/memory$/u);
  await expect(page.getByTestId("project-shell")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Memory", exact: true }),
  ).toBeVisible();
});
