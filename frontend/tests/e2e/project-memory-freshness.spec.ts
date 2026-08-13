import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";

import type { Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const JOB_ID = "20000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-13T00:00:00Z";
const ORIGINAL_CONTENT = "Original server Memory";
const UPDATED_CONTENT = "Fresh server Memory from another client";

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Memory freshness route",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "private_work.read_own",
    "private_work.create",
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

type SharedMemoryState = {
  content: string;
  version: number;
  pendingCount: number;
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockMemoryClient(page: Page, state: SharedMemoryState) {
  let documentReads = 0;
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
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

    const memoryBase = `/api/projects/${PROJECT_ID}/memory`;
    if (path === memoryBase && request.method() === "GET") {
      documentReads += 1;
      return json(route, {
        content: state.content,
        version: state.version,
        updatedAt: TIMESTAMP,
        pendingCount: state.pendingCount,
        dreamRunning: false,
        injectionStatus: "ok",
      });
    }
    if (path === `${memoryBase}/pending` && request.method() === "GET") {
      return json(route, { items: [] });
    }
    if (path === `${memoryBase}/episodes` && request.method() === "GET") {
      return json(route, { items: [], nextCursor: null });
    }
    if (path === `${memoryBase}/versions` && request.method() === "GET") {
      return json(route, { items: [] });
    }
    if (path === `${memoryBase}/dream` && request.method() === "POST") {
      state.content = UPDATED_CONTENT;
      state.version += 1;
      state.pendingCount = 0;
      return json(
        route,
        { disposition: "queued", jobId: JOB_ID, historyCount: 1 },
        202,
      );
    }
    return json(route, { detail: "not found" }, 404);
  });
  return { documentReads: () => documentReads };
}

async function openTwoContextPages(
  context: BrowserContext,
  state: SharedMemoryState,
) {
  const first = await context.newPage();
  const second = await context.newPage();
  const firstRequests = await mockMemoryClient(first, state);
  const secondRequests = await mockMemoryClient(second, state);
  await Promise.all([
    first.goto("/projects/alpha/memory"),
    second.goto("/projects/alpha/memory"),
  ]);
  await Promise.all([
    expect(first.getByText(ORIGINAL_CONTENT)).toBeVisible(),
    expect(second.getByText(ORIGINAL_CONTENT)).toBeVisible(),
  ]);
  return { first, second, firstRequests, secondRequests };
}

test("same-origin tabs refresh from a scoped content-free cache hint", async ({
  context,
}) => {
  const state = { content: ORIGINAL_CONTENT, version: 1, pendingCount: 1 };
  const { first, second, secondRequests } = await openTwoContextPages(
    context,
    state,
  );
  const readsBeforeMutation = secondRequests.documentReads();

  await first.getByRole("button", { name: "Organize now" }).click();

  await expect(second.getByText(UPDATED_CONTENT)).toBeVisible({
    timeout: 5_000,
  });
  expect(secondRequests.documentReads()).toBeGreaterThan(readsBeforeMutation);
});

test("a separate browser context refreshes from the authoritative server fallback", async ({
  browser,
  baseURL,
}) => {
  test.setTimeout(35_000);
  const state = { content: ORIGINAL_CONTENT, version: 1, pendingCount: 1 };
  const firstContext = await browser.newContext({ baseURL });
  const secondContext = await browser.newContext({ baseURL });
  const first = await firstContext.newPage();
  const second = await secondContext.newPage();
  await mockMemoryClient(first, state);
  const secondRequests = await mockMemoryClient(second, state);
  try {
    await Promise.all([
      first.goto("/projects/alpha/memory"),
      second.goto("/projects/alpha/memory"),
    ]);
    await Promise.all([
      expect(first.getByText(ORIGINAL_CONTENT)).toBeVisible(),
      expect(second.getByText(ORIGINAL_CONTENT)).toBeVisible(),
    ]);
    const readsBeforeMutation = secondRequests.documentReads();

    await first.getByRole("button", { name: "Organize now" }).click();
    await second.bringToFront();

    await expect(second.getByText(UPDATED_CONTENT)).toBeVisible({
      timeout: 20_000,
    });
    expect(secondRequests.documentReads()).toBeGreaterThan(readsBeforeMutation);
  } finally {
    await Promise.all([firstContext.close(), secondContext.close()]);
  }
});
