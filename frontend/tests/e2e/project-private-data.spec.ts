import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

import { mockLangGraphAPI } from "./utils/mock-api";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const AGENT_ID = "30000000-0000-4000-8000-000000000001";
const AGENT_VERSION_ID = "30000000-0000-4000-8000-000000000002";
const CONNECTION_ID = "40000000-0000-4000-8000-000000000001";

const project: Project = {
  id: PROJECT_ID,
  slug: "research-lab",
  display_name: "Research Lab",
  description: "Shared research workspace",
  icon: "folder",
  role: "runner",
  capabilities: [
    "project.read",
    "project.enter",
    "shared_assets.read",
    "shared_assets.execute",
    "private_work.create",
    "private_work.read_own",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 2,
  agent_count: 1,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "req-project",
};

const memory = {
  version: "1.0",
  lastUpdated: "2026-07-15T00:00:00Z",
  user: {
    workContext: {
      summary: "Build project-scoped private data",
      updatedAt: "2026-07-15T00:00:00Z",
    },
    personalContext: { summary: "", updatedAt: "2026-07-15T00:00:00Z" },
    topOfMind: { summary: "", updatedAt: "2026-07-15T00:00:00Z" },
  },
  history: {
    recentMonths: { summary: "", updatedAt: "2026-07-15T00:00:00Z" },
    earlierContext: { summary: "", updatedAt: "2026-07-15T00:00:00Z" },
    longTermBackground: {
      summary: "",
      updatedAt: "2026-07-15T00:00:00Z",
    },
  },
  facts: [
    {
      id: "fact-1",
      content: "Ship runnable versions first.",
      category: "preference",
      confidence: 0.9,
      createdAt: "2026-07-15T00:00:00Z",
      source: "manual",
    },
  ],
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockProjectPrivateData(page: Page) {
  let exportCount = 0;
  let connectBody: unknown = null;
  let connection: Record<string, unknown> | null = null;

  await page.route("**/api/v1/auth/me", (route) =>
    json(route, {
      id: ACCOUNT_ID,
      email: "runner@example.test",
      system_role: "user",
      needs_setup: false,
    }),
  );
  await page.route(/\/api\/projects(?:\?.*)?$/, (route) =>
    json(route, { items: [project], next_cursor: null }),
  );
  await page.route(`**/api/projects/${PROJECT_ID}/enter`, (route) =>
    json(route, { ...project, request_id: "req-enter" }),
  );
  await page.route(
    `**/api/projects/${PROJECT_ID}/private-work/readiness`,
    (route) =>
      json(route, {
        status: "ready",
        code: "PRIVATE_WORK_READY",
        request_id: "req-ready",
      }),
  );
  await page.route(`**/api/projects/${PROJECT_ID}/memory**`, (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/export")) {
      exportCount += 1;
      return json(route, memory);
    }
    return json(route, { namespace: "default", version: 3, memory });
  });
  await page.route(`**/api/projects/${PROJECT_ID}/agents`, (route) =>
    json(route, {
      system_items: [],
      project_items: [
        {
          id: AGENT_ID,
          scope: "project",
          project_id: PROJECT_ID,
          slug: "project-analyst",
          display_name: "Project Analyst",
          status: "active",
          current_published_version_id: AGENT_VERSION_ID,
          version: 1,
          created_by_user_id: ACCOUNT_ID,
          created_at: "2026-07-15T00:00:00Z",
          updated_at: "2026-07-15T00:00:00Z",
          capabilities: ["shared_assets.read", "shared_assets.execute"],
          binding: null,
        },
      ],
      request_id: "req-agents",
    }),
  );
  await page.route(
    `**/api/projects/${PROJECT_ID}/connections**`,
    async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (request.method() === "GET") {
        return json(route, { connections: connection ? [connection] : [] });
      }
      if (request.method() === "POST" && path.endsWith("/slack/connect")) {
        connectBody = request.postDataJSON();
        connection = {
          id: CONNECTION_ID,
          provider: "slack",
          status: "connected",
          external_account_id: "slack-user",
          external_account_name: "Project Slack",
          workspace_id: "workspace-1",
          workspace_name: "Research Workspace",
          scopes: ["chat:write"],
          metadata: {},
        };
        return json(route, {
          provider: "slack",
          mode: "binding_code",
          url: null,
          code: "bind-code",
          instruction: "Send the binding code.",
          expires_in: 600,
        });
      }
      if (
        request.method() === "DELETE" &&
        path.endsWith(`/connections/${CONNECTION_ID}`)
      ) {
        connection = null;
        return route.fulfill({ status: 204 });
      }
      return json(route, { detail: "not found" }, 404);
    },
  );

  return {
    connectBody: () => connectBody,
    exportCount: () => exportCount,
  };
}

test.beforeEach(async ({ page }) => {
  mockLangGraphAPI(page);
});

test("project Memory loads and exports from the project route", async ({
  page,
}) => {
  const state = await mockProjectPrivateData(page);

  await page.goto("/projects/research-lab/memory");
  await expect(page.getByText("Ship runnable versions first.")).toBeVisible();
  await page.getByRole("button", { name: "Manage memory" }).click();
  await page.getByRole("menuitem", { name: "Export memory" }).click();

  await expect.poll(state.exportCount).toBe(1);
});

test("project Connections lists, connects, and disconnects imperatively", async ({
  page,
}) => {
  const state = await mockProjectPrivateData(page);

  await page.goto("/projects/research-lab/connections");
  const slack = page.getByRole("listitem").filter({ hasText: "Slack" });
  await slack.getByRole("button", { name: "连接" }).click();
  await page.getByRole("button", { name: /Project Analyst/ }).click();

  await expect(slack.getByText("Project Slack")).toBeVisible();
  expect(state.connectBody()).toEqual({
    agent_asset_id: AGENT_ID,
    agent_scope: "project",
  });

  await slack.getByRole("button", { name: "断开" }).click();
  await expect(slack.getByText("未连接")).toBeVisible();
});
