import { expect, test, type Page, type Route } from "@playwright/test";

import { workspaceLandingPath } from "@/core/projects/features";
import type { Project } from "@/core/projects/types";

import { mockLangGraphAPI } from "./utils/mock-api";

const ACCOUNT_A = "90000000-0000-4000-8000-000000000001";
const ACCOUNT_B = "90000000-0000-4000-8000-000000000002";
const PROJECT_ALPHA = "10000000-0000-4000-8000-000000000001";
const PROJECT_BETA = "10000000-0000-4000-8000-000000000002";
const PROJECT_GAMMA = "10000000-0000-4000-8000-000000000003";

type ScopeState = {
  accountId: string;
  projects: Project[];
  requests: string[];
};

function project(id: string, slug: string, displayName: string): Project {
  return {
    id,
    slug,
    display_name: displayName,
    description: "Private release project",
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
    member_count: 1,
    agent_count: 1,
    skill_count: 0,
    mcp_count: 0,
    status: "active",
    is_suspended: false,
    membership_version: 1,
    request_id: `request-${slug}`,
  };
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function installScopeFixture(page: Page): Promise<ScopeState> {
  mockLangGraphAPI(page);
  const state: ScopeState = {
    accountId: ACCOUNT_A,
    projects: [
      project(PROJECT_ALPHA, "alpha", "Alpha Project"),
      project(PROJECT_BETA, "beta", "Beta Project"),
    ],
    requests: [],
  };

  await page.route("**/api/v1/auth/me", (route) =>
    json(route, {
      id: state.accountId,
      email: `${state.accountId}@example.test`,
      system_role: "user",
      needs_setup: false,
    }),
  );
  await page.route(/\/api\/projects(?:\?.*)?$/, (route) =>
    json(route, { items: state.projects, next_cursor: null }),
  );
  await page.route("**/api/projects/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const current = state.projects.find((item) =>
      path.startsWith(`/api/projects/${item.id}/`),
    );
    if (!current) return json(route, { detail: "not found" }, 404);
    state.requests.push(
      `${state.accountId} ${route.request().method()} ${path}`,
    );
    if (path.endsWith("/enter")) {
      return json(route, { ...current, request_id: `enter-${current.slug}` });
    }
    if (path.endsWith("/private-work/readiness")) {
      return json(route, {
        status: "ready",
        code: "PRIVATE_WORK_READY",
        request_id: `ready-${current.slug}`,
      });
    }
    if (path.endsWith("/private-work/threads/search")) {
      return json(route, {
        items: [
          {
            thread_id: current.id.replace(/^1/, "2"),
            agent_asset_id: "30000000-0000-4000-8000-000000000001",
            agent_scope: "project",
            display_name: `${current.display_name} conversation`,
            status: "idle",
            metadata: {
              created_at: "2026-07-15T00:00:00Z",
              updated_at: "2026-07-15T01:00:00Z",
            },
            version: 1,
          },
        ],
      });
    }
    return json(route, { detail: "not found" }, 404);
  });
  return state;
}

test("project and account switches never retain the previous private list", async ({
  page,
}) => {
  const state = await installScopeFixture(page);

  await page.goto("/projects/alpha/chats");
  await expect(page.getByText("Alpha Project conversation")).toBeVisible();

  await page.goto("/projects/beta/chats");
  await expect(page.getByText("Beta Project conversation")).toBeVisible();
  await expect(page.getByText("Alpha Project conversation")).toHaveCount(0);

  state.accountId = ACCOUNT_B;
  state.projects = [project(PROJECT_GAMMA, "gamma", "Gamma Project")];
  state.requests.length = 0;
  await page.goto("/projects/gamma/chats");
  await page.reload();
  await expect(page.getByText("Gamma Project conversation")).toBeVisible();
  await expect(page.getByText("Beta Project conversation")).toHaveCount(0);
  expect(state.requests).not.toEqual([]);
  expect(state.requests.every((request) => request.startsWith(ACCOUNT_B))).toBe(
    true,
  );
  expect(
    state.requests.every((request) => request.includes(PROJECT_GAMMA)),
  ).toBe(true);
});

test("static demo landing contract has no project entry", () => {
  expect(workspaceLandingPath(true, null)).toBe("/workspace/chats/new");
  expect(workspaceLandingPath(true, "demo-thread")).toBe(
    "/workspace/chats/demo-thread",
  );
});
