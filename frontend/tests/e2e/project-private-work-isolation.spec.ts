import { expect, test, type Page, type Route } from "@playwright/test";

import { workspaceLandingPath } from "@/core/projects/features";
import type { Project } from "@/core/projects/types";

import { mockLangGraphAPI } from "./utils/mock-api";

const ACCOUNT_A = "90000000-0000-4000-8000-000000000001";
const PROJECT_ALPHA = "10000000-0000-4000-8000-000000000001";
const PROJECT_BETA = "10000000-0000-4000-8000-000000000002";

type ScopeState = {
  accountId: string;
  projects: Project[];
  requests: string[];
  holdThreadProjectId: string | null;
  threadRequestStarted: string[];
  releaseHeldThreadResponse: () => void;
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
    quota_summary: {
      members: { used: 1, reserved: 0, limit: 20 },
      storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
      concurrent_runs: { used: 0, reserved: 0, limit: 3 },
      mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
    },
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
  await page.addInitScript(() => {
    const trackedWindow = window as typeof window & {
      __projectAbortPaths?: string[];
    };
    trackedWindow.__projectAbortPaths = [];
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const path = new URL(
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url,
        window.location.origin,
      ).pathname;
      init?.signal?.addEventListener("abort", () => {
        trackedWindow.__projectAbortPaths?.push(path);
      });
      return nativeFetch(input, init);
    };
  });
  mockLangGraphAPI(page);
  let releaseHeldThreadResponse: () => void = () => undefined;
  let heldThreadResponse = Promise.resolve();
  const state: ScopeState = {
    accountId: ACCOUNT_A,
    projects: [
      project(PROJECT_ALPHA, "alpha", "Alpha Project"),
      project(PROJECT_BETA, "beta", "Beta Project"),
    ],
    requests: [],
    holdThreadProjectId: null,
    threadRequestStarted: [],
    releaseHeldThreadResponse: () => releaseHeldThreadResponse(),
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
      state.threadRequestStarted.push(current.id);
      if (current.id === state.holdThreadProjectId) {
        heldThreadResponse = new Promise<void>((resolve) => {
          releaseHeldThreadResponse = resolve;
        });
        await heldThreadResponse;
      }
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

test("Link project switch drops a late previous-project Thread response", async ({
  page,
}) => {
  const state = await installScopeFixture(page);
  state.holdThreadProjectId = PROJECT_ALPHA;

  await page.goto("/projects/alpha/chats");
  await expect.poll(() => state.threadRequestStarted).toContain(PROJECT_ALPHA);
  await page.getByRole("link", { name: "返回工作空间" }).click();
  const betaCard = page
    .getByTestId("project-card")
    .filter({ hasText: "Beta Project" });
  await betaCard.getByRole("link", { name: "进入项目" }).click();
  await page.getByRole("link", { name: "会话", exact: true }).click();
  await expect(page.getByText("Beta Project conversation")).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const trackedWindow = window as typeof window & {
          __projectAbortPaths?: string[];
        };
        return trackedWindow.__projectAbortPaths ?? [];
      }),
    )
    .toContain(`/api/projects/${PROJECT_ALPHA}/private-work/threads/search`);
  state.releaseHeldThreadResponse();
  await expect(page.getByText("Alpha Project conversation")).toHaveCount(0);
});

test("static demo landing path ignores stale thread hints", () => {
  expect(workspaceLandingPath(true, null)).toBe("/workspace");
  expect(workspaceLandingPath(true, "demo-thread")).toBe("/workspace");
});
