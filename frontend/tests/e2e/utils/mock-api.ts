/**
 * Shared mock helpers for E2E tests.
 *
 * Intercepts all LangGraph / Backend API endpoints so tests can run without
 * a real backend.  Each test file imports `mockLangGraphAPI` and
 * `handleRunStream` from here.
 */

import type { Page, Route } from "@playwright/test";

import type {
  Automation,
  AutomationReadiness,
  AutomationRun,
} from "@/core/project-automations/types";
import type { Project } from "@/core/projects/types";

// ---------------------------------------------------------------------------
// Constants — deterministic IDs used across tests
// ---------------------------------------------------------------------------

export const MOCK_THREAD_ID = "00000000-0000-0000-0000-000000000001";
export const MOCK_THREAD_ID_2 = "00000000-0000-0000-0000-000000000002";
export const MOCK_SIDECAR_THREAD_ID = "00000000-0000-0000-0000-0000000000aa";
export const MOCK_RUN_ID = "00000000-0000-0000-0000-000000000099";

const MOCK_AUTH_USER = {
  id: "default",
  email: "default@test.local",
  system_role: "system_admin",
  needs_setup: false,
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type MockThread = {
  thread_id: string;
  title?: string;
  updated_at?: string;
  agent_name?: string;
  metadata?: Record<string, unknown>;
  messages?: unknown[];
  artifacts?: string[];
  goal?: Record<string, unknown> | null;
};

export type MockAgent = {
  name: string;
  description?: string;
  system_prompt?: string;
};

export type MockSkill = {
  name: string;
  description: string;
  category?: string;
  license?: string | null;
  enabled?: boolean;
};

export type MockAPIOptions = {
  threads?: MockThread[];
  agents?: MockAgent[];
  skills?: MockSkill[];
  skillContents?: Record<string, string>;
  scheduledTasks?: Array<{
    id: string;
    thread_id: string | null;
    context_mode?: "fresh_thread_per_run" | "reuse_thread";
    last_thread_id?: string | null;
    title: string;
    prompt: string;
    schedule_type: "once" | "cron";
    schedule_spec: Record<string, unknown>;
    timezone: string;
    status:
      | "enabled"
      | "paused"
      | "running"
      | "completed"
      | "failed"
      | "cancelled";
    next_run_at: string | null;
    last_run_at: string | null;
    last_run_id: string | null;
    last_error: string | null;
    run_count: number;
    created_at: string;
    updated_at: string;
  }>;
  uploadLimits?: {
    max_files: number;
    max_file_size: number;
    max_total_size: number;
  };
  suggestionsEnabled?: boolean;
  runStreamHandler?: (route: Route) => {
    values: Record<string, unknown>;
  };
};

const DEFAULT_SKILLS: MockSkill[] = [
  {
    name: "data-analysis",
    description: "Analyze structured data and produce charts.",
    category: "public",
    enabled: true,
  },
  {
    name: "frontend-design",
    description: "Create polished frontend interfaces.",
    category: "public",
    enabled: true,
  },
  {
    name: "disabled-skill",
    description: "Hidden from slash autocomplete.",
    category: "public",
    enabled: false,
  },
];

function isHiddenInputMessage(message: unknown) {
  if (typeof message !== "object" || message === null) {
    return false;
  }
  const additionalKwargs = Reflect.get(message, "additional_kwargs");
  return (
    typeof additionalKwargs === "object" &&
    additionalKwargs !== null &&
    Reflect.get(additionalKwargs, "hide_from_ui") === true
  );
}

function visibleInputMessages(messages: unknown[]) {
  return messages.filter((message) => !isHiddenInputMessage(message));
}

function visibleRunInputMessages(route: Route) {
  try {
    const body = route.request().postDataJSON() as {
      input?: { messages?: unknown[] };
    };
    return visibleInputMessages(body.input?.messages ?? []);
  } catch {
    return [];
  }
}

function messageId(message: unknown): string | undefined {
  if (typeof message !== "object" || message === null) {
    return undefined;
  }
  const raw = Reflect.get(message, "id");
  return typeof raw === "string" ? raw : undefined;
}

function branchMessagesFromTurn(messages: unknown[], targetIds: Set<string>) {
  let targetEndIndex = -1;
  for (const [index, message] of messages.entries()) {
    const id = messageId(message);
    if (id && targetIds.has(id)) {
      targetEndIndex = Math.max(targetEndIndex, index);
    }
  }
  return targetEndIndex >= 0 ? messages.slice(0, targetEndIndex + 1) : messages;
}

function mockStreamMessages(route?: Route, inputMessages?: unknown[]) {
  const submittedMessages = inputMessages
    ? visibleInputMessages(inputMessages)
    : route
      ? visibleRunInputMessages(route)
      : [];
  const responseMessage = {
    type: "ai",
    id: "msg-ai-1",
    content: "Hello from DeerFlow!",
  };
  if (submittedMessages.length > 0) {
    return [...submittedMessages, responseMessage];
  }

  return [
    {
      type: "human",
      id: "msg-human-1",
      content: [{ type: "text", text: "Hello" }],
    },
    responseMessage,
  ];
}

function runStreamThreadId(route: Route) {
  const pathThreadId = /\/threads\/([^/]+)\/runs\/stream/.exec(
    new URL(route.request().url()).pathname,
  )?.[1];
  if (pathThreadId) {
    return pathThreadId;
  }

  try {
    const body = route.request().postDataJSON() as {
      thread_id?: string;
      threadId?: string;
      context?: { thread_id?: string };
      config?: { configurable?: { thread_id?: string } };
    };
    return (
      body.thread_id ??
      body.threadId ??
      body.context?.thread_id ??
      body.config?.configurable?.thread_id ??
      MOCK_THREAD_ID
    );
  } catch {
    return MOCK_THREAD_ID;
  }
}

// ---------------------------------------------------------------------------
// mockLangGraphAPI
// ---------------------------------------------------------------------------

/**
 * Mock all LangGraph API endpoints that the frontend calls on page load and
 * during message sending.  Without these mocks the pages would hang waiting
 * for a real backend.
 */
export function mockLangGraphAPI(page: Page, options?: MockAPIOptions) {
  let threads = [...(options?.threads ?? [])];
  const agents = options?.agents ?? [];
  const skills = (options?.skills ?? DEFAULT_SKILLS).map((skill) => ({
    ...skill,
  }));
  const scheduledTasks = options?.scheduledTasks ?? [];
  let mutableScheduledTasks = [...scheduledTasks];
  const mutableTaskRuns: Record<
    string,
    Array<{
      id: string;
      task_id: string;
      thread_id: string | null;
      run_id: string | null;
      scheduled_for: string;
      trigger: "scheduled" | "manual";
      status: "queued" | "running" | "success" | "failed" | "skipped";
      error: string | null;
      started_at: string | null;
      finished_at: string | null;
      created_at: string;
    }>
  > = {};
  const uploadLimits = options?.uploadLimits ?? {
    max_files: 10,
    max_file_size: 50 * 1024 * 1024,
    max_total_size: 100 * 1024 * 1024,
  };

  const upsertThread = (thread: MockThread) => {
    threads = [
      thread,
      ...threads.filter((existing) => existing.thread_id !== thread.thread_id),
    ];
  };

  const applyRunStreamValues = (
    route: Route,
    values: Record<string, unknown>,
  ) => {
    const threadId = runStreamThreadId(route);
    const existingThread = threads.find(
      (thread) => thread.thread_id === threadId,
    );
    const nextThread: MockThread = {
      ...(existingThread ?? {
        thread_id: threadId,
        title: threadId === MOCK_SIDECAR_THREAD_ID ? "Side chat" : "New Chat",
      }),
      thread_id: threadId,
      updated_at: new Date().toISOString(),
    };
    if (Array.isArray(values.messages)) {
      nextThread.messages = values.messages;
    }
    if (Array.isArray(values.artifacts)) {
      nextThread.artifacts = values.artifacts.filter(
        (artifact): artifact is string => typeof artifact === "string",
      );
    }
    if (typeof values.title === "string") {
      nextThread.title = values.title;
    }
    if (
      values.goal === null ||
      (typeof values.goal === "object" && values.goal !== null)
    ) {
      nextThread.goal = values.goal as Record<string, unknown> | null;
    }
    upsertThread(nextThread);
  };

  const threadSearchResult = (thread: MockThread) => ({
    thread_id: thread.thread_id,
    created_at: "2025-01-01T00:00:00Z",
    updated_at: thread.updated_at ?? "2025-01-01T00:00:00Z",
    metadata: {
      ...(thread.metadata ?? {}),
      ...(thread.agent_name ? { agent_name: thread.agent_name } : {}),
    },
    status: "idle",
    values: { title: thread.title ?? "Untitled", goal: thread.goal ?? null },
  });

  // Auth — keep workspace tests independent from a real gateway session.
  void page.route("**/api/v1/auth/me", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(MOCK_AUTH_USER),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/v1/auth/setup-status", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ needs_setup: false }),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/v1/auth/logout", (route) => {
    if (route.request().method() === "POST") {
      return route.fulfill({ status: 204 });
    }
    return route.fallback();
  });

  // Project-first workspace always asks for the current account's pending
  // invitations. Keep unrelated E2E files isolated from a real gateway; tests
  // that exercise invitation behavior register a more specific override.
  void page.route("**/api/project-invitations/mine", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "[]",
      });
    }
    return route.fallback();
  });

  void page.route("**/api/channels/providers", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: false, providers: [] }),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/channels/connections", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ connections: [] }),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/suggestions/config", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: options?.suggestionsEnabled ?? false }),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/scheduled-tasks", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mutableScheduledTasks.map((task) => ({
            context_mode: "fresh_thread_per_run",
            last_thread_id: null,
            ...task,
            thread_id: task.thread_id ?? null,
          })),
        ),
      });
    }
    if (route.request().method() === "POST") {
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      const threadId =
        typeof payload.thread_id === "string" ? payload.thread_id : "";
      const title = typeof payload.title === "string" ? payload.title : "";
      const prompt = typeof payload.prompt === "string" ? payload.prompt : "";
      const timezone =
        typeof payload.timezone === "string" ? payload.timezone : "UTC";
      const created = {
        id: "task-created",
        thread_id: threadId || null,
        context_mode:
          (payload.context_mode as "fresh_thread_per_run" | "reuse_thread") ??
          "fresh_thread_per_run",
        last_thread_id: null,
        title,
        prompt,
        schedule_type: payload.schedule_type as "once" | "cron",
        schedule_spec: (payload.schedule_spec as Record<string, unknown>) ?? {},
        timezone,
        status: "enabled" as const,
        next_run_at: null,
        last_run_at: null,
        last_run_id: null,
        last_error: null,
        run_count: 0,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      };
      mutableScheduledTasks = [created, ...mutableScheduledTasks];
      mutableTaskRuns[created.id] = [];
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(created),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/scheduled-tasks/*/pause", (route) => {
    if (route.request().method() === "POST") {
      const taskId = decodeURIComponent(
        new URL(route.request().url()).pathname.split("/").at(-2) ?? "",
      );
      mutableScheduledTasks = mutableScheduledTasks.map((task) =>
        task.id === taskId ? { ...task, status: "paused" as const } : task,
      );
      const task = mutableScheduledTasks.find((item) => item.id === taskId);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(task),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/scheduled-tasks/*/resume", (route) => {
    if (route.request().method() === "POST") {
      const taskId = decodeURIComponent(
        new URL(route.request().url()).pathname.split("/").at(-2) ?? "",
      );
      mutableScheduledTasks = mutableScheduledTasks.map((task) =>
        task.id === taskId ? { ...task, status: "enabled" as const } : task,
      );
      const task = mutableScheduledTasks.find((item) => item.id === taskId);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(task),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/scheduled-tasks/*/trigger", (route) => {
    if (route.request().method() === "POST") {
      const taskId = decodeURIComponent(
        new URL(route.request().url()).pathname.split("/").at(-2) ?? "",
      );
      const task = mutableScheduledTasks.find((item) => item.id === taskId);
      if (task) {
        const runId = `run-${taskId}`;
        mutableTaskRuns[taskId] = [
          {
            id: `task-run-${taskId}`,
            task_id: taskId,
            thread_id: task.thread_id,
            run_id: runId,
            scheduled_for: "2026-07-01T00:00:00+00:00",
            trigger: "manual",
            status: "success",
            error: null,
            started_at: "2026-07-01T00:00:00+00:00",
            finished_at: "2026-07-01T00:00:00+00:00",
            created_at: "2026-07-01T00:00:00+00:00",
          },
          ...(mutableTaskRuns[taskId] ?? []),
        ];
        mutableScheduledTasks = mutableScheduledTasks.map((item) =>
          item.id === taskId
            ? {
                ...item,
                last_run_id: runId,
                last_run_at: "2026-07-01T00:00:00+00:00",
                run_count: item.run_count + 1,
              }
            : item,
        );
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: taskId, triggered: true }),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/scheduled-tasks/*", (route) => {
    const request = route.request();
    if (request.method() === "PATCH") {
      const taskId = decodeURIComponent(
        new URL(request.url()).pathname.split("/").at(-1) ?? "",
      );
      const payload = request.postDataJSON() as Record<string, unknown>;
      let updated: (typeof mutableScheduledTasks)[number] | undefined;
      mutableScheduledTasks = mutableScheduledTasks.map((task) => {
        if (task.id !== taskId) {
          return task;
        }
        updated = {
          ...task,
          ...(typeof payload.title === "string"
            ? { title: payload.title }
            : {}),
          ...(typeof payload.prompt === "string"
            ? { prompt: payload.prompt }
            : {}),
          ...(payload.schedule_spec
            ? {
                schedule_spec: payload.schedule_spec as Record<string, unknown>,
              }
            : {}),
          ...(typeof payload.timezone === "string"
            ? { timezone: payload.timezone }
            : {}),
          updated_at: "2026-07-01T00:00:00+00:00",
        };
        return updated;
      });
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(updated ?? {}),
      });
    }
    if (request.method() === "DELETE") {
      const taskId = decodeURIComponent(
        new URL(request.url()).pathname.split("/").at(-1) ?? "",
      );
      mutableScheduledTasks = mutableScheduledTasks.filter(
        (task) => task.id !== taskId,
      );
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: taskId, deleted: true }),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/threads/*/scheduled-tasks", (route) => {
    if (route.request().method() === "GET") {
      const url = new URL(route.request().url());
      const parts = url.pathname.split("/");
      const threadId = decodeURIComponent(
        parts[parts.indexOf("threads") + 1] ?? "",
      );
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mutableScheduledTasks
            .filter((task) => task.thread_id === threadId)
            .map((task) => ({
              context_mode: "fresh_thread_per_run",
              last_thread_id: null,
              ...task,
              thread_id: task.thread_id ?? null,
            })),
        ),
      });
    }
    return route.fallback();
  });

  void page.route("**/api/scheduled-tasks/*/runs", (route) => {
    if (route.request().method() === "GET") {
      const taskId = decodeURIComponent(
        new URL(route.request().url()).pathname.split("/").at(-2) ?? "",
      );
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mutableTaskRuns[taskId] ?? []),
      });
    }
    return route.fallback();
  });

  // Thread search — sidebar thread list & chats list page
  void page.route("**/api/langgraph/threads/search", async (route) => {
    let body = threads.map(threadSearchResult);

    let limit: number | undefined;
    let offset = 0;
    try {
      const postData = route.request().postDataJSON() as {
        limit?: number;
        offset?: number;
        metadata?: Record<string, unknown>;
      } | null;
      if (postData) {
        if (typeof postData.limit === "number") {
          limit = postData.limit;
        }
        if (typeof postData.offset === "number") {
          offset = postData.offset;
        }
        if (postData.metadata && typeof postData.metadata === "object") {
          body = body.filter((thread) =>
            Object.entries(postData.metadata ?? {}).every(
              ([key, value]) => thread.metadata?.[key] === value,
            ),
          );
        }
      }
    } catch {
      // No / invalid JSON body — fall back to returning the full list.
    }

    const sliced =
      typeof limit === "number" ? body.slice(offset, offset + limit) : body;

    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(sliced),
    });
  });

  // Thread create — called when user sends first message in a new chat
  void page.route("**/api/langgraph/threads", (route) => {
    if (route.request().method() === "POST") {
      upsertThread({
        thread_id: MOCK_THREAD_ID,
        title: "New Chat",
        updated_at: new Date().toISOString(),
        messages: mockStreamMessages(),
      });
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          thread_id: MOCK_THREAD_ID,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          metadata: {},
          status: "idle",
          values: {},
        }),
      });
    }
    return route.fallback();
  });

  // Thread update (PATCH) — metadata update after creation
  void page.route("**/api/langgraph/threads/*", (route) => {
    const threadId = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").at(-1) ?? "",
    );
    const matchingThread = threads.find(
      (thread) => thread.thread_id === threadId,
    );
    if (route.request().method() === "GET") {
      if (!matchingThread) {
        return route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Thread not found" }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(threadSearchResult(matchingThread)),
      });
    }
    if (route.request().method() === "PATCH") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ thread_id: MOCK_THREAD_ID }),
      });
    }
    if (route.request().method() === "DELETE") {
      threads = threads.filter((thread) => thread.thread_id !== threadId);
      return route.fulfill({
        status: 204,
      });
    }
    return route.fallback();
  });

  void page.route("**/api/threads", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as {
        thread_id?: string;
        metadata?: Record<string, unknown>;
      };
      const threadId = body.thread_id ?? MOCK_SIDECAR_THREAD_ID;
      upsertThread({
        thread_id: threadId,
        title: "Side chat",
        updated_at: new Date().toISOString(),
        metadata: body.metadata ?? {},
        messages: [],
      });
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          thread_id: threadId,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          metadata: body.metadata ?? {},
          status: "idle",
          values: {},
        }),
      });
    }
    return route.fallback();
  });

  void page.route(/\/api\/threads\/[^/]+$/, (route) => {
    if (route.request().method() === "DELETE") {
      const threadId = decodeURIComponent(
        new URL(route.request().url()).pathname.split("/").at(-1) ?? "",
      );
      // Mirror the gateway's `require_existing=True` ownership guard: deleting
      // an already-removed thread 404s. `useDeleteThread` first deletes via the
      // LangGraph route (which drops the thread_meta row) and then hits this
      // route, so this reproduces the real double-delete 404 the frontend must
      // treat as idempotent success.
      if (!threads.some((thread) => thread.thread_id === threadId)) {
        return route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: `Thread ${threadId} not found` }),
        });
      }
      threads = threads.filter((thread) => thread.thread_id !== threadId);
      return route.fulfill({
        status: 204,
      });
    }
    return route.fallback();
  });

  void page.route(/\/api\/threads\/[^/]+\/branches$/, (route) => {
    if (route.request().method() === "POST") {
      const pathParts = new URL(route.request().url()).pathname.split("/");
      const sourceThreadId = decodeURIComponent(pathParts.at(-2) ?? "");
      const sourceThread = threads.find(
        (thread) => thread.thread_id === sourceThreadId,
      );
      const body = route.request().postDataJSON() as {
        message_id?: string;
        message_ids?: string[];
        title?: string;
      };
      const targetIds = new Set(
        [body.message_id, ...(body.message_ids ?? [])].filter(
          (id): id is string => typeof id === "string" && id.length > 0,
        ),
      );
      let sourceTitle = sourceThread?.title?.trim();
      if (sourceThread?.metadata?.deerflow_branch === true) {
        sourceTitle = sourceTitle?.replace(/^(Branch:\s*)+/i, "").trim();
      }
      const title = body.title ?? sourceTitle;

      upsertThread({
        thread_id: MOCK_THREAD_ID_2,
        title,
        updated_at: new Date().toISOString(),
        metadata: {
          deerflow_branch: true,
          branch_parent_thread_id: sourceThreadId,
          branch_parent_message_id: body.message_id,
          branch_parent_checkpoint_id: "mock-checkpoint",
        },
        messages: branchMessagesFromTurn(
          sourceThread?.messages ?? [],
          targetIds,
        ),
      });

      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          thread_id: MOCK_THREAD_ID_2,
          parent_thread_id: sourceThreadId,
          parent_checkpoint_id: "mock-checkpoint",
          branched_from_message_id: body.message_id,
          workspace_clone_mode: "current_thread_best_effort",
        }),
      });
    }
    return route.fallback();
  });

  void page.route(/\/api\/threads\/[^/]+\/goal$/, async (route) => {
    const threadId = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").at(-2) ?? "",
    );
    let matchingThread = threads.find(
      (thread) => thread.thread_id === threadId,
    );

    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ goal: matchingThread?.goal ?? null }),
      });
    }

    if (route.request().method() === "DELETE") {
      if (matchingThread) {
        matchingThread.goal = null;
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ goal: null }),
      });
    }

    if (route.request().method() === "PUT") {
      const payload = route.request().postDataJSON() as {
        objective?: string;
      };
      const goal = {
        objective: payload.objective ?? "",
        status: "active",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        continuation_count: 0,
        max_continuations: 8,
        no_progress_count: 0,
        max_no_progress_continuations: 2,
      };
      matchingThread ??= {
        thread_id: threadId,
        title: "New Chat",
        updated_at: new Date().toISOString(),
      };
      upsertThread({ ...matchingThread, goal });
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ goal }),
      });
    }

    return route.fallback();
  });

  void page.route("**/api/threads/*/uploads/limits", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(uploadLimits),
      });
    }
    return route.fallback();
  });

  // Thread history — useStream fetches state history on mount
  void page.route("**/api/langgraph/threads/*/history", (route) => {
    const url = route.request().url();

    // For threads that exist in our mock data, return history with messages
    const matchingThread = threads.find((t) => url.includes(t.thread_id));
    if (matchingThread) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            values: {
              title: matchingThread.title ?? "Untitled",
              goal: matchingThread.goal ?? null,
              messages: matchingThread.messages ?? [
                {
                  type: "human",
                  id: `msg-human-${matchingThread.thread_id}`,
                  content: [{ type: "text", text: "Previous question" }],
                },
                {
                  type: "ai",
                  id: `msg-ai-${matchingThread.thread_id}`,
                  content: `Response in thread ${matchingThread.title ?? matchingThread.thread_id}`,
                },
              ],
              artifacts: matchingThread.artifacts ?? [],
            },
            next: [],
            metadata: {},
            created_at: "2025-01-01T00:00:00Z",
            parent_config: null,
          },
        ]),
      });
    }

    // New threads — empty history
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "[]",
    });
  });

  // Thread state — getState for individual thread
  void page.route("**/api/langgraph/threads/*/state", (route) => {
    if (route.request().method() === "GET") {
      const url = route.request().url();
      const matchingThread = threads.find((t) => url.includes(t.thread_id));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          values: {
            title: matchingThread?.title ?? "Untitled",
            goal: matchingThread?.goal ?? null,
            messages: matchingThread
              ? (matchingThread.messages ?? [
                  {
                    type: "human",
                    id: `msg-human-${matchingThread.thread_id}`,
                    content: [{ type: "text", text: "Previous question" }],
                  },
                  {
                    type: "ai",
                    id: `msg-ai-${matchingThread.thread_id}`,
                    content: `Response in thread ${matchingThread.title ?? matchingThread.thread_id}`,
                  },
                ])
              : [],
            artifacts: matchingThread?.artifacts ?? [],
          },
          next: [],
          metadata: {},
          created_at: "2025-01-01T00:00:00Z",
        }),
      });
    }
    return route.fallback();
  });

  // The URL carries a query string (e.g. `?limit=10&offset=0`), which Playwright
  // glob `*` does NOT cross, so we match with a regex anchored to `/runs`
  // followed by `?` or end-of-string.  This must NOT match `/runs/stream`.
  void page.route(/\/api\/langgraph\/threads\/[^/]+\/runs(\?|$)/, (route) => {
    if (route.request().method() === "GET") {
      const url = route.request().url();
      const matchingThread = threads.find((t) => url.includes(t.thread_id));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          matchingThread
            ? [
                {
                  run_id: `run-${matchingThread.thread_id}`,
                  thread_id: matchingThread.thread_id,
                  assistant_id: "lead_agent",
                  status: "success",
                  metadata: {},
                  kwargs: {},
                  created_at: "2025-01-01T00:00:00Z",
                  updated_at:
                    matchingThread.updated_at ?? "2025-01-01T00:00:00Z",
                },
              ]
            : [],
        ),
      });
    }
    return route.fallback();
  });

  void page.route(
    /\/api\/threads\/([^/]+)\/runs\/([^/]+)\/messages/,
    (route) => {
      if (route.request().method() === "GET") {
        const url = route.request().url();
        const matchingThread = threads.find((t) =>
          url.includes(`/api/threads/${t.thread_id}/runs/`),
        );
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            data: (matchingThread?.messages ?? []).map((message, index) => ({
              run_id: `run-${matchingThread?.thread_id ?? "unknown"}`,
              content: message,
              metadata: { caller: "lead_agent" },
              created_at: `2025-01-01T00:00:${String(index).padStart(2, "0")}Z`,
            })),
            hasMore: false,
          }),
        });
      }
      return route.fallback();
    },
  );

  // Run stream — returns a minimal SSE response with an AI message
  const handleMockRunStream = (route: Route) => {
    if (options?.runStreamHandler) {
      const result = options.runStreamHandler(route);
      applyRunStreamValues(route, result.values);
      return fulfillRunStreamValues(route, result.values);
    }
    const threadId = runStreamThreadId(route);
    const existingThread = threads.find(
      (thread) => thread.thread_id === threadId,
    );
    const fallbackGoal = threads.find((thread) => thread.goal)?.goal ?? null;
    const goal = existingThread?.goal ?? fallbackGoal;
    const values = { goal, messages: mockStreamMessages(route) };
    applyRunStreamValues(route, values);
    return fulfillRunStreamValues(route, values);
  };

  void page.route("**/api/langgraph/runs/stream", handleMockRunStream);
  void page.route(
    "**/api/langgraph/threads/*/runs/stream",
    handleMockRunStream,
  );

  // Models list — model picker dropdown
  void page.route("**/api/models", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          models: [],
          token_usage: { enabled: false },
        }),
      });
    }
    return route.fallback();
  });

  // Feature flags — frontend gates UI (e.g. agents) on these. Default to
  // enabled so existing tests exercise the normal path; tests that need the
  // disabled state override this route after calling mockLangGraphAPI.
  void page.route("**/api/features", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ agents_api: { enabled: true } }),
      });
    }
    return route.fallback();
  });

  // Skills list — settings page and slash autocomplete
  void page.route("**/api/skills/**", (route) => {
    const segments = new URL(route.request().url()).pathname
      .split("/")
      .filter(Boolean);
    const isContentRequest = segments.at(-2) === "content";
    const encodedName = segments.at(-1);
    const name = decodeURIComponent(encodedName ?? "");
    const skill = skills.find((candidate) => candidate.name === name);
    if (route.request().method() === "PUT" && skill) {
      const body = route.request().postDataJSON() as { enabled: boolean };
      skill.enabled = body.enabled;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...skill,
          editable: skill.category === "custom",
        }),
      });
    }
    if (route.request().method() !== "GET" || !isContentRequest) {
      return route.fallback();
    }
    const content = options?.skillContents?.[name];
    if (!skill || content === undefined) {
      return route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Skill content unavailable" }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ content }),
    });
  });

  void page.route("**/api/skills", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ skills }),
      });
    }
    return route.fallback();
  });

  // Follow-up suggestions — input box auto-suggest after AI response
  void page.route("**/api/threads/*/suggestions", (route) => {
    if (route.request().method() === "POST") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ suggestions: [] }),
      });
    }
    return route.fallback();
  });

  // Agents list — sidebar & gallery page
  void page.route("**/api/agents", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ agents }),
      });
    }
    return route.fallback();
  });

  // Individual agent — agent chat page
  void page.route("**/api/agents/*", (route) => {
    if (route.request().method() === "GET") {
      const url = route.request().url();
      const agent = agents.find((a) => url.endsWith(`/api/agents/${a.name}`));
      if (agent) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(agent),
        });
      }
    }
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Agent not found" }),
    });
  });
}

// ---------------------------------------------------------------------------
// handleRunStream
// ---------------------------------------------------------------------------

/**
 * Build a minimal SSE stream that the LangGraph SDK can parse.
 * The stream returns a single AI message: "Hello from DeerFlow!".
 */
export function handleRunStream(
  route: Route,
  values: Record<string, unknown> = {},
  inputMessages?: unknown[],
) {
  return fulfillRunStreamValues(route, {
    ...values,
    messages: mockStreamMessages(route, inputMessages),
  });
}

function fulfillRunStreamValues(route: Route, values: Record<string, unknown>) {
  const threadId = runStreamThreadId(route);
  const events = [
    {
      event: "metadata",
      data: { run_id: MOCK_RUN_ID, thread_id: threadId },
    },
    {
      event: "values",
      data: values,
    },
    { event: "end", data: {} },
  ];

  const body = events
    .map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`)
    .join("");

  return route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body,
  });
}

// ---------------------------------------------------------------------------
// Project Automation mock
// ---------------------------------------------------------------------------

export type MockProjectAutomationAccount = {
  id: string;
  email: string;
  projects: Project[];
  automations?: Record<string, Automation[]>;
};

export type MockProjectAutomationRequest = {
  accountId: string;
  projectId: string;
  method: string;
  path: string;
  headers: Record<string, string>;
  body: Record<string, unknown> | null;
};

type MockAutomationAction =
  | "create"
  | "update"
  | "pause"
  | "resume"
  | "trigger"
  | "delete";

type MockAutomationFailure = {
  status: number;
  code:
    | "AUTOMATION_VERSION_CONFLICT"
    | "AUTOMATION_CONCURRENCY_LIMIT"
    | "AUTOMATION_UNAVAILABLE";
};

type DeferredAutomationList = {
  started: Promise<void>;
  release: () => void;
};

export type MockProjectAutomationState = {
  requests: MockProjectAutomationRequest[];
  requestedPaths: string[];
  abortedListScopes: Set<string>;
  switchAccount: (accountId: string) => void;
  getAutomations: (accountId: string, projectId: string) => Automation[];
  getRuns: (
    accountId: string,
    projectId: string,
    automationId: string,
  ) => AutomationRun[];
  setReadiness: (
    accountId: string,
    projectId: string,
    readiness: Partial<AutomationReadiness>,
  ) => void;
  failNext: (
    accountId: string,
    projectId: string,
    action: MockAutomationAction,
    failure: MockAutomationFailure,
  ) => void;
  holdNextList: (
    accountId: string,
    projectId: string,
  ) => DeferredAutomationList;
};

export function automationStoreKey(
  accountId: string,
  projectId: string,
): string {
  return `${accountId}:${projectId}`;
}

function automationRunStoreKey(scopeKey: string, automationId: string) {
  return `${scopeKey}:${automationId}`;
}

function automationJson(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function automationRequestBody(route: Route): Record<string, unknown> | null {
  try {
    return route.request().postDataJSON() as Record<string, unknown>;
  } catch {
    return null;
  }
}

/**
 * Installs the release-gate Automation fixture. Automation state is always
 * selected by the authenticated account plus the project UUID parsed from the
 * project URL. There is deliberately no global task map or legacy endpoint
 * fallback.
 */
export async function mockProjectAutomationAPI(
  page: Page,
  accounts: MockProjectAutomationAccount[],
): Promise<MockProjectAutomationState> {
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
  const accountsById = new Map(
    accounts.map((account) => [account.id, account]),
  );
  let currentAccountId = accounts[0]?.id ?? "";
  const stores = new Map<string, Automation[]>();
  const runs = new Map<string, AutomationRun[]>();
  const readiness = new Map<string, AutomationReadiness>();
  const failures = new Map<string, MockAutomationFailure[]>();
  const heldLists = new Map<
    string,
    { started: () => void; released: Promise<void> }
  >();
  const requests: MockProjectAutomationRequest[] = [];
  const requestedPaths: string[] = [];
  const abortedListScopes = new Set<string>();

  for (const account of accounts) {
    for (const currentProject of account.projects) {
      const key = automationStoreKey(account.id, currentProject.id);
      stores.set(key, [...(account.automations?.[currentProject.id] ?? [])]);
      readiness.set(key, {
        status: "ready",
        code: "AUTOMATION_READY",
        scheduler_enabled: true,
        scheduler_status: "running",
        project_private_work_ready: true,
        schema_ready: true,
        request_id: `automation-ready-${account.id}-${currentProject.id}`,
      });
    }
  }

  page.on("request", (request) => {
    requestedPaths.push(new URL(request.url()).pathname);
  });
  page.on("requestfailed", (request) => {
    const path = new URL(request.url()).pathname;
    const match = /\/api\/projects\/([^/]+)\/automations$/.exec(path);
    if (match?.[1]) {
      const record = [...requests]
        .reverse()
        .find(
          (candidate) =>
            candidate.projectId === match[1] &&
            candidate.method === "GET" &&
            candidate.path === path,
        );
      abortedListScopes.add(
        automationStoreKey(record?.accountId ?? currentAccountId, match[1]),
      );
    }
  });

  const state: MockProjectAutomationState = {
    requests,
    requestedPaths,
    abortedListScopes,
    switchAccount(accountId) {
      if (!accountsById.has(accountId)) throw new Error("Unknown mock account");
      currentAccountId = accountId;
    },
    getAutomations(accountId, projectId) {
      return stores.get(automationStoreKey(accountId, projectId)) ?? [];
    },
    getRuns(accountId, projectId, automationId) {
      const scope = automationStoreKey(accountId, projectId);
      return runs.get(automationRunStoreKey(scope, automationId)) ?? [];
    },
    setReadiness(accountId, projectId, value) {
      const key = automationStoreKey(accountId, projectId);
      readiness.set(key, { ...readiness.get(key)!, ...value });
    },
    failNext(accountId, projectId, action, failure) {
      const key = `${automationStoreKey(accountId, projectId)}:${action}`;
      failures.set(key, [...(failures.get(key) ?? []), failure]);
    },
    holdNextList(accountId, projectId) {
      const key = automationStoreKey(accountId, projectId);
      let markStarted!: () => void;
      let release!: () => void;
      const started = new Promise<void>((resolve) => {
        markStarted = resolve;
      });
      const released = new Promise<void>((resolve) => {
        release = resolve;
      });
      heldLists.set(key, { started: markStarted, released });
      return { started, release };
    },
  };

  const takeFailure = (scopeKey: string, action: MockAutomationAction) => {
    const key = `${scopeKey}:${action}`;
    const queue = failures.get(key) ?? [];
    const next = queue.shift();
    if (queue.length === 0) failures.delete(key);
    return next;
  };
  const fail = (route: Route, failure: MockAutomationFailure) =>
    automationJson(
      route,
      {
        detail: {
          code: failure.code,
          message: "private mock detail",
          request_id: `mock-${failure.code.toLowerCase()}`,
        },
      },
      failure.status,
    );

  await page.route("**/api/v1/auth/me", (route) => {
    const account = accountsById.get(currentAccountId);
    if (!account) return automationJson(route, { detail: "not found" }, 404);
    return automationJson(route, {
      id: account.id,
      email: account.email,
      system_role: "user",
      needs_setup: false,
    });
  });
  await page.route(/\/api\/projects(?:\?.*)?$/, (route) => {
    const account = accountsById.get(currentAccountId);
    return automationJson(route, {
      items: account?.projects ?? [],
      next_cursor: null,
    });
  });
  await page.route("**/api/projects/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const projectId = /^\/api\/projects\/([^/]+)/.exec(path)?.[1];
    const account = accountsById.get(currentAccountId);
    const currentProject = account?.projects.find(({ id }) => id === projectId);
    if (!projectId || !currentProject) {
      return automationJson(route, { detail: "not found" }, 404);
    }
    const accountId = currentAccountId;
    const scopeKey = automationStoreKey(accountId, projectId);
    const scopedAutomations = stores.get(scopeKey)!;
    const base = `/api/projects/${projectId}/automations`;

    if (path.endsWith("/enter")) {
      return automationJson(route, currentProject);
    }
    if (path.endsWith("/private-work/readiness")) {
      return automationJson(route, {
        status: "ready",
        code: "PRIVATE_WORK_READY",
        request_id: `private-ready-${scopeKey}`,
      });
    }
    if (path.endsWith("/agents") && method === "GET") {
      return automationJson(route, {
        system_items: [],
        project_items: [
          {
            id: "20000000-0000-4000-8000-000000000001",
            scope: "project",
            project_id: projectId,
            slug: "automation-agent",
            display_name: "Automation Agent",
            status: "active",
            current_published_version_id:
              "30000000-0000-4000-8000-000000000001",
            version: 1,
            created_by_user_id: accountId,
            created_at: "2026-07-16T00:00:00Z",
            updated_at: "2026-07-16T00:00:00Z",
            capabilities: ["shared_assets.read", "shared_assets.execute"],
            binding: null,
          },
        ],
        request_id: `agents-${scopeKey}`,
      });
    }

    if (!path.startsWith(base)) {
      return automationJson(route, { detail: "not found" }, 404);
    }
    if (!currentProject.capabilities.includes("private_work.read_own")) {
      return automationJson(route, { detail: "forbidden" }, 403);
    }
    const record: MockProjectAutomationRequest = {
      accountId,
      projectId,
      method,
      path,
      headers: request.headers(),
      body: automationRequestBody(route),
    };
    requests.push(record);

    if (path === `${base}/readiness` && method === "GET") {
      return automationJson(route, readiness.get(scopeKey));
    }
    if (path === base && method === "GET") {
      const held = heldLists.get(scopeKey);
      if (held) {
        heldLists.delete(scopeKey);
        held.started();
        await held.released;
      }
      try {
        return await automationJson(route, { items: scopedAutomations });
      } catch {
        abortedListScopes.add(scopeKey);
        return;
      }
    }
    if (path === base && method === "POST") {
      const failure = takeFailure(scopeKey, "create");
      if (failure) return fail(route, failure);
      const body = record.body ?? {};
      const now = "2026-07-16T00:00:00Z";
      const created: Automation = {
        id: `automation-${scopedAutomations.length + 1}`,
        thread_id: (body.thread_id as string | null | undefined) ?? null,
        context_mode: body.context_mode as Automation["context_mode"],
        agent_asset_id: body.agent_asset_id as string,
        agent_scope: body.agent_scope as Automation["agent_scope"],
        title: body.title as string,
        prompt: body.prompt as string,
        schedule_type: body.schedule_type as Automation["schedule_type"],
        schedule_spec: body.schedule_spec as Record<string, unknown>,
        timezone: body.timezone as string,
        status: "enabled",
        next_run_at: "2026-07-17T09:00:00Z",
        last_run_at: null,
        last_outcome: null,
        last_error_code: null,
        run_count: 0,
        version: 1,
        created_at: now,
        updated_at: now,
      };
      scopedAutomations.push(created);
      return automationJson(route, created, 201);
    }

    const suffix = path.slice(base.length + 1);
    const [automationId, action] = suffix.split("/");
    const selectedIndex = scopedAutomations.findIndex(
      ({ id }) => id === automationId,
    );
    if (selectedIndex < 0 || !automationId) {
      return automationJson(route, { detail: "not found" }, 404);
    }
    const selected = scopedAutomations[selectedIndex]!;
    if (action === "runs" && method === "GET") {
      return automationJson(route, {
        items: runs.get(automationRunStoreKey(scopeKey, automationId)) ?? [],
      });
    }
    if (action === "trigger" && method === "POST") {
      const failure = takeFailure(scopeKey, "trigger");
      if (failure) return fail(route, failure);
      const now = "2026-07-16T00:00:00Z";
      const run: AutomationRun = {
        id: `run-${automationId}-${state.getRuns(accountId, projectId, automationId).length + 1}`,
        automation_id: automationId,
        automation_version: selected.version,
        scheduled_for: now,
        trigger: "manual",
        status: "queued",
        thread_id: null,
        run_id: null,
        error_code: null,
        started_at: null,
        finished_at: null,
        created_at: now,
        updated_at: now,
      };
      const runKey = automationRunStoreKey(scopeKey, automationId);
      runs.set(runKey, [...(runs.get(runKey) ?? []), run]);
      return automationJson(route, run);
    }
    if ((action === "pause" || action === "resume") && method === "POST") {
      const failure = takeFailure(scopeKey, action);
      if (failure) return fail(route, failure);
      const updated: Automation = {
        ...selected,
        status: action === "pause" ? "paused" : "enabled",
        version: selected.version + 1,
      };
      scopedAutomations[selectedIndex] = updated;
      return automationJson(route, updated);
    }
    if (action === undefined && method === "PATCH") {
      const failure = takeFailure(scopeKey, "update");
      if (failure) return fail(route, failure);
      const body = record.body ?? {};
      const changes = { ...body };
      delete changes.expected_version;
      const updated = {
        ...selected,
        ...changes,
        version: selected.version + 1,
      } as Automation;
      scopedAutomations[selectedIndex] = updated;
      return automationJson(route, updated);
    }
    if (action === undefined && method === "DELETE") {
      const failure = takeFailure(scopeKey, "delete");
      if (failure) return fail(route, failure);
      scopedAutomations.splice(selectedIndex, 1);
      return automationJson(route, { id: automationId, deleted: true });
    }
    if (action === undefined && method === "GET") {
      return automationJson(route, selected);
    }
    return automationJson(route, { detail: "not found" }, 404);
  });

  return state;
}
