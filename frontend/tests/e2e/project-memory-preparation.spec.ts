import { expect, test, type Page, type Route } from "@playwright/test";

import type { MemoryDreamPreparationStatus } from "@/core/private-work/memory/types";
import type { Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const THREAD_ID = "20000000-0000-4000-8000-000000000001";
const AGENT_ID = "30000000-0000-4000-8000-000000000001";
const FIRST_PREPARATION_JOB_ID = "40000000-0000-4000-8000-000000000001";
const SECOND_PREPARATION_JOB_ID = "40000000-0000-4000-8000-000000000002";
const DREAM_JOB_ID = "50000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-13T00:00:00Z";

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Dream preparation browser coverage",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "private_work.read_own",
    "private_work.create",
    "shared_assets.execute",
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

const thread = {
  thread_id: THREAD_ID,
  agent_asset_id: AGENT_ID,
  agent_scope: "system",
  display_name: "Durable Dream preparation",
  status: "idle",
  metadata: {},
  version: 1,
  created_at: TIMESTAMP,
  updated_at: TIMESTAMP,
} as const;

const systemAgent = {
  id: AGENT_ID,
  scope: "system",
  project_id: null,
  slug: "project-assistant",
  display_name: "Main",
  status: "active",
  current_published_version_id: null,
  version: 1,
  created_by_user_id: ACCOUNT_ID,
  created_at: TIMESTAMP,
  updated_at: TIMESTAMP,
  capabilities: [],
  binding: null,
  description: "Main project Agent",
} as const;

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function queuedPreparation(jobId: string): MemoryDreamPreparationStatus {
  return {
    jobId,
    status: "queued",
    phase: "queued",
    compactedPasses: 0,
    dreamJobId: null,
    historyCount: null,
    admissionKind: null,
    resultDisposition: "queued",
    cancelRequested: false,
    publicErrorCode: null,
    updatedAt: TIMESTAMP,
  };
}

async function mockProjectChatWithDreamPreparation(page: Page) {
  let preparation: MemoryDreamPreparationStatus | null = null;
  let admissions = 0;
  let latestReads = 0;
  const admissionBodies: unknown[] = [];
  const unexpectedRequests: string[] = [];
  const memoryBase = `/api/projects/${PROJECT_ID}/memory`;
  const privateWorkBase = `/api/projects/${PROJECT_ID}/private-work`;

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
    if (path === "/api/models" && method === "GET") {
      return json(route, {
        models: [
          {
            name: "mock-model",
            model: "mock-model",
            display_name: "Mock model",
            description: "Deterministic browser-test model",
            supports_thinking: false,
            supports_reasoning_effort: false,
            supports_vision: false,
            is_default: true,
          },
        ],
        token_usage: { enabled: false },
      });
    }
    if (path === `/api/projects/${PROJECT_ID}/agents` && method === "GET") {
      return json(route, {
        system_items: [systemAgent],
        project_items: [],
        request_id: "request-agents",
      });
    }
    if (path === `/api/projects/${PROJECT_ID}/skills` && method === "GET") {
      return json(route, {
        system_items: [],
        project_items: [],
        request_id: "request-skills",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/default-agent` &&
      method === "GET"
    ) {
      return json(route, {
        agent_asset_id: AGENT_ID,
        revision: 1,
        request_id: "request-default-agent",
      });
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
    if (path === `${privateWorkBase}/readiness` && method === "GET") {
      return json(route, {
        status: "ready",
        code: "READY",
        request_id: "request-readiness",
      });
    }
    if (path === `${privateWorkBase}/threads/search` && method === "POST") {
      return json(route, { items: [thread] });
    }
    if (
      path === `${privateWorkBase}/threads/${THREAD_ID}` &&
      method === "GET"
    ) {
      return json(route, thread);
    }
    if (
      path === `${privateWorkBase}/threads/${THREAD_ID}/state` &&
      method === "GET"
    ) {
      return json(route, {
        values: {
          title: thread.display_name,
          messages: [],
          artifacts: [],
          todos: [],
        },
        next: [],
        metadata: {},
        checkpoint: {},
        checkpoint_id: null,
        parent_checkpoint_id: null,
        created_at: TIMESTAMP,
        tasks: [],
      });
    }
    if (
      path === `${privateWorkBase}/threads/${THREAD_ID}/runs` &&
      method === "GET"
    ) {
      return json(route, []);
    }
    if (
      path === `${privateWorkBase}/threads/${THREAD_ID}/context-usage` &&
      method === "GET"
    ) {
      return json(route, {
        thread_id: THREAD_ID,
        enabled: true,
        estimated_tokens: 0,
        message_count: 0,
        summary_present: false,
        context_window_tokens: 100_000,
        triggers: [],
        primary_trigger: null,
      });
    }
    if (
      path === `${privateWorkBase}/threads/${THREAD_ID}/uploads/limits` &&
      method === "GET"
    ) {
      return json(route, {
        max_files: 5,
        max_file_size: 10_000_000,
        max_total_size: 20_000_000,
        project_storage: {
          policy: "project_quota",
          remaining_bytes: 1_000_000_000,
        },
        request_id: "request-upload-limits",
      });
    }
    if (
      path === `${privateWorkBase}/threads/${THREAD_ID}/uploads` &&
      method === "GET"
    ) {
      return json(route, []);
    }
    if (
      path === `${memoryBase}/dream-preparations/latest` &&
      method === "GET"
    ) {
      latestReads += 1;
      if (url.searchParams.get("threadId") !== THREAD_ID) {
        unexpectedRequests.push(`${method} ${url.pathname}${url.search}`);
        return json(route, { detail: "unexpected thread scope" }, 400);
      }
      return preparation === null
        ? json(
            route,
            {
              detail: {
                code: "MEMORY_DREAM_PREPARE_NOT_FOUND",
                message: "No Dream preparation exists for this thread",
              },
            },
            404,
          )
        : json(route, preparation);
    }
    if (path === `${memoryBase}/dream-preparations` && method === "POST") {
      admissions += 1;
      admissionBodies.push(request.postDataJSON());
      const jobId =
        admissions === 1 ? FIRST_PREPARATION_JOB_ID : SECOND_PREPARATION_JOB_ID;
      preparation = queuedPreparation(jobId);
      return json(
        route,
        { disposition: "queued", jobId, status: "queued" },
        202,
      );
    }
    if (
      preparation !== null &&
      path === `${memoryBase}/dream-preparations/${preparation.jobId}/cancel` &&
      method === "POST"
    ) {
      preparation = {
        ...preparation,
        status: "cancelled",
        phase: "cancelled",
        resultDisposition: "cancelled",
        cancelRequested: true,
      };
      return json(route, preparation);
    }

    unexpectedRequests.push(`${method} ${url.pathname}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return {
    admissionBodies,
    latestReads: () => latestReads,
    unexpectedRequests,
    setRunning(compactedPasses: number) {
      if (preparation === null) throw new Error("Preparation was not admitted");
      preparation = {
        ...preparation,
        status: "running",
        phase: "draining",
        compactedPasses,
      };
    },
    setSucceeded() {
      if (preparation === null) throw new Error("Preparation was not admitted");
      preparation = {
        ...preparation,
        status: "succeeded",
        phase: "succeeded",
        dreamJobId: DREAM_JOB_ID,
        historyCount: 2,
        admissionKind: "history",
        resultDisposition: "queued",
      };
    },
  };
}

test("a durable Dream preparation recovers, cancels, and completes through the chat composer", async ({
  page,
}) => {
  const preparation = await mockProjectChatWithDreamPreparation(page);
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await expect(composer).toBeVisible();
  await expect(composer).toBeEnabled();

  // Keep the trailing space so Enter submits the built-in command instead of
  // accepting the slash-command autocomplete first.
  await composer.fill("/dream ");
  await composer.press("Enter");
  const status = page.getByTestId("dream-preparation-status");
  await expect(status).toContainText("Dream preparation is queued.");
  expect(preparation.admissionBodies).toHaveLength(1);
  expect(preparation.admissionBodies[0]).toEqual({
    threadId: THREAD_ID,
    operationId: expect.stringMatching(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u,
    ),
  });

  preparation.setRunning(1);
  await expect(status).toContainText(
    "Archiving earlier chat turns for Dream…",
    {
      timeout: 5_000,
    },
  );
  await expect(status).toContainText("(1 archive passes)");

  const latestReadsBeforeReload = preparation.latestReads();
  await page.reload();
  await expect(page.getByTestId("dream-preparation-status")).toContainText(
    "Archiving earlier chat turns for Dream…",
  );
  expect(preparation.latestReads()).toBeGreaterThan(latestReadsBeforeReload);

  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByTestId("dream-preparation-status")).toContainText(
    "Dream preparation was cancelled.",
  );

  await composer.fill("/dream ");
  await composer.press("Enter");
  await expect(page.getByTestId("dream-preparation-status")).toContainText(
    "Dream preparation is queued.",
  );
  expect(preparation.admissionBodies).toHaveLength(2);

  preparation.setRunning(2);
  await expect(page.getByTestId("dream-preparation-status")).toContainText(
    "Archiving earlier chat turns for Dream…",
    { timeout: 5_000 },
  );
  preparation.setSucceeded();
  await expect(page.getByTestId("dream-preparation-status")).toContainText(
    "Dream preparation completed.",
    { timeout: 5_000 },
  );
  await expect(
    page.getByRole("button", { name: "Cancel", exact: true }),
  ).toHaveCount(0);
  expect(preparation.unexpectedRequests).toEqual([]);
});
