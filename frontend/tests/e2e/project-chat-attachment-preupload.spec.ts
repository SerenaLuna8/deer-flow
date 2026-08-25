import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const THREAD_ID = "20000000-0000-4000-8000-000000000001";
const AGENT_ID = "30000000-0000-4000-8000-000000000001";
const MODEL_ID = "30000000-0000-4000-8000-000000000002";
const FILE_ID = "40000000-0000-4000-8000-000000000001";
const RUN_ID = "50000000-0000-4000-8000-000000000001";
const SECOND_RUN_ID = "50000000-0000-4000-8000-000000000002";
const FILE_NAME = "preupload-notes.txt";
const FILE_CONTENT = "Upload this before the message is sent.";
const CLIPBOARD_IMAGE_NAME = "clipboard.png";
const CLIPBOARD_IMAGE_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGP8z8Dwn4GBgYEJRIAwAB8XAgICR7MUAAAAAElFTkSuQmCC";
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
  current_version_id: null,
  revision: 1,
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

type MockRunControlEvent = Record<string, unknown> & {
  type: string;
  reason_code: string;
  observation_id: string;
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

async function mockProjectChat(
  page: Page,
  upload: {
    filename: string;
    mediaType: string;
    size: number;
    firstRunError?: string;
    firstRunAdmissionError?: string;
    effectiveWorkloadProfiles?: readonly ("interactive" | "research")[];
    liveRunControlEvents?: readonly MockRunControlEvent[];
    duplicateLiveRunControlEvents?: boolean;
    liveValuesMessages?: readonly Record<string, unknown>[];
    continuationValuesMessages?: readonly Record<string, unknown>[];
    followupSuggestions?: readonly string[];
    gateFirstRunAdmission?: boolean;
  } = {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
  },
) {
  const privateWorkBase = `/api/projects/${PROJECT_ID}/private-work`;
  const uploadPath = `${privateWorkBase}/threads/${THREAD_ID}/uploads`;
  const releaseUpload = deferred<void>();
  const firstRunAdmissionGate = deferred<void>();
  const unexpectedRequests: string[] = [];
  const uploadRequests: string[] = [];
  const runRequestBodies: unknown[] = [];
  let uploadPostCount = 0;
  let runPostCount = 0;
  let runListGetCount = 0;
  let runMessagesGetCount = 0;
  let suggestionPostCount = 0;
  let failedRunVisible = false;
  let completedRunId: string | null = null;
  let effectiveWorkloadProfile: "interactive" | "research" | null = null;
  let durableRunId: string | null = null;
  let durableRunControlEvents: Record<string, unknown>[] = [];

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
            supports_vision: true,
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
      runListGetCount += 1;
      const successfulRunIds = completedRunId
        ? upload.continuationValuesMessages && runPostCount >= 2
          ? [SECOND_RUN_ID, RUN_ID]
          : [completedRunId]
        : [];
      return json(
        route,
        failedRunVisible && upload.firstRunError
          ? [
              {
                run_id: RUN_ID,
                thread_id: THREAD_ID,
                assistant_id: AGENT_ID,
                created_at: TIMESTAMP,
                updated_at: TIMESTAMP,
                status: "error",
                metadata: {},
                multitask_strategy: "reject",
                error: upload.firstRunError,
                model_name: MODEL_ID,
                execution_profile: {
                  model_name: MODEL_ID,
                  thinking_enabled: false,
                  reasoning_effort: null,
                  supports_vision: true,
                },
                workload_profile: effectiveWorkloadProfile,
              },
            ]
          : successfulRunIds.map((runId) => ({
              run_id: runId,
              thread_id: THREAD_ID,
              assistant_id: AGENT_ID,
              created_at: TIMESTAMP,
              updated_at: TIMESTAMP,
              status: "success",
              metadata: {},
              multitask_strategy: "reject",
              error: null,
              model_name: MODEL_ID,
              execution_profile: {
                model_name: MODEL_ID,
                thinking_enabled: false,
                reasoning_effort: null,
                supports_vision: true,
              },
              workload_profile: effectiveWorkloadProfile,
            })),
      );
    }
    if (
      (path ===
        `${privateWorkBase}/threads/${THREAD_ID}/runs/${RUN_ID}/events` ||
        path ===
          `${privateWorkBase}/threads/${THREAD_ID}/runs/${SECOND_RUN_ID}/events`) &&
      method === "GET"
    ) {
      const requestedRunId = path.includes(SECOND_RUN_ID)
        ? SECOND_RUN_ID
        : RUN_ID;
      return json(
        route,
        requestedRunId === durableRunId ? durableRunControlEvents : [],
      );
    }
    if (
      (path ===
        `${privateWorkBase}/threads/${THREAD_ID}/runs/${RUN_ID}/messages` ||
        path ===
          `${privateWorkBase}/threads/${THREAD_ID}/runs/${SECOND_RUN_ID}/messages`) &&
      method === "GET"
    ) {
      runMessagesGetCount += 1;
      const requestedRunId = path.includes(SECOND_RUN_ID)
        ? SECOND_RUN_ID
        : RUN_ID;
      const durableMessages = path.includes(SECOND_RUN_ID)
        ? upload.continuationValuesMessages?.slice(-1)
        : upload.liveValuesMessages;
      if (durableMessages) {
        return json(route, {
          data: durableMessages.map((content, index) => ({
            run_id: requestedRunId,
            seq: String(index + 1),
            content,
            metadata: { caller: "lead_agent" },
            created_at: TIMESTAMP,
          })),
          has_more: false,
        });
      }
      if (!upload.firstRunError) {
        return json(route, { data: [], has_more: false });
      }
      return json(route, {
        data: [
          {
            run_id: RUN_ID,
            seq: "1",
            content: {
              type: "human",
              id: "human-failed-clipboard",
              content: "",
              additional_kwargs: {
                files: [
                  {
                    file_id: FILE_ID,
                    filename: upload.filename,
                    size: upload.size,
                    path: `uploads/${upload.filename}`,
                    status: "uploaded",
                  },
                ],
              },
            },
            metadata: { caller: "lead_agent", source: "run_admission" },
            created_at: TIMESTAMP,
          },
        ],
        has_more: false,
      });
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
        `${privateWorkBase}/threads/${THREAD_ID}/context-usage/authority` &&
      method === "GET"
    ) {
      return json(route, {
        thread_id: THREAD_ID,
        cache_marker: completedRunId ? `idle:${completedRunId}` : "idle:none",
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
          logical_path: `uploads/${upload.filename}`,
          display_name: upload.filename,
          kind: "upload",
          media_type: upload.mediaType,
          size: upload.size,
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
      const requestBody = request.postDataJSON() as Record<string, unknown>;
      runRequestBodies.push(requestBody);
      if (runPostCount === 1 && upload.gateFirstRunAdmission) {
        await firstRunAdmissionGate.promise;
      }
      effectiveWorkloadProfile =
        upload.effectiveWorkloadProfiles?.[runPostCount - 1] ??
        (requestBody.workload_profile === "research"
          ? "research"
          : "interactive");
      if (runPostCount === 1 && upload.firstRunAdmissionError) {
        return json(
          route,
          {
            detail: {
              code: upload.firstRunAdmissionError,
              message: "Run admission rejected for browser coverage",
            },
          },
          422,
        );
      }
      const shouldFail = runPostCount === 1 && Boolean(upload.firstRunError);
      failedRunVisible = shouldFail;
      const currentRunId = runPostCount === 1 ? RUN_ID : SECOND_RUN_ID;
      const liveRunControlEvents = (upload.liveRunControlEvents ?? []).map(
        (event) => ({
          ...event,
          run_id: currentRunId,
        }),
      );
      durableRunId = currentRunId;
      durableRunControlEvents = liveRunControlEvents.map((event, index) => {
        const { type, ...content } = event;
        const eventType =
          type === "repeated_call"
            ? "middleware:repeated_call"
            : type === "subagent_limit"
              ? "middleware:subagent_limit"
              : "middleware:tool_call_budget";
        return {
          thread_id: THREAD_ID,
          run_id: currentRunId,
          event_type: eventType,
          category: "middleware",
          content,
          metadata: {
            reason_code: event.reason_code,
            observation_id: event.observation_id,
          },
          seq: String(index + 10),
          created_at: TIMESTAMP,
        };
      });
      if (!shouldFail) {
        completedRunId = currentRunId;
      }
      const liveFrames = liveRunControlEvents.flatMap((event) =>
        upload.duplicateLiveRunControlEvents ? [event, event] : [event],
      );
      const streamLines = [
        "event: metadata",
        `data: ${JSON.stringify({ run_id: currentRunId, thread_id: THREAD_ID })}`,
        "id: 1",
        "",
      ];
      const liveValuesMessages =
        runPostCount === 1
          ? upload.liveValuesMessages
          : upload.continuationValuesMessages;
      if (liveValuesMessages) {
        streamLines.push(
          "event: values",
          `data: ${JSON.stringify({ messages: liveValuesMessages })}`,
          "id: 2",
          "",
        );
      }
      liveFrames.forEach((event, index) => {
        streamLines.push(
          "event: custom",
          `data: ${JSON.stringify(event)}`,
          `id: ${index + (liveValuesMessages ? 3 : 2)}`,
          "",
        );
      });
      streamLines.push(
        "event: end",
        `data: ${JSON.stringify(
          shouldFail
            ? { status: "error", error_code: upload.firstRunError }
            : { status: "success" },
        )}`,
        `id: ${liveFrames.length + (liveValuesMessages ? 3 : 2)}`,
        "",
        "",
      );
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: {
          "Content-Location": `${privateWorkBase}/threads/${THREAD_ID}/runs/${currentRunId}`,
          Location: `/threads/${THREAD_ID}/runs/${currentRunId}/stream`,
        },
        body: streamLines.join("\n"),
      });
    }
    if (
      path === `${privateWorkBase}/threads/${THREAD_ID}/suggestions` &&
      method === "POST"
    ) {
      suggestionPostCount += 1;
      return json(route, {
        suggestions: upload.followupSuggestions ?? [],
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
    releaseFirstRunAdmission: () => firstRunAdmissionGate.resolve(),
    runPostCount: () => runPostCount,
    runListGetCount: () => runListGetCount,
    runMessagesGetCount: () => runMessagesGetCount,
    suggestionPostCount: () => suggestionPostCount,
    runRequestBodies,
    unexpectedRequests,
    uploadPostCount: () => uploadPostCount,
    uploadRequests,
    uploadPath,
  };
}

test("does not offer follow-up suggestions while the user is answering a clarification", async ({
  page,
}) => {
  const clarificationRequestId = "clarification:call-scope";
  const prematureSuggestion = "Compare the two Harness scopes.";
  const clarificationMessages: Record<string, unknown>[] = [
    {
      id: "human-research",
      type: "human",
      content: "Research Harness history.",
      additional_kwargs: { run_id: RUN_ID },
    },
    {
      id: "ai-clarification",
      type: "ai",
      content: "",
      additional_kwargs: { run_id: RUN_ID },
      tool_calls: [
        {
          id: "call-scope",
          name: "ask_clarification",
          args: { question: "Which Harness scope should I cover?" },
        },
      ],
    },
    {
      id: clarificationRequestId,
      type: "tool",
      name: "ask_clarification",
      tool_call_id: "call-scope",
      content: "Which Harness scope should I cover?",
      additional_kwargs: { run_id: RUN_ID },
      artifact: {
        human_input: {
          version: 1,
          kind: "human_input_request",
          source: "ask_clarification",
          request_id: clarificationRequestId,
          tool_call_id: "call-scope",
          clarification_type: "scope",
          question: "Which Harness scope should I cover?",
          input_mode: "choice_with_other",
          options: [
            {
              id: "agent-and-harness",
              label: "Agent + Harness",
              value: "agent+harness",
            },
          ],
        },
      },
    },
  ];
  const answerResponse = {
    version: 1,
    kind: "human_input_response",
    source: "ask_clarification",
    request_id: clarificationRequestId,
    response_kind: "option",
    option_id: "agent-and-harness",
    value: "agent+harness",
  };
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    followupSuggestions: [prematureSuggestion],
    liveValuesMessages: clarificationMessages,
    continuationValuesMessages: [
      ...clarificationMessages,
      {
        id: "human-clarification-response",
        type: "human",
        content:
          'For your clarification "Which Harness scope should I cover?", my answer is: agent+harness',
        additional_kwargs: {
          hide_from_ui: true,
          human_input_response: answerResponse,
          run_id: SECOND_RUN_ID,
        },
      },
    ],
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill("Research Harness history.");
  await composer.press("Enter");

  await expect(
    page.getByText("Which Harness scope should I cover?", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(prematureSuggestion)).toHaveCount(0);
  await expect.poll(requests.suggestionPostCount).toBe(0);

  const humanInputCard = page.getByTestId("human-input-card");
  await humanInputCard.getByRole("radio", { name: "Agent + Harness" }).click();
  await humanInputCard
    .getByRole("button", { name: /Submit answer|提交回答/u })
    .click();
  await expect.poll(requests.runPostCount).toBe(2);
  await expect(humanInputCard).toHaveAttribute(
    "data-human-input-state",
    "answered",
  );
  await expect(humanInputCard).toContainText(/Answered:|已回答：/u);
  await expect(page.getByText(prematureSuggestion)).toHaveCount(0);
  await expect.poll(requests.suggestionPostCount).toBe(0);
  expect(requests.unexpectedRequests).toEqual([]);
});

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

test("pastes an image, uploads it, and submits only its opaque file reference", async ({
  page,
}) => {
  const imageBytes = Buffer.from(CLIPBOARD_IMAGE_BASE64, "base64");
  const requests = await mockProjectChat(page, {
    filename: CLIPBOARD_IMAGE_NAME,
    mediaType: "image/png",
    size: imageBytes.byteLength,
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await expect(composer).toBeVisible();
  await expect(composer).toBeEnabled();

  await composer.evaluate(
    (element, image) => {
      const binary = atob(image.base64);
      const bytes = Uint8Array.from(binary, (character) =>
        character.charCodeAt(0),
      );
      const clipboard = new DataTransfer();
      clipboard.items.add(
        new File([bytes], image.filename, { type: "image/png" }),
      );
      element.dispatchEvent(
        new ClipboardEvent("paste", {
          bubbles: true,
          cancelable: true,
          clipboardData: clipboard,
        }),
      );
    },
    { base64: CLIPBOARD_IMAGE_BASE64, filename: CLIPBOARD_IMAGE_NAME },
  );

  try {
    await expect.poll(requests.uploadPostCount).toBe(1);
    const attachment = page.locator("[data-upload-status]", {
      hasText: CLIPBOARD_IMAGE_NAME,
    });
    await expect(attachment).toHaveAttribute("data-upload-status", "uploading");

    requests.releaseUpload();
    await expect(attachment).toHaveAttribute("data-upload-status", "ready");
    await composer.press("Enter");

    await expect.poll(requests.runPostCount).toBe(1);
    expect(requests.uploadPostCount()).toBe(1);
    expect(requests.runRequestBodies).toHaveLength(1);
    const serializedRun = JSON.stringify(requests.runRequestBodies[0]);
    expect(serializedRun).toContain(FILE_ID);
    expect(serializedRun).toContain(CLIPBOARD_IMAGE_NAME);
    expect(serializedRun).not.toContain("data:image/");
    expect(serializedRun).not.toContain(CLIPBOARD_IMAGE_BASE64);
    expect(requests.unexpectedRequests).toEqual([]);
  } finally {
    requests.releaseUpload();
  }
});

test("restores a failed pasted image and resubmits without another upload", async ({
  page,
}) => {
  const imageBytes = Buffer.from(CLIPBOARD_IMAGE_BASE64, "base64");
  const requests = await mockProjectChat(page, {
    filename: CLIPBOARD_IMAGE_NAME,
    mediaType: "image/png",
    size: imageBytes.byteLength,
    firstRunError: "CURRENT_UPLOAD_UNAVAILABLE",
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await expect(composer).toBeVisible();
  await composer.evaluate(
    (element, image) => {
      const binary = atob(image.base64);
      const bytes = Uint8Array.from(binary, (character) =>
        character.charCodeAt(0),
      );
      const clipboard = new DataTransfer();
      clipboard.items.add(
        new File([bytes], image.filename, { type: "image/png" }),
      );
      element.dispatchEvent(
        new ClipboardEvent("paste", {
          bubbles: true,
          cancelable: true,
          clipboardData: clipboard,
        }),
      );
    },
    { base64: CLIPBOARD_IMAGE_BASE64, filename: CLIPBOARD_IMAGE_NAME },
  );

  try {
    await expect.poll(requests.uploadPostCount).toBe(1);
    requests.releaseUpload();
    const attachment = page.locator("[data-upload-status]", {
      hasText: CLIPBOARD_IMAGE_NAME,
    });
    await expect(attachment).toHaveAttribute("data-upload-status", "ready");
    await composer.press("Enter");

    await expect.poll(requests.runPostCount).toBe(1);
    await expect.poll(requests.runListGetCount).toBeGreaterThan(1);
    await expect.poll(requests.runMessagesGetCount).toBeGreaterThan(0);
    const failureAlert = page.getByTestId("run-failure-alert");
    await expect(failureAlert).toHaveAttribute(
      "data-run-failure-code",
      "CURRENT_UPLOAD_UNAVAILABLE",
    );
    await expect(failureAlert).toContainText(
      /Image attachment could not be read|当前图片附件不可用/u,
    );
    await page
      .getByRole("button", {
        name: /Restore to composer|恢复到输入框/u,
      })
      .click();

    const restoredAttachment = page.locator("[data-upload-status]", {
      hasText: CLIPBOARD_IMAGE_NAME,
    });
    await expect(restoredAttachment).toHaveAttribute(
      "data-upload-status",
      "ready",
    );
    await composer.press("Enter");

    await expect.poll(requests.runPostCount).toBe(2);
    expect(requests.uploadPostCount()).toBe(1);
    const replay = JSON.stringify(requests.runRequestBodies[1]);
    expect(replay).toContain(FILE_ID);
    expect(replay).not.toContain("data:image/");
    expect(replay).not.toContain(CLIPBOARD_IMAGE_BASE64);
    expect(requests.unexpectedRequests).toEqual([]);
  } finally {
    requests.releaseUpload();
  }
});

test("uses Research for one admitted Run and resets the next send to Interactive", async ({
  page,
}) => {
  const requests = await mockProjectChat(page);
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  const research = page.getByTestId("research-workload-toggle");
  await expect(composer).toBeEnabled();
  await expect(research).toHaveAttribute("aria-pressed", "false");

  await research.click();
  await expect(research).toHaveAttribute("aria-pressed", "true");
  await composer.fill("Research Agent history.");
  await composer.press("Enter");

  await expect.poll(requests.runPostCount).toBe(1);
  expect(requests.runRequestBodies[0]).toMatchObject({
    workload_profile: "research",
  });
  await expect(research).toHaveAttribute("aria-pressed", "false");

  await composer.fill("Summarize one point.");
  await composer.press("Enter");
  await expect.poll(requests.runPostCount).toBe(2);
  expect(requests.runRequestBodies[1]).toMatchObject({
    workload_profile: "interactive",
  });
  expect(requests.unexpectedRequests).toEqual([]);
});

test("does not display a server-confirmed workload profile badge", async ({
  page,
}) => {
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    effectiveWorkloadProfiles: ["interactive"],
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await page.getByTestId("research-workload-toggle").click();
  await composer.fill("Research Agent history.");
  await composer.press("Enter");

  await expect.poll(requests.runPostCount).toBe(1);
  expect(requests.runRequestBodies[0]).toMatchObject({
    workload_profile: "research",
  });
  await expect(page.getByTestId("effective-run-workload-profile")).toHaveCount(
    0,
  );
});

test("soft-wraps plain-text code blocks without changing their content", async ({
  page,
}) => {
  const plainText =
    "{路由1：佛山南海桂城二《局内路由》HongKongMEGIPOP1《局内路由》深圳信息枢纽大厦《滨海通信机楼ODF传输机房》}";
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    liveValuesMessages: [
      {
        id: "human-text-preview",
        type: "human",
        content: "Show the TXT route.",
        additional_kwargs: { run_id: RUN_ID },
      },
      {
        id: "ai-text-preview",
        type: "ai",
        content: `\`\`\`text\n${plainText}\n\`\`\``,
        additional_kwargs: { run_id: RUN_ID },
      },
    ],
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill("Show the TXT route.");
  await composer.press("Enter");
  await expect.poll(requests.runPostCount).toBe(1);

  const codeBlock = page.locator(
    '[data-code-block][data-language="text"]:visible',
  );
  await expect(codeBlock).toContainText(plainText);
  const presentation = await codeBlock.evaluate((element) => {
    const pre = element.querySelector("pre");
    return {
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      overflowX: getComputedStyle(element).overflowX,
      preFontSize: pre ? getComputedStyle(pre).fontSize : null,
      preLineHeight: pre ? getComputedStyle(pre).lineHeight : null,
      preOverflowX: pre ? getComputedStyle(pre).overflowX : null,
      preWhiteSpace: pre ? getComputedStyle(pre).whiteSpace : null,
      text: pre?.textContent ?? null,
    };
  });

  expect(presentation.overflowX).toBe("hidden");
  expect(presentation.preFontSize).toBe("14px");
  expect(presentation.preLineHeight).toBe("22px");
  expect(presentation.preOverflowX).toBe("hidden");
  expect(presentation.preWhiteSpace).toBe("pre-wrap");
  expect(presentation.scrollWidth).toBeLessThanOrEqual(
    presentation.clientWidth,
  );
  expect(presentation.text?.replace(/\n$/, "")).toBe(plainText);
  expect(presentation.text?.match(/\n/g) ?? []).toHaveLength(1);
});

test("mounts delivered files only after the final answer without moving them", async ({
  page,
}) => {
  const finalAnswer = "FINAL_DELIVERY_ANSWER";
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    liveValuesMessages: [
      {
        id: "human-delivery-order",
        type: "human",
        content: "Create and deliver the report.",
        additional_kwargs: { run_id: RUN_ID },
      },
      {
        id: "ai-present-files",
        type: "ai",
        content: "Preparing the report delivery.",
        additional_kwargs: { run_id: RUN_ID },
        tool_calls: [
          {
            id: "present-report",
            name: "present_files",
            args: { filepaths: ["outputs/report.md"] },
          },
        ],
      },
      {
        id: "tool-present-files",
        type: "tool",
        name: "present_files",
        tool_call_id: "present-report",
        content: "ok",
        additional_kwargs: { run_id: RUN_ID },
      },
      {
        id: "ai-final-delivery",
        type: "ai",
        content: finalAnswer,
        additional_kwargs: { run_id: RUN_ID },
      },
    ],
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);
  await page.evaluate((answer) => {
    const samples: string[] = [];
    const sample = () => {
      const card = document.querySelector(
        '[data-testid="assistant-delivered-files"]',
      );
      if (!card) return;
      const finalTurn = Array.from(
        document.querySelectorAll('[data-assistant-turn=""]'),
      ).find((element) => element.textContent?.includes(answer));
      if (!finalTurn) {
        samples.push("card-before-final-mounted");
        return;
      }
      samples.push(
        finalTurn.compareDocumentPosition(card) &
          Node.DOCUMENT_POSITION_FOLLOWING
          ? "card-after-final"
          : "card-before-final",
      );
    };
    const observer = new MutationObserver(sample);
    observer.observe(document.body, { childList: true, subtree: true });
    Reflect.set(window, "__deliveredFilesOrderProbe", { observer, samples });
  }, finalAnswer);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill("Create and deliver the report.");
  await composer.press("Enter");
  await expect.poll(requests.runPostCount).toBe(1);
  await expect(page.getByText(finalAnswer)).toBeVisible();
  await expect(page.getByTestId("assistant-delivered-files")).toHaveCount(1);

  const samples = await page.evaluate(() => {
    const probe = Reflect.get(window, "__deliveredFilesOrderProbe") as {
      observer: MutationObserver;
      samples: string[];
    };
    probe.observer.disconnect();
    return probe.samples;
  });
  expect(samples.length).toBeGreaterThan(0);
  expect(new Set(samples)).toEqual(new Set(["card-after-final"]));
});

test("keeps the one-Run Research choice after a pre-admission failure", async ({
  page,
}) => {
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    firstRunAdmissionError: "RUN_ADMISSION_REJECTED",
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  const research = page.getByTestId("research-workload-toggle");
  await expect(composer).toBeEnabled();
  await research.click();
  await composer.fill("Research Agent history.");
  await composer.press("Enter");

  await expect.poll(requests.runPostCount).toBe(1);
  expect(requests.runRequestBodies[0]).toMatchObject({
    workload_profile: "research",
  });
  await expect(research).toHaveAttribute("aria-pressed", "true");

  await composer.press("Enter");
  await expect.poll(requests.runPostCount).toBe(2);
  expect(requests.runRequestBodies[1]).toMatchObject({
    workload_profile: "research",
  });
  await expect(research).toHaveAttribute("aria-pressed", "false");
  expect(requests.unexpectedRequests).toEqual([]);
});

test("shows immediate send feedback and restores the draft when Run Admission is rejected", async ({
  page,
}) => {
  const message = "Confirm";
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    gateFirstRunAdmission: true,
    firstRunAdmissionError: "RUN_ADMISSION_REJECTED",
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  try {
    await composer.fill(message);
    await composer.press("Enter");
    await expect.poll(requests.runPostCount).toBe(1);

    await expect(page.getByTestId("run-activity")).toBeVisible();
    await expect(composer).toHaveValue("");
    await expect(composer).toBeDisabled();

    requests.releaseFirstRunAdmission();
    await expect(composer).toHaveValue(message);
    await expect(composer).toBeEnabled();
    await expect(composer).toBeFocused();
    await expect(page.getByTestId("run-activity")).toHaveCount(0);

    expect(requests.runRequestBodies).toHaveLength(1);
    expect(requests.unexpectedRequests).toEqual([]);
  } finally {
    requests.releaseFirstRunAdmission();
  }
});

test("deduplicates live and durable Run-control progress across refresh", async ({
  page,
}) => {
  const repeatedObservation = {
    type: "repeated_call",
    schema_version: 1,
    reason_code: "repeated_call_warning",
    workload_profile: "research",
    role: "lead",
    run_id: RUN_ID,
    execution_id: null,
    count_before: 1,
    proposed: 1,
    admitted: 1,
    rejected: 0,
    count_after: 2,
    warn_threshold: 2,
    hard_limit: 4,
    disposition: "advisory",
    observation_id: "a".repeat(64),
  } as const;
  const toolObservation = {
    type: "tool_call_budget",
    schema_version: 2,
    reason_code: "tool_budget_exhausted",
    workload_profile: "research",
    role: "lead",
    run_id: RUN_ID,
    execution_id: null,
    count_before: 9,
    proposed: 3,
    admitted: 1,
    rejected: 2,
    count_after: 10,
    hard_limit: 10,
    disposition: "truncate_tool_calls",
    observation_id: "b".repeat(64),
  } as const;
  const subagentObservation = {
    type: "subagent_limit",
    schema_version: 1,
    reason_code: "subagent_total_limit",
    role: "lead",
    run_id: RUN_ID,
    count_before: 8,
    proposed: 3,
    admitted: 1,
    rejected: 2,
    count_after: 9,
    hard_limit: 9,
    disposition: "truncate_tool_calls",
    observation_id: "c".repeat(64),
  } as const;
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    liveRunControlEvents: [
      repeatedObservation,
      toolObservation,
      subagentObservation,
    ],
    duplicateLiveRunControlEvents: true,
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill("Research the Agent timeline.");
  await composer.press("Enter");

  const progress = page.getByTestId("run-control-progress");
  await expect(progress).toBeVisible();
  await expect(
    progress.locator('[data-reason-code="repeated_call_warning"]'),
  ).toHaveCount(1);
  await expect(
    progress.locator('[data-reason-code="tool_budget_exhausted"]'),
  ).toHaveCount(1);
  await expect(
    progress.locator('[data-reason-code="subagent_total_limit"]'),
  ).toHaveCount(1);
  await expect(progress).toContainText(
    /finish with the evidence already collected|基于已有证据完成结果/u,
  );
  await expect(page.getByTestId("run-failure-alert")).toHaveCount(0);

  await page.reload();
  const replayedProgress = page.getByTestId("run-control-progress");
  await expect(
    replayedProgress.locator('[data-reason-code="repeated_call_warning"]'),
  ).toHaveCount(1);
  await expect(
    replayedProgress.locator('[data-reason-code="tool_budget_exhausted"]'),
  ).toHaveCount(1);
  await expect(
    replayedProgress.locator('[data-reason-code="subagent_total_limit"]'),
  ).toHaveCount(1);
  expect(requests.unexpectedRequests).toEqual([]);
});

test("keeps a specific terminal failure authoritative after a tool budget receipt", async ({
  page,
}) => {
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    firstRunError: "LLM_PROVIDER_UNAVAILABLE",
    liveRunControlEvents: [
      {
        type: "tool_call_budget",
        schema_version: 2,
        reason_code: "tool_budget_exhausted",
        workload_profile: "research",
        role: "lead",
        run_id: RUN_ID,
        execution_id: null,
        count_before: 9,
        proposed: 1,
        admitted: 1,
        rejected: 0,
        count_after: 10,
        hard_limit: 10,
        disposition: "exhaust_run",
        observation_id: "d".repeat(64),
      },
    ],
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill("Research the Agent timeline.");
  await composer.press("Enter");

  await expect(
    page.locator('[data-reason-code="tool_budget_exhausted"]'),
  ).toHaveCount(1);
  const failure = page.getByTestId("run-failure-alert");
  await expect(failure).toHaveAttribute(
    "data-run-failure-code",
    "LLM_PROVIDER_UNAVAILABLE",
  );
  await expect(failure).toContainText(
    /Model provider temporarily unavailable|模型服务暂时不可用/u,
  );
  expect(requests.unexpectedRequests).toEqual([]);
});
