import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const THREAD_ID = "20000000-0000-4000-8000-000000000001";
const AGENT_ID = "30000000-0000-4000-8000-000000000001";
const MODEL_ID = "30000000-0000-4000-8000-000000000002";
const FILE_ID = "40000000-0000-4000-8000-000000000001";
const RUN_ID = "50000000-0000-4000-8000-000000000001";
const FILE_NAME = "preupload-notes.txt";
const FILE_CONTENT = "Upload this before the message is sent.";
const TIMESTAMP = "2026-08-16T00:00:00Z";

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Attachment pre-upload browser coverage",
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
  display_name: "Attachment pre-upload",
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

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: Deferred<T>["resolve"];
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockProjectChat(page: Page) {
  const privateWorkBase = `/api/projects/${PROJECT_ID}/private-work`;
  const uploadPath = `${privateWorkBase}/threads/${THREAD_ID}/uploads`;
  const releaseUpload = deferred<void>();
  const unexpectedRequests: string[] = [];
  const uploadRequests: string[] = [];
  let uploadPostCount = 0;
  let runPostCount = 0;

  await page.route("**/api/**", async (route) => {
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
            name: MODEL_ID,
            model: MODEL_ID,
            display_name: "Mock model",
            supports_thinking: false,
            supports_reasoning_effort: false,
            supports_vision: false,
            supports_vision_bridge: false,
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
      path ===
        `${privateWorkBase}/threads/${THREAD_ID}/execution-approvals/active` &&
      method === "GET"
    ) {
      return json(route, {
        schema_version: 1,
        server_time: TIMESTAMP,
        approval: null,
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
    if (path === uploadPath && method === "GET") {
      return json(route, []);
    }
    if (path === uploadPath && method === "POST") {
      uploadPostCount += 1;
      uploadRequests.push(`${method} ${path}`);
      await releaseUpload.promise;
      return json(
        route,
        {
          id: FILE_ID,
          logical_path: `uploads/${FILE_NAME}`,
          display_name: FILE_NAME,
          kind: "upload",
          media_type: "text/plain",
          size: Buffer.byteLength(FILE_CONTENT),
          sha256: "a".repeat(64),
          status: "ready",
          created_at: TIMESTAMP,
          updated_at: TIMESTAMP,
        },
        201,
      );
    }
    if (
      path === `${privateWorkBase}/threads/${THREAD_ID}/runs/stream` &&
      method === "POST"
    ) {
      runPostCount += 1;
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: [
          "event: metadata",
          `data: ${JSON.stringify({ run_id: RUN_ID, thread_id: THREAD_ID })}`,
          "id: 1",
          "",
          "event: end",
          'data: {"status":"success"}',
          "id: 2",
          "",
          "",
        ].join("\n"),
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/memory/dream-preparations/latest` &&
      method === "GET"
    ) {
      return json(
        route,
        {
          detail: {
            code: "MEMORY_DREAM_PREPARE_NOT_FOUND",
            message: "No Dream preparation exists for this thread",
          },
        },
        404,
      );
    }

    unexpectedRequests.push(`${method} ${url.pathname}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return {
    releaseUpload: () => releaseUpload.resolve(),
    runPostCount: () => runPostCount,
    unexpectedRequests,
    uploadPostCount: () => uploadPostCount,
    uploadRequests,
    uploadPath,
  };
}

test("uploads an attachment before send while keeping the composer interactive", async ({
  page,
}) => {
  const requests = await mockProjectChat(page);
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await expect(composer).toBeVisible();
  await expect
    .poll(
      async () => ({
        enabled: await composer.isEnabled(),
        unexpectedRequests: [...requests.unexpectedRequests],
      }),
      { timeout: 5_000 },
    )
    .toEqual({ enabled: true, unexpectedRequests: [] });

  await page.getByLabel("Upload files").setInputFiles({
    name: FILE_NAME,
    mimeType: "text/plain",
    buffer: Buffer.from(FILE_CONTENT),
  });

  try {
    await expect.poll(requests.uploadPostCount).toBe(1);
    expect(requests.uploadRequests).toEqual([`POST ${requests.uploadPath}`]);

    const attachment = page.locator("[data-upload-status]", {
      hasText: FILE_NAME,
    });
    await expect(attachment).toHaveAttribute("data-upload-status", "uploading");
    await expect(composer).toBeEnabled();
    await composer.fill("Use the attached notes.");
    await expect(composer).toHaveValue("Use the attached notes.");

    requests.releaseUpload();
    await expect(attachment).toHaveAttribute("data-upload-status", "ready");

    await composer.press("Enter");
    await expect.poll(requests.runPostCount).toBe(1);
    expect(requests.uploadPostCount()).toBe(1);
    expect(requests.unexpectedRequests).toEqual([]);
  } finally {
    requests.releaseUpload();
  }
});
