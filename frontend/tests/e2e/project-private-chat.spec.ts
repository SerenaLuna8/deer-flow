import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

import { handleRunStream, mockLangGraphAPI } from "./utils/mock-api";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const THREAD_ID = "20000000-0000-4000-8000-000000000001";
const AGENT_ID = "30000000-0000-4000-8000-000000000001";

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

const privateThread = {
  thread_id: THREAD_ID,
  agent_asset_id: AGENT_ID,
  agent_scope: "project",
  display_name: "Owner research",
  status: "idle",
  metadata: {
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T01:00:00Z",
  },
  version: 1,
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockProjectContext(page: Page, currentProject = project) {
  await page.route("**/api/v1/auth/me", (route) =>
    json(route, {
      id: ACCOUNT_ID,
      email: "runner@example.test",
      system_role: "user",
      needs_setup: false,
    }),
  );
  await page.route(/\/api\/projects(?:\?.*)?$/, (route) =>
    json(route, { items: [currentProject], next_cursor: null }),
  );
  await page.route(`**/api/projects/${PROJECT_ID}/enter`, (route) =>
    json(route, { ...currentProject, request_id: "req-enter" }),
  );
}

async function mockPrivateWork(page: Page, includeThread = true) {
  const requests: string[] = [];
  let hasStreamed = false;
  await page.route(`**/api/projects/${PROJECT_ID}/private-work/**`, (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    requests.push(`${request.method()} ${path}`);

    if (path.endsWith("/readiness")) {
      return json(route, {
        status: "ready",
        code: "PRIVATE_WORK_READY",
        request_id: "req-ready",
      });
    }
    if (path.endsWith("/threads/search")) {
      return json(route, { items: includeThread ? [privateThread] : [] });
    }
    if (path.endsWith(`/threads/${THREAD_ID}/state`)) {
      if (!includeThread) return json(route, { detail: "not found" }, 404);
      return json(route, {
        values: {
          title: "Owner research",
          messages: [
            {
              type: "human",
              id: "msg-project-history",
              content: [{ type: "text", text: "Previous project question" }],
            },
            ...(hasStreamed
              ? [
                  {
                    type: "human",
                    id: "msg-project-submitted",
                    content: [{ type: "text", text: "Hello from project" }],
                  },
                  {
                    type: "ai",
                    id: "msg-ai-1",
                    content: "Hello from DeerFlow!",
                  },
                ]
              : []),
          ],
          artifacts: [],
          todos: [],
        },
        next: [],
        metadata: {},
        checkpoint: {},
        checkpoint_id: null,
        parent_checkpoint_id: null,
        created_at: "2026-07-15T01:00:00Z",
        tasks: [],
      });
    }
    if (path.endsWith(`/threads/${THREAD_ID}/token-usage`)) {
      return json(route, {
        total_input_tokens: 0,
        total_output_tokens: 0,
        total_tokens: 0,
      });
    }
    if (path.endsWith(`/threads/${THREAD_ID}`)) {
      return includeThread
        ? json(route, privateThread)
        : json(route, { detail: "not found" }, 404);
    }
    if (path.endsWith(`/threads/${THREAD_ID}/runs/stream`)) {
      hasStreamed = true;
      return handleRunStream(route);
    }
    if (/\/threads\/[^/]+\/runs(?:\?|$)/u.test(request.url())) {
      return json(route, []);
    }
    return json(route, { detail: "not found" }, 404);
  });
  return requests;
}

test.beforeEach(async ({ page }) => {
  mockLangGraphAPI(page);
  await mockProjectContext(page);
});

test("project detail loads history and streams without legacy private-work calls", async ({
  page,
}) => {
  const projectRequests = await mockPrivateWork(page);
  const legacyPrivateRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      path.startsWith("/api/langgraph/threads") ||
      path.startsWith("/api/threads/")
    ) {
      legacyPrivateRequests.push(`${request.method()} ${path}`);
    }
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);

  await expect(page.getByText("Previous project question")).toBeVisible();
  await expect(page.getByRole("button", { name: "Submit" })).toBeVisible();
  await expect(page.getByTestId("add-attachments-button")).toBeVisible();
  await expect(page.getByText("Scheduled tasks")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /sidecar/i })).toHaveCount(0);

  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("Hello from project");
  await textarea.press("Enter");
  await expect(page.getByText("Hello from DeerFlow!")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Branch conversation" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Regenerate" })).toHaveCount(0);

  expect(projectRequests).toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/stream`,
  );
  expect(legacyPrivateRequests).toEqual([]);
});

test("project list is owner-scoped and direct metadata misses show one public not-found", async ({
  page,
}) => {
  await mockPrivateWork(page);
  await page.goto("/projects/research-lab/chats");
  await expect(page.getByText("Owner research")).toBeVisible();
  await expect(page.getByRole("button", { name: "新建对话" })).toBeDisabled();

  await mockPrivateWork(page, false);
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(
    page.getByRole("heading", { name: "找不到这个对话" }),
  ).toBeVisible();
  await expect(page.getByText(/owner|跨项目|其他用户/iu)).toHaveCount(0);
});
