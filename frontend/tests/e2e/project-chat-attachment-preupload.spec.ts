import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const THREAD_ID = "20000000-0000-4000-8000-000000000001";
const OTHER_THREAD_ID = "20000000-0000-4000-8000-000000000002";
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
  created_at: "2026-07-01T00:00:00Z",
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
  metadata: {
    agent_asset_id: AGENT_ID,
    agent_scope: "system",
  },
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
  definition_id: "30000000-0000-4000-8000-000000000003",
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

function emptyContextProjection(threadId: string) {
  return {
    contract_version: 2,
    thread_id: threadId,
    subject: { kind: "lead_thread", thread_id: threadId, execution_id: null },
    phase: "idle",
    projection_seq: "0",
    evidence_seq: "0",
    context_window_generation: "60000000-0000-4000-8000-000000000001",
    checkpoint_id: null,
    projector_revision: "context-projector-v2",
    model: {
      identity_digest: "a".repeat(64),
      context_window_tokens: 100_000,
    },
    basis: "empty",
    coverage: "complete",
    freshness: "current",
    totals: {
      projected_tokens: 0,
      lower_bound_tokens: 0,
      safety_upper_bound_tokens: 0,
      context_window_tokens: 100_000,
      remaining_tokens: 100_000,
      progress_percent: 0,
    },
    lanes: [],
    last_provider_observation: null,
    compaction: {
      enabled: true,
      threshold_tokens: 80_000,
      reached: false,
      authority: "idle_history",
      blocked_reason: null,
    },
    notices: [],
    as_of: TIMESTAMP,
  };
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
    canonicalValuesMessages?: readonly Record<string, unknown>[];
    continuationValuesMessages?: readonly Record<string, unknown>[];
    followupSuggestions?: readonly string[];
    gateFirstRunAdmission?: boolean;
    gateTerminalHandoff?: boolean;
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
  const terminalExecutionStateGate = deferred<void>();
  const canonicalHistoryGate = deferred<void>();
  const unexpectedRequests: string[] = [];
  const uploadRequests: string[] = [];
  const runRequestBodies: unknown[] = [];
  let uploadPostCount = 0;
  let runPostCount = 0;
  let runListGetCount = 0;
  let runMessagesGetCount = 0;
  let suggestionPostCount = 0;
  let executionStateGetCount = 0;
  let canonicalHistoryGetCount = 0;
  let canonicalHistoryReleased = false;
  let failedRunVisible = false;
  let completedRunId: string | null = null;
  let effectiveWorkloadProfile: "interactive" | "research" | null = null;
  let durableRunId: string | null = null;
  let durableRunControlEvents: Record<string, unknown>[] = [];

  if (upload.gateTerminalHandoff && upload.liveValuesMessages) {
    await page.addInitScript(
      ({ streamPath, threadId, runId, messages }) => {
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (input, init) => {
          const requestURL =
            input instanceof Request ? input.url : String(input);
          const requestMethod =
            init?.method ?? (input instanceof Request ? input.method : "GET");
          const path = new URL(requestURL, window.location.origin).pathname;
          if (requestMethod === "POST" && path === streamPath) {
            const encoder = new TextEncoder();
            const body = new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(
                  encoder.encode(
                    [
                      "event: metadata",
                      `data: ${JSON.stringify({ run_id: runId, thread_id: threadId })}`,
                      "id: 1",
                      "",
                      "event: values",
                      `data: ${JSON.stringify({ messages })}`,
                      "id: 2",
                      "",
                      "",
                    ].join("\n"),
                  ),
                );
              },
              cancel() {
                Reflect.set(window, "__terminalHeldStreamCancelled", true);
              },
            });
            return new Response(body, {
              status: 200,
              headers: {
                "Content-Type": "text/event-stream",
                "Content-Location": `${streamPath.replace(/\/runs\/stream$/u, "")}/runs/${runId}`,
                Location: `/threads/${threadId}/runs/${runId}/stream`,
              },
            });
          }
          return originalFetch(input, init);
        };
      },
      {
        streamPath: `${privateWorkBase}/threads/${THREAD_ID}/runs/stream`,
        threadId: THREAD_ID,
        runId: RUN_ID,
        messages: upload.liveValuesMessages,
      },
    );
  }

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
          messages:
            upload.gateTerminalHandoff && completedRunId
              ? canonicalHistoryReleased
                ? (upload.canonicalValuesMessages ?? upload.liveValuesMessages)
                : upload.liveValuesMessages
              : [],
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
      upload.gateTerminalHandoff &&
      completedRunId === RUN_ID &&
      path === `${privateWorkBase}/threads/${THREAD_ID}/runs/${RUN_ID}` &&
      method === "GET"
    ) {
      canonicalHistoryGetCount += 1;
      await canonicalHistoryGate.promise;
      return json(route, {
        run_id: RUN_ID,
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
      });
    }
    if (
      (path === `${privateWorkBase}/threads/${THREAD_ID}/runs/${RUN_ID}` ||
        path ===
          `${privateWorkBase}/threads/${THREAD_ID}/runs/${SECOND_RUN_ID}`) &&
      method === "GET"
    ) {
      const requestedRunId = path.includes(SECOND_RUN_ID)
        ? SECOND_RUN_ID
        : RUN_ID;
      const requestedRunFailed =
        requestedRunId === RUN_ID && Boolean(upload.firstRunError);
      return json(route, {
        run_id: requestedRunId,
        thread_id: THREAD_ID,
        assistant_id: AGENT_ID,
        created_at: TIMESTAMP,
        updated_at: TIMESTAMP,
        status: requestedRunFailed ? "error" : "success",
        metadata: {},
        multitask_strategy: "reject",
        error: requestedRunFailed ? upload.firstRunError : null,
        model_name: MODEL_ID,
        execution_profile: {
          model_name: MODEL_ID,
          thinking_enabled: false,
          reasoning_effort: null,
          supports_vision: true,
        },
        workload_profile: effectiveWorkloadProfile,
      });
    }
    if (
      upload.gateTerminalHandoff &&
      path ===
        `${privateWorkBase}/threads/${THREAD_ID}/runs/${RUN_ID}/execution-state` &&
      method === "GET"
    ) {
      executionStateGetCount += 1;
      await terminalExecutionStateGate.promise;
      completedRunId = RUN_ID;
      return json(route, {
        phase: "terminal",
        observed_at: TIMESTAMP,
        phase_started_at: TIMESTAMP,
        execution_started_at: TIMESTAMP,
        retry_at: null,
        run_status: "success",
      });
    }
    if (
      (path ===
        `${privateWorkBase}/threads/${THREAD_ID}/runs/${RUN_ID}/execution-state` ||
        path ===
          `${privateWorkBase}/threads/${THREAD_ID}/runs/${SECOND_RUN_ID}/execution-state`) &&
      method === "GET"
    ) {
      executionStateGetCount += 1;
      const requestedRunId = path.includes(SECOND_RUN_ID)
        ? SECOND_RUN_ID
        : RUN_ID;
      return json(route, {
        phase: "terminal",
        observed_at: TIMESTAMP,
        phase_started_at: TIMESTAMP,
        execution_started_at: TIMESTAMP,
        retry_at: null,
        run_status:
          requestedRunId === RUN_ID && Boolean(upload.firstRunError)
            ? "error"
            : "success",
      });
    }
    if (
      path ===
        `${privateWorkBase}/threads/${THREAD_ID}/runs/${RUN_ID}/feedback` &&
      method === "GET"
    ) {
      return json(route, null);
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
        : upload.gateTerminalHandoff && canonicalHistoryReleased
          ? (upload.canonicalValuesMessages ?? upload.liveValuesMessages)
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
      return json(route, emptyContextProjection(THREAD_ID));
    }
    if (
      path === `${privateWorkBase}/threads/${THREAD_ID}/context-usage/stream` &&
      method === "GET"
    ) {
      return route.fulfill({ status: 204 });
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
    releaseTerminalExecutionState: () => terminalExecutionStateGate.resolve(),
    releaseCanonicalHistory: () => {
      canonicalHistoryReleased = true;
      canonicalHistoryGate.resolve();
    },
    runPostCount: () => runPostCount,
    runListGetCount: () => runListGetCount,
    runMessagesGetCount: () => runMessagesGetCount,
    suggestionPostCount: () => suggestionPostCount,
    executionStateGetCount: () => executionStateGetCount,
    canonicalHistoryGetCount: () => canonicalHistoryGetCount,
    runRequestBodies,
    unexpectedRequests,
    uploadPostCount: () => uploadPostCount,
    uploadRequests,
    uploadPath,
  };
}

async function mockActiveSubtaskReconnect(page: Page) {
  const taskId = "task-active-reconnect";
  const taskDescription = "ACTIVE_SUBTASK_RECONNECT";
  const returnedStep = "RETURNED_TASK_RUNNING_MARKER";
  const privateWorkBase = `/api/projects/${PROJECT_ID}/private-work`;
  const activeMessages: Record<string, unknown>[] = [
    {
      id: "human-active-reconnect",
      type: "human",
      content: "Keep researching while I switch conversations.",
      additional_kwargs: { run_id: RUN_ID },
    },
    {
      id: "ai-active-reconnect",
      type: "ai",
      content: "",
      additional_kwargs: { run_id: RUN_ID },
      tool_calls: [
        {
          id: taskId,
          name: "task",
          args: {
            subagent_type: "general-purpose",
            description: taskDescription,
            prompt: "Continue the active research.",
          },
        },
      ],
    },
  ];
  const otherThread = {
    ...thread,
    thread_id: OTHER_THREAD_ID,
    display_name: "Other active conversation",
  };
  const base = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    liveValuesMessages: activeMessages,
  });
  const returnCatalogGate = deferred<void>();
  let gateReturnCatalog = false;

  await page.addInitScript(
    ({ streamPath, threadId, runId, messages, taskId, returnedStep }) => {
      const originalFetch = window.fetch.bind(window);
      window.fetch = async (input, init) => {
        const requestURL = input instanceof Request ? input.url : String(input);
        const requestMethod =
          init?.method ?? (input instanceof Request ? input.method : "GET");
        const path = new URL(requestURL, window.location.origin).pathname;
        if (requestMethod === "GET" && path === streamPath) {
          const encoder = new TextEncoder();
          const body = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(
                encoder.encode(
                  [
                    "event: metadata",
                    `data: ${JSON.stringify({ run_id: runId, thread_id: threadId })}`,
                    "id: 1",
                    "",
                    "event: values",
                    `data: ${JSON.stringify({ messages })}`,
                    "id: 2",
                    "",
                    "event: custom",
                    `data: ${JSON.stringify({
                      type: "task_running",
                      task_id: taskId,
                      message: {
                        type: "ai",
                        id: "returned-task-running",
                        content: returnedStep,
                      },
                      message_index: 1,
                      usage: {
                        input_tokens: 80_000,
                        output_tokens: 6_160,
                        total_tokens: 86_160,
                      },
                    })}`,
                    "id: 3",
                    "",
                    "",
                  ].join("\n"),
                ),
              );
            },
          });
          return new Response(body, {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          });
        }
        return originalFetch(input, init);
      };
    },
    {
      streamPath: `${privateWorkBase}/threads/${THREAD_ID}/runs/${RUN_ID}/stream`,
      threadId: THREAD_ID,
      runId: RUN_ID,
      messages: activeMessages,
      taskId,
      returnedStep,
    },
  );

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;
    const otherThreadBase = `${privateWorkBase}/threads/${OTHER_THREAD_ID}`;

    if (path === `${privateWorkBase}/threads/search` && method === "POST") {
      return json(route, { items: [thread, otherThread] });
    }
    if (path === `${privateWorkBase}/threads/${THREAD_ID}/state`) {
      return json(route, {
        values: {
          title: thread.display_name,
          messages: activeMessages,
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
      if (gateReturnCatalog) {
        await returnCatalogGate.promise;
      }
      return json(route, [
        {
          run_id: RUN_ID,
          thread_id: THREAD_ID,
          assistant_id: AGENT_ID,
          created_at: TIMESTAMP,
          updated_at: TIMESTAMP,
          status: "running",
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
          workload_profile: "research",
        },
      ]);
    }
    if (
      path ===
        `${privateWorkBase}/threads/${THREAD_ID}/runs/${RUN_ID}/execution-state` &&
      method === "GET"
    ) {
      return json(route, {
        phase: "executing",
        observed_at: TIMESTAMP,
        phase_started_at: TIMESTAMP,
        execution_started_at: TIMESTAMP,
        retry_at: null,
        run_status: "running",
      });
    }
    if (path === otherThreadBase && method === "GET") {
      return json(route, otherThread);
    }
    if (path === `${otherThreadBase}/state` && method === "GET") {
      return json(route, {
        values: {
          title: otherThread.display_name,
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
    if (path === `${otherThreadBase}/runs` && method === "GET") {
      return json(route, []);
    }
    if (path === `${otherThreadBase}/context-usage` && method === "GET") {
      return json(route, emptyContextProjection(OTHER_THREAD_ID));
    }
    if (
      path === `${otherThreadBase}/context-usage/stream` &&
      method === "GET"
    ) {
      return route.fulfill({ status: 204 });
    }
    if (
      path === `${otherThreadBase}/execution-approvals/active` &&
      method === "GET"
    ) {
      return json(route, {
        schema_version: 1,
        server_time: TIMESTAMP,
        approval: null,
      });
    }
    if (path === `${otherThreadBase}/uploads/limits` && method === "GET") {
      return json(route, {
        max_files: 5,
        max_file_size: 10_000_000,
        max_total_size: 20_000_000,
        project_storage: {
          policy: "project_quota",
          remaining_bytes: 1_000_000_000,
        },
        request_id: "request-other-upload-limits",
      });
    }
    if (path === `${otherThreadBase}/uploads` && method === "GET") {
      return json(route, []);
    }
    return route.fallback();
  });

  return {
    ...base,
    taskDescription,
    returnedStep,
    otherThreadName: otherThread.display_name,
    gateReturnCatalog() {
      gateReturnCatalog = true;
    },
    releaseReturnCatalog() {
      returnCatalogGate.resolve();
    },
  };
}

test("never flashes a failed Sub-Agent card after switching conversations", async ({
  page,
}) => {
  const fixture = await mockActiveSubtaskReconnect(page);
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  await expect(
    page.getByText(fixture.taskDescription, { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/Running subtask|子任务运行中/u)).toBeVisible();

  await page.evaluate(() => {
    Reflect.set(window, "__subtaskFailureSeen", false);
    const observer = new MutationObserver(() => {
      if (/Subtask failed|子任务失败/u.test(document.body.innerText)) {
        Reflect.set(window, "__subtaskFailureSeen", true);
      }
    });
    observer.observe(document.body, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    Reflect.set(window, "__subtaskFailureObserver", observer);
  });

  const rail = page.getByTestId("project-conversation-rail");
  // The row's blank center must navigate just like its title.
  await rail
    .locator("li")
    .filter({ has: page.getByText(fixture.otherThreadName, { exact: true }) })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`/projects/alpha/chats/${OTHER_THREAD_ID}$`, "u"),
  );

  fixture.gateReturnCatalog();
  // Row padding must also remain part of the conversation link.
  await rail
    .locator("li")
    .filter({ has: page.getByText(thread.display_name, { exact: true }) })
    .click({ position: { x: 2, y: 2 } });
  await expect(page).toHaveURL(
    new RegExp(`/projects/alpha/chats/${THREAD_ID}$`, "u"),
  );
  await expect(
    page.getByText(fixture.taskDescription, { exact: true }),
  ).toBeVisible();
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      }),
  );
  await expect(page.getByText(/Subtask failed|子任务失败/u)).toHaveCount(0);
  await expect(
    page.getByText(/Subtask status pending|子任务状态待确认/u),
  ).toBeVisible();

  fixture.releaseReturnCatalog();
  await expect(page.getByText(/Running subtask|子任务运行中/u)).toBeVisible();
  await page.getByText(fixture.taskDescription, { exact: true }).click();
  await expect(
    page.getByText(fixture.returnedStep, { exact: true }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => Reflect.get(window, "__subtaskFailureSeen")),
    )
    .toBe(false);
  expect(fixture.unexpectedRequests).toEqual([]);
});

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

  await expect.poll(requests.executionStateGetCount).toBeGreaterThan(0);
  await expect(composer).toBeEnabled();
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

test("auto-opens each execution-created file in preview after a prior source view", async ({
  page,
}) => {
  const firstPrompt = "Create the first Markdown report.";
  const secondPrompt = "Create the second Markdown report.";
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    liveValuesMessages: [
      {
        id: "human-first-preview",
        type: "human",
        content: firstPrompt,
        additional_kwargs: { run_id: RUN_ID },
      },
      {
        id: "ai-first-preview",
        type: "ai",
        content: "",
        additional_kwargs: { run_id: RUN_ID },
        tool_calls: [
          {
            id: "write-first-preview",
            name: "write_file",
            args: {
              path: "/mnt/data/outputs/first-report.md",
              content: "# First execution preview",
            },
          },
        ],
      },
    ],
    continuationValuesMessages: [
      {
        id: "human-second-preview",
        type: "human",
        content: secondPrompt,
        additional_kwargs: { run_id: SECOND_RUN_ID },
      },
      {
        id: "ai-second-preview",
        type: "ai",
        content: "",
        additional_kwargs: { run_id: SECOND_RUN_ID },
        tool_calls: [
          {
            id: "write-second-preview",
            name: "write_file",
            args: {
              path: "/mnt/data/outputs/second-report.md",
              content: "# Second execution preview",
            },
          },
        ],
      },
    ],
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill(firstPrompt);
  await composer.press("Enter");
  await expect.poll(requests.runPostCount).toBe(1);

  const artifactsPanel = page.locator("#artifacts");
  await expect(
    artifactsPanel.getByRole("heading", { name: "First execution preview" }),
  ).toBeVisible();

  const viewToggles = artifactsPanel.locator('[data-slot="toggle-group-item"]');
  await viewToggles.nth(0).click();
  await expect(viewToggles.nth(0)).toHaveAttribute("data-state", "on");

  await expect.poll(requests.executionStateGetCount).toBeGreaterThan(0);
  await expect(composer).toBeEnabled();
  await composer.fill(secondPrompt);
  await composer.press("Enter");
  await expect.poll(requests.runPostCount).toBe(2);
  await expect(
    artifactsPanel.getByRole("heading", { name: "Second execution preview" }),
  ).toBeVisible();
  await expect(viewToggles.nth(1)).toHaveAttribute("data-state", "on");
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

test("keeps the terminal handoff at the bottom without replaying a smooth scroll", async ({
  page,
}) => {
  const finalMarker = "TERMINAL_HANDOFF_FINAL_MARKER";
  const canonicalOnlyMarker = "TERMINAL_HANDOFF_CANONICAL_ONLY_MARKER";
  const longFinalAnswer = [
    finalMarker,
    ...Array.from(
      { length: 70 },
      (_, index) =>
        `Terminal handoff paragraph ${index + 1}. This keeps the conversation taller than the viewport.`,
    ),
  ].join("\n\n");
  const canonicalFinalAnswer = [
    longFinalAnswer,
    canonicalOnlyMarker,
    ...Array.from(
      { length: 24 },
      (_, index) =>
        `Canonical-only terminal paragraph ${index + 1}. This creates a deterministic positive resize after the gate opens.`,
    ),
  ].join("\n\n");
  const liveValuesMessages = [
    {
      id: "human-terminal-handoff",
      type: "human",
      content: "Keep the completed conversation visually stable.",
      additional_kwargs: { run_id: RUN_ID },
    },
    {
      id: "ai-terminal-present-files",
      type: "ai",
      content: "Publishing the terminal report.",
      additional_kwargs: { run_id: RUN_ID },
      tool_calls: [
        {
          id: "terminal-present-report",
          name: "present_files",
          args: { filepaths: ["outputs/terminal-report.md"] },
        },
      ],
    },
    {
      id: "tool-terminal-present-files",
      type: "tool",
      name: "present_files",
      tool_call_id: "terminal-present-report",
      content: "ok",
      additional_kwargs: { run_id: RUN_ID },
    },
    {
      id: "ai-terminal-final",
      type: "ai",
      content: longFinalAnswer,
      additional_kwargs: { run_id: RUN_ID },
    },
  ] satisfies readonly Record<string, unknown>[];
  const canonicalValuesMessages = liveValuesMessages.map((message) =>
    message.id === "ai-terminal-final"
      ? { ...message, content: canonicalFinalAnswer }
      : message,
  );
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    gateTerminalHandoff: true,
    liveValuesMessages,
    canonicalValuesMessages,
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill("Keep the completed conversation visually stable.");
  await composer.press("Enter");
  await expect(page.getByText(finalMarker, { exact: true })).toBeVisible();
  await expect.poll(requests.executionStateGetCount).toBe(1);

  const messageList = page.getByTestId("main-message-list");
  await messageList.evaluate((root) => {
    const scroller = root.firstElementChild;
    if (!(scroller instanceof HTMLElement)) {
      throw new Error("The main conversation scroller is unavailable.");
    }
    scroller.scrollTop = scroller.scrollHeight;
  });
  await expect
    .poll(() =>
      messageList.evaluate((root) => {
        const scroller = root.firstElementChild;
        if (!(scroller instanceof HTMLElement)) return null;
        return {
          bottomDistance:
            scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop,
          scrollTop: scroller.scrollTop,
        };
      }),
    )
    .toMatchObject({ bottomDistance: 0 });

  await page.evaluate(
    ({ answer, canonicalMarker }) => {
      const root = document.querySelector('[data-testid="main-message-list"]');
      const scroller = root?.firstElementChild;
      const turn = Array.from(
        document.querySelectorAll('[data-assistant-turn=""]'),
      ).find((element) => element.textContent?.includes(answer));
      if (
        !(root instanceof HTMLElement) ||
        !(scroller instanceof HTMLElement) ||
        !(turn instanceof HTMLElement)
      ) {
        throw new Error(
          "The terminal handoff probe could not find its DOM seam.",
        );
      }

      const scrollSamples: Array<{
        bottomDistance: number;
        canonicalCommitted: boolean;
        scrollTop: number;
      }> = [];
      let removedVisibleTurn = false;
      let observedEmptyConversation = false;
      let scrollAnimationFrame = 0;
      const sampleScroll = () => {
        scrollAnimationFrame = 0;
        scrollSamples.push({
          bottomDistance:
            scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop,
          canonicalCommitted: Boolean(
            document.body.textContent?.includes(canonicalMarker),
          ),
          scrollTop: scroller.scrollTop,
        });
      };
      const onScroll = () => {
        if (scrollAnimationFrame === 0) {
          scrollAnimationFrame = requestAnimationFrame(sampleScroll);
        }
      };
      const stopScrollProbe = () => {
        if (scrollAnimationFrame !== 0) {
          cancelAnimationFrame(scrollAnimationFrame);
        }
        scroller.removeEventListener("scroll", onScroll);
      };
      scroller.addEventListener("scroll", onScroll, { passive: true });
      const observer = new MutationObserver((records) => {
        for (const record of records) {
          for (const removedNode of record.removedNodes) {
            if (
              removedNode === turn ||
              (removedNode instanceof Element && removedNode.contains(turn))
            ) {
              removedVisibleTurn = true;
            }
          }
        }
        if (
          document.querySelectorAll('[data-assistant-turn=""]').length === 0
        ) {
          observedEmptyConversation = true;
        }
      });
      observer.observe(root, { childList: true, subtree: true });
      Reflect.set(window, "__terminalHandoffProbe", {
        initialScrollHeight: scroller.scrollHeight,
        initialScrollTop: scroller.scrollTop,
        observer,
        onScroll,
        stopScrollProbe,
        removedVisibleTurn: () => removedVisibleTurn,
        root,
        scrollSamples,
        scroller,
        turn,
        observedEmptyConversation: () => observedEmptyConversation,
      });
    },
    { answer: finalMarker, canonicalMarker: canonicalOnlyMarker },
  );

  let handoff: {
    bottomDistance: number;
    initialScrollHeight: number;
    initialScrollTop: number;
    observedEmptyConversation: boolean;
    removedVisibleTurn: boolean;
    sameRoot: boolean;
    sameScroller: boolean;
    sameTurn: boolean;
    scrollHeight: number;
    scrollSamples: Array<{
      bottomDistance: number;
      canonicalCommitted: boolean;
      scrollTop: number;
    }>;
  } | null = null;
  try {
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
        }),
    );
    requests.releaseTerminalExecutionState();
    await expect.poll(requests.canonicalHistoryGetCount).toBeGreaterThan(0);
    await expect(
      page.getByText(canonicalOnlyMarker, { exact: true }),
    ).toHaveCount(0);

    requests.releaseCanonicalHistory();
    await expect(
      page.getByText(canonicalOnlyMarker, { exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId("assistant-delivered-files")).toHaveCount(1);
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
        }),
    );
    await expect
      .poll(() =>
        page.evaluate(() => {
          const probe = Reflect.get(window, "__terminalHandoffProbe") as {
            scrollSamples: Array<{ canonicalCommitted: boolean }>;
          };
          return probe.scrollSamples.some(
            (sample) => sample.canonicalCommitted,
          );
        }),
      )
      .toBe(true);

    handoff = await page.evaluate((answer) => {
      const probe = Reflect.get(window, "__terminalHandoffProbe") as {
        initialScrollHeight: number;
        initialScrollTop: number;
        observedEmptyConversation: () => boolean;
        removedVisibleTurn: () => boolean;
        root: HTMLElement;
        scrollSamples: Array<{
          bottomDistance: number;
          canonicalCommitted: boolean;
          scrollTop: number;
        }>;
        scroller: HTMLElement;
        turn: HTMLElement;
      };
      const currentRoot = document.querySelector(
        '[data-testid="main-message-list"]',
      );
      const currentScroller = currentRoot?.firstElementChild;
      const currentTurn = Array.from(
        document.querySelectorAll('[data-assistant-turn=""]'),
      ).find((element) => element.textContent?.includes(answer));
      return {
        bottomDistance:
          probe.scroller.scrollHeight -
          probe.scroller.clientHeight -
          probe.scroller.scrollTop,
        initialScrollHeight: probe.initialScrollHeight,
        initialScrollTop: probe.initialScrollTop,
        observedEmptyConversation: probe.observedEmptyConversation(),
        removedVisibleTurn: probe.removedVisibleTurn(),
        sameRoot: currentRoot === probe.root,
        sameScroller: currentScroller === probe.scroller,
        sameTurn: currentTurn === probe.turn,
        scrollHeight: probe.scroller.scrollHeight,
        scrollSamples: [...probe.scrollSamples],
      };
    }, finalMarker);
  } finally {
    requests.releaseCanonicalHistory();
    await page.evaluate(() => {
      const probe = Reflect.get(window, "__terminalHandoffProbe") as
        | {
            observer: MutationObserver;
            stopScrollProbe: () => void;
          }
        | undefined;
      probe?.observer.disconnect();
      probe?.stopScrollProbe();
    });
  }

  expect(handoff).not.toBeNull();
  if (handoff === null) {
    throw new Error("The bottom terminal handoff was not observed.");
  }
  expect(handoff).toMatchObject({
    observedEmptyConversation: false,
    removedVisibleTurn: false,
    sameRoot: true,
    sameScroller: true,
    sameTurn: true,
  });
  expect(handoff.initialScrollTop).toBeGreaterThan(0);
  expect(handoff.scrollHeight).toBeGreaterThan(handoff.initialScrollHeight);
  expect(handoff.scrollSamples.length).toBeGreaterThan(0);
  expect(
    handoff.scrollSamples.some((sample) => sample.canonicalCommitted),
  ).toBe(true);
  expect(
    handoff.scrollSamples.filter((sample) => sample.bottomDistance > 2),
  ).toEqual([]);
  expect(handoff.bottomDistance).toBeLessThanOrEqual(2);
  expect(requests.unexpectedRequests).toEqual([]);
});

test("preserves the reading position when the user scrolls up before terminal handoff", async ({
  page,
}) => {
  const finalMarker = "TERMINAL_HANDOFF_SCROLLED_UP_MARKER";
  const canonicalOnlyMarker =
    "TERMINAL_HANDOFF_SCROLLED_UP_CANONICAL_ONLY_MARKER";
  const longFinalAnswer = [
    finalMarker,
    ...Array.from(
      { length: 70 },
      (_, index) =>
        `Scrolled-up paragraph ${index + 1}. This keeps the active Run taller than the viewport.`,
    ),
  ].join("\n\n");
  const canonicalFinalAnswer = [
    longFinalAnswer,
    canonicalOnlyMarker,
    ...Array.from(
      { length: 24 },
      (_, index) =>
        `Scrolled-up canonical paragraph ${index + 1}. This grows content below the stable reading anchor.`,
    ),
  ].join("\n\n");
  const liveValuesMessages = [
    {
      id: "human-terminal-scrolled-up",
      type: "human",
      content: "Keep my reading position when this Run finishes.",
      additional_kwargs: { run_id: RUN_ID },
    },
    {
      id: "ai-terminal-scrolled-up",
      type: "ai",
      content: longFinalAnswer,
      additional_kwargs: { run_id: RUN_ID },
    },
  ] satisfies readonly Record<string, unknown>[];
  const canonicalValuesMessages = liveValuesMessages.map((message) =>
    message.id === "ai-terminal-scrolled-up"
      ? { ...message, content: canonicalFinalAnswer }
      : message,
  );
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    gateTerminalHandoff: true,
    liveValuesMessages,
    canonicalValuesMessages,
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill("Keep my reading position when this Run finishes.");
  await composer.press("Enter");
  await expect(page.getByText(finalMarker, { exact: true })).toBeVisible();
  await expect.poll(requests.executionStateGetCount).toBe(1);

  const messageList = page.getByTestId("main-message-list");
  await messageList.hover();
  await page.mouse.wheel(0, -600);
  await expect
    .poll(() =>
      messageList.evaluate((root) => {
        const scroller = root.firstElementChild;
        if (!(scroller instanceof HTMLElement)) return 0;
        return (
          scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop
        );
      }),
    )
    .toBeGreaterThan(200);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      }),
  );

  await page.evaluate((canonicalMarker) => {
    const root = document.querySelector('[data-testid="main-message-list"]');
    const scroller = root?.firstElementChild;
    if (!(scroller instanceof HTMLElement)) {
      throw new Error("The scrolled-up handoff probe could not find its seam.");
    }
    const scrollerBounds = scroller.getBoundingClientRect();
    const viewportCenter =
      scrollerBounds.top +
      Math.min(scrollerBounds.height, window.innerHeight) / 2;
    const anchor = Array.from(scroller.querySelectorAll("p"))
      .filter((element) => {
        const bounds = element.getBoundingClientRect();
        return (
          bounds.bottom > scrollerBounds.top &&
          bounds.top < scrollerBounds.bottom
        );
      })
      .sort(
        (left, right) =>
          Math.abs(left.getBoundingClientRect().top - viewportCenter) -
          Math.abs(right.getBoundingClientRect().top - viewportCenter),
      )[0];
    if (!anchor?.textContent) {
      throw new Error(
        "The scrolled-up handoff probe could not find an anchor.",
      );
    }

    const anchorText = anchor.textContent;
    const anchorTop = anchor.getBoundingClientRect().top;
    const samples: Array<{
      anchorOffset: number;
      bottomDistance: number;
      canonicalCommitted: boolean;
      scrollTop: number;
    }> = [];
    let anchorMissing = false;
    let animationFrame = 0;
    const sample = () => {
      const currentAnchor = Array.from(scroller.querySelectorAll("p")).find(
        (element) => element.textContent === anchorText,
      );
      if (!currentAnchor) {
        anchorMissing = true;
        return;
      }
      samples.push({
        anchorOffset: currentAnchor.getBoundingClientRect().top - anchorTop,
        bottomDistance:
          scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop,
        canonicalCommitted: Boolean(
          document.body.textContent?.includes(canonicalMarker),
        ),
        scrollTop: scroller.scrollTop,
      });
    };
    const onScroll = () => sample();
    const onAnimationFrame = () => {
      sample();
      animationFrame = requestAnimationFrame(onAnimationFrame);
    };
    const stop = () => {
      cancelAnimationFrame(animationFrame);
      scroller.removeEventListener("scroll", onScroll);
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    sample();
    animationFrame = requestAnimationFrame(onAnimationFrame);
    Reflect.set(window, "__terminalScrolledUpProbe", {
      anchorMissing: () => anchorMissing,
      initialBottomDistance:
        scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop,
      initialScrollHeight: scroller.scrollHeight,
      sample,
      samples,
      scroller,
      stop,
    });
  }, canonicalOnlyMarker);

  let handoff: {
    anchorMissing: boolean;
    bottomDistance: number;
    initialBottomDistance: number;
    initialScrollHeight: number;
    samples: Array<{
      anchorOffset: number;
      bottomDistance: number;
      canonicalCommitted: boolean;
      scrollTop: number;
    }>;
    scrollHeight: number;
  } | null = null;
  try {
    requests.releaseTerminalExecutionState();
    await expect.poll(requests.canonicalHistoryGetCount).toBeGreaterThan(0);
    await expect(
      page.getByText(canonicalOnlyMarker, { exact: true }),
    ).toHaveCount(0);

    requests.releaseCanonicalHistory();
    await expect(
      page.getByText(canonicalOnlyMarker, { exact: true }),
    ).toBeVisible();
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
        }),
    );
    handoff = await page.evaluate(() => {
      const probe = Reflect.get(window, "__terminalScrolledUpProbe") as {
        anchorMissing: () => boolean;
        initialBottomDistance: number;
        initialScrollHeight: number;
        sample: () => void;
        samples: Array<{
          anchorOffset: number;
          bottomDistance: number;
          canonicalCommitted: boolean;
          scrollTop: number;
        }>;
        scroller: HTMLElement;
      };
      probe.sample();
      return {
        anchorMissing: probe.anchorMissing(),
        bottomDistance:
          probe.scroller.scrollHeight -
          probe.scroller.clientHeight -
          probe.scroller.scrollTop,
        initialBottomDistance: probe.initialBottomDistance,
        initialScrollHeight: probe.initialScrollHeight,
        samples: [...probe.samples],
        scrollHeight: probe.scroller.scrollHeight,
      };
    });
  } finally {
    requests.releaseCanonicalHistory();
    await page.evaluate(() => {
      const probe = Reflect.get(window, "__terminalScrolledUpProbe") as
        | { stop: () => void }
        | undefined;
      probe?.stop();
    });
  }

  expect(handoff).not.toBeNull();
  if (handoff === null) {
    throw new Error("The scrolled-up terminal handoff was not observed.");
  }
  expect(handoff.anchorMissing).toBe(false);
  expect(handoff.initialBottomDistance).toBeGreaterThan(200);
  expect(handoff.bottomDistance).toBeGreaterThan(200);
  expect(handoff.scrollHeight).toBeGreaterThan(handoff.initialScrollHeight);
  expect(handoff.samples.length).toBeGreaterThan(0);
  expect(handoff.samples.some((sample) => sample.canonicalCommitted)).toBe(
    true,
  );
  expect(
    handoff.samples.filter((sample) => Math.abs(sample.anchorOffset) > 2),
  ).toEqual([]);
  expect(
    handoff.samples.filter((sample) => sample.bottomDistance <= 2),
  ).toEqual([]);
  expect(requests.unexpectedRequests).toEqual([]);
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
  await expect(composer).toHaveValue("Research Agent history.");
  await expect(composer).toBeEnabled();

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
  ).toHaveCount(0);
  await expect(
    progress.locator('[data-reason-code="tool_budget_exhausted"]'),
  ).toHaveCount(1);
  await expect(
    progress.locator('[data-reason-code="subagent_total_limit"]'),
  ).toHaveCount(1);
  await expect(page.getByTestId("run-failure-alert")).toHaveCount(0);

  await page.reload();
  const replayedProgress = page.getByTestId("run-control-progress");
  await expect(
    replayedProgress.locator('[data-reason-code="repeated_call_warning"]'),
  ).toHaveCount(0);
  await expect(
    replayedProgress.locator('[data-reason-code="tool_budget_exhausted"]'),
  ).toHaveCount(1);
  await expect(
    replayedProgress.locator('[data-reason-code="subagent_total_limit"]'),
  ).toHaveCount(1);
  expect(requests.unexpectedRequests).toEqual([]);
});

test("renders knowledge citations under the final answer and restores them after refresh", async ({
  page,
}) => {
  const finalAnswer = "KNOWLEDGE_CITED_FINAL_ANSWER";
  const knowledgeCitations = [
    {
      knowledge_base_id: "40000000-0000-4000-8000-000000000021",
      knowledge_base_name: "产品手册",
      document_id: "50000000-0000-4000-8000-000000000021",
      document_name: "发布说明.pdf",
      segment_id: "60000000-0000-4000-8000-000000000021",
      segment_position: 7,
      snippet: "发布前需要完成回归测试与变更评审。",
      score: 0.93,
      source_position: { page: 7 },
    },
    {
      knowledge_base_id: "40000000-0000-4000-8000-000000000021",
      knowledge_base_name: "产品手册",
      document_id: "50000000-0000-4000-8000-000000000022",
      document_name: "运维守则.md",
      segment_id: "60000000-0000-4000-8000-000000000022",
      segment_position: 2,
      snippet: "发布窗口固定在每周四凌晨。",
      score: 0.41,
      source_position: { row: 12 },
    },
  ] as const;
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    liveValuesMessages: [
      {
        id: "human-knowledge-citations",
        type: "human",
        content: "发布流程有哪些步骤？",
        additional_kwargs: { run_id: RUN_ID },
      },
      {
        id: "ai-knowledge-search",
        type: "ai",
        content: "",
        additional_kwargs: { run_id: RUN_ID },
        tool_calls: [
          {
            id: "call-knowledge-search",
            name: "knowledge_search",
            args: { query: "发布流程" },
          },
        ],
      },
      {
        id: "tool-knowledge-search",
        type: "tool",
        name: "knowledge_search",
        tool_call_id: "call-knowledge-search",
        content:
          "[1] 产品手册 / 发布说明.pdf\n发布前需要完成回归测试与变更评审。",
        additional_kwargs: {
          run_id: RUN_ID,
          knowledge_citations: knowledgeCitations,
        },
      },
      {
        id: "ai-knowledge-final",
        type: "ai",
        content: finalAnswer,
        additional_kwargs: { run_id: RUN_ID },
      },
    ],
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill("发布流程有哪些步骤？");
  await composer.press("Enter");
  await expect.poll(requests.runPostCount).toBe(1);
  await expect(page.getByText(finalAnswer, { exact: true })).toBeVisible();

  // The projection attaches the ToolMessage's citations to the Run's final AI
  // text message, so the panel must mount inside that same assistant turn.
  const finalTurn = page
    .locator('[data-assistant-turn=""]')
    .filter({ hasText: finalAnswer });
  const panel = finalTurn.getByTestId("knowledge-citations-panel");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("Cited 2 knowledge sources");
  await panel.locator("summary").click();
  await expect(panel.getByRole("listitem")).toHaveCount(2);
  const firstCitation = panel.getByRole("listitem").first();
  await expect(firstCitation).toContainText("发布说明.pdf");
  await expect(firstCitation).toContainText("Retrieval score 0.930");
  await expect(firstCitation).toContainText("Segment #7");
  await expect(firstCitation).toContainText("Page 7");
  await expect(panel.getByRole("listitem").nth(1)).toContainText(
    "运维守则.md",
  );

  // After a refresh the live stream is gone; the durable Run messages must
  // restore the same citations from additional_kwargs.
  await page.reload();
  await expect(page.getByText(finalAnswer, { exact: true })).toBeVisible();
  const replayedPanel = page.getByTestId("knowledge-citations-panel");
  await expect(replayedPanel).toBeVisible();
  await expect(replayedPanel).toContainText("Cited 2 knowledge sources");
  await replayedPanel.locator("summary").click();
  await expect(replayedPanel.getByRole("listitem")).toHaveCount(2);
  await expect(replayedPanel.getByRole("listitem").first()).toContainText(
    "发布说明.pdf",
  );
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

test("preserves the graph step limit and partial results after refresh without replay", async ({
  page,
}) => {
  const partialAnswer =
    "The first draft is ready, but visual review is incomplete.";
  const requests = await mockProjectChat(page, {
    filename: FILE_NAME,
    mediaType: "text/plain",
    size: Buffer.byteLength(FILE_CONTENT),
    firstRunError: "GRAPH_RECURSION_LIMIT",
    liveValuesMessages: [
      {
        id: "human-graph-step-limit",
        type: "human",
        content: "Create and review a draft.",
        additional_kwargs: { run_id: RUN_ID },
      },
      {
        id: "ai-graph-step-limit-partial",
        type: "ai",
        content: partialAnswer,
        additional_kwargs: { run_id: RUN_ID },
      },
    ],
  });
  await page.goto(`/projects/alpha/chats/${THREAD_ID}`);

  const composer = page.getByPlaceholder(/how can i assist you today/i);
  await composer.fill("Create and review a draft.");
  await composer.press("Enter");
  await expect.poll(requests.runPostCount).toBe(1);

  const assertStoppedWithoutReplay = async () => {
    const failure = page.getByTestId("run-failure-alert");
    await expect(failure).toHaveAttribute(
      "data-run-failure-code",
      "GRAPH_RECURSION_LIMIT",
    );
    await expect(failure).toContainText("Graph execution step limit reached");
    await expect(failure).toContainText("The Agent has stopped");
    await expect(failure).toContainText("do not resend this message directly");
    await expect(failure).not.toContainText("Worker could not confirm");
    await expect(page.getByText(partialAnswer, { exact: true })).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: /Restore to composer|Retry without deep thinking|Regenerate/u,
      }),
    ).toHaveCount(0);
  };

  await assertStoppedWithoutReplay();
  await page.reload();
  await assertStoppedWithoutReplay();
  expect(requests.runPostCount()).toBe(1);
  expect(requests.unexpectedRequests).toEqual([]);
});
