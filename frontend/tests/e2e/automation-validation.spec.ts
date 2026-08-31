import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";
import type { ProjectAssetItem } from "@/core/shared-assets";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const AGENT_ID = "20000000-0000-4000-8000-000000000001";
const AGENT_DEFINITION_ID = "30000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-17T03:26:00Z";

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Automation validation browser coverage",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "private_work.read_own",
    "private_work.create",
    "shared_assets.read",
    "shared_assets.execute",
    "automation.manage_own",
  ],
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

const systemAgent: ProjectAssetItem = {
  id: AGENT_ID,
  scope: "system",
  project_id: null,
  slug: "project-assistant",
  display_name: "Main",
  description: "Main project Agent",
  status: "active",
  definition_id: AGENT_DEFINITION_ID,
  revision: 1,
  capabilities: ["shared_assets.execute"],
  binding: null,
  created_by_user_id: "system",
  created_at: TIMESTAMP,
  updated_at: TIMESTAMP,
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockProjectAutomations(page: Page) {
  const unexpectedRequests: string[] = [];

  await page.route("**/api/**", (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/v1/auth/me" && method === "GET") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "owner@example.test",
        username: "owner",
        system_role: "user",
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
      return json(route, { items: [project], next_cursor: null });
    }
    if (path === `/api/projects/${PROJECT_ID}/enter` && method === "POST") {
      return json(route, project);
    }
    if (
      path === `/api/projects/${PROJECT_ID}/automations/readiness` &&
      method === "GET"
    ) {
      return json(route, {
        status: "ready",
        code: "READY",
        scheduler_enabled: true,
        scheduler_status: "running",
        project_private_work_ready: true,
        schema_ready: true,
        request_id: "request-automation-readiness",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/automations` &&
      method === "GET"
    ) {
      return json(route, { items: [] });
    }
    if (path === `/api/projects/${PROJECT_ID}/agents` && method === "GET") {
      return json(route, {
        system_items: [systemAgent],
        project_items: [],
        request_id: "request-agents",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/agents/runtime-assessments` &&
      method === "POST"
    ) {
      return json(route, {
        items: [
          {
            agent_asset_id: AGENT_ID,
            selected_definition_id: AGENT_DEFINITION_ID,
            status: "ready",
            reason_code: null,
          },
        ],
        request_id: "request-agent-runtime",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/private-work/threads/search` &&
      method === "POST"
    ) {
      return json(route, { items: [] });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/private-work/readiness` &&
      method === "GET"
    ) {
      return json(route, {
        status: "ready",
        code: "READY",
        request_id: "request-private-work-readiness",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/knowledge/health` &&
      method === "GET"
    ) {
      return json(route, {
        enabled: false,
        database_ok: false,
        storage_ok: false,
        message: "Knowledge module disabled for this deployment.",
        request_id: "request-knowledge-health",
      });
    }

    unexpectedRequests.push(`${method} ${path}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return { unexpectedRequests };
}

test("automation validation follows the current template-filled draft", async ({
  page,
}) => {
  const { unexpectedRequests } = await mockProjectAutomations(page);

  await page.goto("/projects/alpha/automations");
  await page.getByTestId("automation-empty").getByRole("button").click();

  const form = page.getByTestId("automation-form");
  const title = form.locator('input[aria-required="true"]').first();
  const prompt = form.locator('textarea[aria-required="true"]');
  const submit = form.locator('button[type="submit"]');

  await submit.click();
  await expect(form.getByRole("alert")).toHaveText("请输入 title。");

  await title.fill("Manual title");
  await expect(form.getByRole("alert")).toHaveText("请输入 prompt。");

  await form.getByRole("button", { name: "GitHub Trending" }).click();
  await expect(title).toHaveValue("GitHub Trending");
  await expect(prompt).not.toHaveValue("");
  await expect(form.getByRole("alert")).toHaveCount(0);

  expect(unexpectedRequests).toEqual([]);
});
