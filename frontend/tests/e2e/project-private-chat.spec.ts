import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";

import type { Project } from "@/core/projects/types";

import { handleRunStream, mockLangGraphAPI } from "./utils/mock-api";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const THREAD_ID = "20000000-0000-4000-8000-000000000001";
const SECOND_THREAD_ID = "20000000-0000-4000-8000-000000000002";
const MISSING_THREAD_ID = "20000000-0000-4000-8000-000000000099";
const AGENT_ID = "30000000-0000-4000-8000-000000000001";
const MAIN_AGENT_ID = "30000000-0000-4000-8000-000000000002";
const PROJECT_FILE_ID = "40000000-0000-4000-8000-000000000001";
const PROJECT_SKILL_FILE_ID = "40000000-0000-4000-8000-000000000003";
const PROJECT_SKILL_ID = "50000000-0000-4000-8000-000000000001";
const SIDECAR_THREAD_ID = "60000000-0000-4000-8000-000000000001";
const SECOND_SIDECAR_THREAD_ID = "60000000-0000-4000-8000-000000000002";
const WRITE_ARTIFACT_PATH = "/mnt/user-data/outputs/project-report.md";
const PRESENTED_ARTIFACT_PATH = "/mnt/user-data/outputs/presented-report.md";
const PRESENTED_SKILL_PATH = "/mnt/user-data/outputs/reviewer.skill";

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
  quota_summary: {
    members: { used: 2, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
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
  created_at: "2026-07-15T00:00:00Z",
  updated_at: "2026-07-15T01:00:00Z",
};

const projectArtifactMessages = [
  {
    type: "human",
    id: "msg-artifact-request",
    content: [{ type: "text", text: "Create project files" }],
  },
  {
    type: "ai",
    id: "msg-artifact-write",
    content: "",
    tool_calls: [
      {
        id: "write-project-file",
        name: "write_file",
        args: {
          description: "Writing project report",
          path: WRITE_ARTIFACT_PATH,
          content: "# Project report",
        },
      },
    ],
  },
  {
    type: "tool",
    id: "msg-artifact-result",
    name: "write_file",
    tool_call_id: "write-project-file",
    content: "OK",
  },
  {
    type: "ai",
    id: "msg-artifact-present",
    content: "The report is ready.",
    tool_calls: [
      {
        id: "present-project-file",
        name: "present_files",
        args: { filepaths: [PRESENTED_ARTIFACT_PATH] },
      },
    ],
  },
];

type MockPrivateWorkOptions = {
  metadataStatus?: number;
  stateMessages?: unknown[];
  stateMessagesAfterCompact?: unknown[];
  stateMessagesAfterStream?: unknown[];
  initialStateGate?: Promise<void>;
  initialStateGateRequests?: string[];
  historyRunMessages?: unknown[];
  /** Ordered newest-first, matching the private Run list contract. */
  historyRuns?: Array<{
    runId: string;
    messages: unknown[];
    createdAt: string;
  }>;
  stateArtifacts?: string[];
  artifactFileStatus?: number;
  runBodies?: unknown[];
  streamGate?: Promise<void>;
  streamValueSequence?: Array<Record<string, unknown>>;
  uploadRequests?: string[];
  searchThreads?: (typeof privateThread)[];
  streamValues?: Record<string, unknown>;
  workspaceChanges?: unknown;
  uploadedFiles?: Array<Record<string, unknown>>;
  uploadedFilesAfterStream?: Array<Record<string, unknown>>;
  controlBodies?: Array<{ path: string; body: unknown }>;
  goalGate?: ReturnType<typeof deferredGate>;
  compactGate?: ReturnType<typeof deferredGate>;
  streamTerminalStatus?: "error" | "failed" | "timeout";
  streamResponseStatus?: number;
  stateStatusAfterStream?: number;
  stateStatusAfterStreamRequests?: string[];
  failurePartialMessages?: unknown[];
  failureMessageIds?: Partial<
    Record<"submitted" | "live" | "admission", string>
  >;
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function captureReasoningEvidence(
  page: Page,
  filename: string,
  target?: Locator,
) {
  const directory = process.env.CAPTURE_REASONING_EVIDENCE_DIR?.trim();
  if (!directory) {
    return;
  }
  const path = `${directory}/${filename}.png`;
  if (target) {
    await target.screenshot({ path, animations: "disabled" });
  } else {
    await page.screenshot({
      path,
      fullPage: false,
      animations: "disabled",
    });
  }
}

function latestVisibleSubmittedHumanMessage(
  messages: unknown[],
): Record<string, unknown> | null {
  for (const value of [...messages].reverse()) {
    if (typeof value !== "object" || value === null) continue;
    const type = Reflect.get(value, "type");
    const role = Reflect.get(value, "role");
    if (type !== "human" && role !== "user") continue;
    const additionalKwargs = Reflect.get(value, "additional_kwargs");
    if (
      typeof additionalKwargs === "object" &&
      additionalKwargs !== null &&
      Reflect.get(additionalKwargs, "hide_from_ui") === true
    ) {
      continue;
    }
    const messageId = Reflect.get(value, "id");
    return {
      type: "human",
      ...(typeof messageId === "string" && messageId ? { id: messageId } : {}),
      content: Reflect.get(value, "content"),
      ...(typeof additionalKwargs === "object" && additionalKwargs !== null
        ? { additional_kwargs: additionalKwargs }
        : {}),
    };
  }
  return null;
}

async function mockProjectContext(page: Page, currentProject = project) {
  await page.route("**/api/v1/auth/me", (route) =>
    json(route, {
      id: ACCOUNT_ID,
      email: "runner@example.test",
      system_role: "user",
      needs_setup: false,
      oauth_provider: null,
    }),
  );
  await page.route(/\/api\/projects(?:\?.*)?$/, (route) =>
    json(route, { items: [currentProject], next_cursor: null }),
  );
  await page.route(`**/api/projects/${PROJECT_ID}/enter`, (route) =>
    json(route, { ...currentProject, request_id: "req-enter" }),
  );
}

async function mockPrivateWork(
  page: Page,
  includeThread = true,
  options: MockPrivateWorkOptions = {},
) {
  const requests: string[] = [];
  let threadExists = includeThread;
  let hasStreamed = false;
  let hasCompacted = false;
  let failedSubmittedMessage: Record<string, unknown> | null = null;
  let goal: Record<string, unknown> | null = null;
  const historyRuns =
    options.historyRuns ??
    (options.historyRunMessages
      ? [
          {
            runId: "run-history",
            messages: options.historyRunMessages,
            createdAt: "2026-07-15T02:00:00Z",
          },
        ]
      : []);
  const initialStateMessages = options.stateMessages ?? [
    {
      type: "human",
      id: "msg-project-history",
      content: [{ type: "text", text: "Previous project question" }],
    },
  ];
  await page.route(
    `**/api/projects/${PROJECT_ID}/private-work/**`,
    async (route) => {
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
        const body = request.postDataJSON() as {
          offset?: number;
          limit?: number;
        };
        const allThreads =
          options.searchThreads ?? (threadExists ? [privateThread] : []);
        const offset = body.offset ?? 0;
        const limit = body.limit ?? 50;
        return json(route, {
          items: allThreads.slice(offset, offset + limit),
        });
      }
      if (path.endsWith(`/threads/${THREAD_ID}/state`)) {
        if (!threadExists) return json(route, { detail: "not found" }, 404);
        if (!hasStreamed && options.initialStateGate) {
          options.initialStateGateRequests?.push(`${request.method()} ${path}`);
          await options.initialStateGate;
        }
        if (hasStreamed && options.stateStatusAfterStream) {
          options.stateStatusAfterStreamRequests?.push(
            `${request.method()} ${path}`,
          );
          return json(
            route,
            { detail: "post-stream state temporarily unavailable" },
            options.stateStatusAfterStream,
          );
        }
        return json(route, {
          values: {
            title: "Owner research",
            messages: [
              ...(hasCompacted && options.stateMessagesAfterCompact
                ? options.stateMessagesAfterCompact
                : initialStateMessages),
              ...(hasStreamed && !options.streamTerminalStatus
                ? (options.stateMessagesAfterStream ?? [
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
                  ])
                : []),
            ],
            artifacts: options.stateArtifacts ?? [],
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
      if (path.endsWith(`/threads/${THREAD_ID}/goal`)) {
        if (request.method() === "GET") {
          return json(route, { goal });
        }
        if (request.method() === "DELETE") {
          goal = null;
          return json(route, { goal });
        }
        const body = request.postDataJSON() as { objective: string };
        options.controlBodies?.push({ path, body });
        if (options.goalGate) {
          options.goalGate.markStarted();
          await options.goalGate.promise;
        }
        goal = {
          objective: body.objective,
          status: "active",
          created_at: "2026-07-15T01:00:00Z",
          updated_at: "2026-07-15T01:00:00Z",
          continuation_count: 0,
          max_continuations: 8,
          no_progress_count: 0,
          max_no_progress_continuations: 2,
        };
        return json(route, { goal });
      }
      if (path.endsWith(`/threads/${THREAD_ID}/compact`)) {
        const body = request.postDataJSON();
        options.controlBodies?.push({ path, body });
        if (options.compactGate) {
          options.compactGate.markStarted();
          await options.compactGate.promise;
        }
        hasCompacted = true;
        return json(route, {
          thread_id: THREAD_ID,
          compacted: true,
          removed_message_count: 4,
          preserved_message_count: 2,
          summary_updated: true,
          checkpoint_id: "checkpoint-compact",
          total_tokens: 120,
        });
      }
      if (path.endsWith(`/threads/${THREAD_ID}/branches`)) {
        const body = request.postDataJSON() as { message_id: string };
        options.controlBodies?.push({ path, body });
        return json(route, {
          thread_id: SECOND_THREAD_ID,
          parent_thread_id: THREAD_ID,
          parent_checkpoint_id: "checkpoint-branch",
          branched_from_message_id: body.message_id,
          workspace_clone_mode: "current_thread_authority_copy",
        });
      }
      if (path.endsWith(`/threads/${THREAD_ID}/runs/edit-regenerate/prepare`)) {
        const body = request.postDataJSON() as {
          human_message_id: string;
          replacement_text: string;
        };
        options.controlBodies?.push({ path, body });
        const replacementMessage = {
          type: "human",
          id: "msg-project-edited",
          content: [{ type: "text", text: body.replacement_text }],
          additional_kwargs: {},
        };
        return json(route, {
          input: { messages: [replacementMessage] },
          checkpoint: {
            checkpoint_ns: "",
            checkpoint_id: "checkpoint-before-human",
            checkpoint_map: null,
          },
          metadata: {
            replay_kind: "edit",
            regenerate_from_message_id: "msg-edit-ai",
            regenerate_from_run_id: "run-original",
            regenerate_checkpoint_id: "checkpoint-before-human",
            edit_from_message_id: body.human_message_id,
            edit_message_id: replacementMessage.id,
            edit_version_group_id: body.human_message_id,
          },
          target_run_id: "run-original",
          replacement_human_message_id: replacementMessage.id,
          source_message_ids: [body.human_message_id, "msg-edit-ai"],
        });
      }
      if (path.endsWith(`/threads/${THREAD_ID}/runs/regenerate/prepare`)) {
        const body = request.postDataJSON() as { message_id: string };
        options.controlBodies?.push({ path, body });
        return json(route, {
          input: {
            messages: [
              {
                type: "human",
                id: "msg-project-submitted",
                content: [{ type: "text", text: "Hello from project" }],
                additional_kwargs: {},
              },
            ],
          },
          checkpoint: {
            checkpoint_ns: "",
            checkpoint_id: "checkpoint-before-human",
            checkpoint_map: null,
          },
          metadata: {
            regenerate_from_message_id: body.message_id,
            regenerate_from_run_id: "run-original",
            regenerate_checkpoint_id: "checkpoint-before-human",
          },
          target_run_id: "run-original",
        });
      }
      if (path.endsWith(`/threads/${THREAD_ID}/suggestions`)) {
        const body = request.postDataJSON();
        options.controlBodies?.push({ path, body });
        return json(route, {
          suggestions: ["Review the project result?"],
        });
      }
      if (path.endsWith(`/threads/${THREAD_ID}/uploads/limits`)) {
        return json(route, {
          max_files: 10,
          max_file_size: 50 * 1024 * 1024,
          max_total_size: 100 * 1024 * 1024,
          project_storage: {
            policy: "project_quota",
            remaining_bytes: 5 * 1024 * 1024 * 1024,
          },
          request_id: "project-private-upload-limits",
        });
      }
      if (
        request.method() === "POST" &&
        path.endsWith(`/threads/${THREAD_ID}/uploads`)
      ) {
        options.uploadRequests?.push(`${request.method()} ${path}`);
        return json(route, {
          id: "40000000-0000-4000-8000-000000000002",
          logical_path: "uploads/release.txt",
          display_name: "release.txt",
          kind: "upload",
          media_type: "text/plain",
          size: 15,
          sha256: "release-upload-sha",
          status: "ready",
          created_at: "2026-07-15T00:00:00Z",
          updated_at: "2026-07-15T00:00:00Z",
        });
      }
      if (
        request.method() === "GET" &&
        path.endsWith(`/threads/${THREAD_ID}/uploads`)
      ) {
        return json(
          route,
          (hasStreamed
            ? options.uploadedFilesAfterStream
            : options.uploadedFiles) ??
            options.uploadedFiles ?? [
              {
                id: PROJECT_FILE_ID,
                logical_path: "outputs/presented-report.md",
                display_name: "presented-report.md",
                kind: "output",
                media_type: "text/markdown",
                size: 26,
                sha256: "project-file-sha",
                status: "ready",
                created_at: "2026-07-15T00:00:00Z",
                updated_at: "2026-07-15T00:00:00Z",
              },
            ],
        );
      }
      if (path.endsWith(`/threads/${THREAD_ID}/files/${PROJECT_FILE_ID}`)) {
        if (options.artifactFileStatus) {
          return json(
            route,
            { detail: "private artifact storage failure" },
            options.artifactFileStatus,
          );
        }
        return route.fulfill({
          status: 200,
          contentType: "text/markdown",
          body: "# Presented project report",
        });
      }
      if (path.endsWith(`/threads/${THREAD_ID}`)) {
        if (request.method() === "DELETE") {
          threadExists = false;
          return route.fulfill({ status: 204 });
        }
        if (!threadExists) return json(route, { detail: "not found" }, 404);
        if (options.metadataStatus && options.metadataStatus !== 200) {
          return json(
            route,
            { detail: "temporarily unavailable" },
            options.metadataStatus,
          );
        }
        return json(route, privateThread);
      }
      if (path.endsWith(`/threads/${THREAD_ID}/runs/stream`)) {
        const body = request.postDataJSON() as {
          input?: { messages?: unknown[] };
        };
        options.runBodies?.push(body);
        await options.streamGate;
        if (options.streamResponseStatus) {
          return json(
            route,
            { detail: "run admission temporarily unavailable" },
            options.streamResponseStatus,
          );
        }
        hasStreamed = true;
        if (options.streamTerminalStatus) {
          failedSubmittedMessage = latestVisibleSubmittedHumanMessage(
            body.input?.messages ?? [],
          );
          const submittedMessageId = failedSubmittedMessage
            ? Reflect.get(failedSubmittedMessage, "id")
            : null;
          if (
            options.failureMessageIds &&
            typeof submittedMessageId === "string" &&
            submittedMessageId
          ) {
            options.failureMessageIds.submitted = submittedMessageId;
          }
          const liveFailedMessage = failedSubmittedMessage
            ? {
                ...failedSubmittedMessage,
                id:
                  typeof submittedMessageId === "string" && submittedMessageId
                    ? submittedMessageId
                    : "msg-project-failed",
              }
            : {
                type: "human",
                id: "msg-project-failed",
                content: [{ type: "text", text: "Trigger project failure" }],
              };
          if (options.failureMessageIds) {
            options.failureMessageIds.live = String(
              Reflect.get(liveFailedMessage, "id"),
            );
          }
          return route.fulfill({
            status: 200,
            contentType: "text/event-stream",
            headers: {
              "Content-Location": `/threads/${THREAD_ID}/runs/run-failed`,
            },
            body: [
              "event: metadata",
              `data: ${JSON.stringify({ run_id: "run-failed", thread_id: THREAD_ID })}`,
              "id: 1",
              "",
              "event: values",
              `data: ${JSON.stringify({
                title: "Owner research",
                messages: [
                  ...initialStateMessages,
                  liveFailedMessage,
                  ...(options.failurePartialMessages ?? []),
                ],
                artifacts: [],
                todos: [],
              })}`,
              "id: 2",
              "",
              "event: end",
              `data: ${JSON.stringify({ status: options.streamTerminalStatus })}`,
              "id: 3",
              "",
              "",
            ].join("\n"),
          });
        }
        if (options.streamValueSequence) {
          return route.fulfill({
            status: 200,
            contentType: "text/event-stream",
            headers: {
              "Content-Location": `/threads/${THREAD_ID}/runs/run-artifact`,
            },
            body: [
              "event: metadata",
              `data: ${JSON.stringify({ run_id: "run-artifact", thread_id: THREAD_ID })}`,
              "",
              ...options.streamValueSequence.flatMap((values) => [
                "event: values",
                `data: ${JSON.stringify(values)}`,
                "",
              ]),
              "event: end",
              "data: {}",
              "",
              "",
            ].join("\n"),
          });
        }
        return handleRunStream(route, options.streamValues, undefined, {
          "Content-Location": `/threads/${THREAD_ID}/runs/00000000-0000-0000-0000-000000000099`,
        });
      }
      if (path.endsWith(`/threads/${THREAD_ID}/runs/run-retained/events`)) {
        return json(route, []);
      }
      if (
        path.endsWith(
          `/threads/${THREAD_ID}/runs/run-retained/workspace-changes`,
        )
      ) {
        return json(
          route,
          options.workspaceChanges ?? { detail: "not found" },
          options.workspaceChanges ? 200 : 404,
        );
      }
      if (path.endsWith(`/threads/${THREAD_ID}/runs/run-failed/messages`)) {
        const submittedMessageId = failedSubmittedMessage
          ? Reflect.get(failedSubmittedMessage, "id")
          : null;
        const admittedFailedMessage = failedSubmittedMessage
          ? {
              ...failedSubmittedMessage,
              id:
                typeof submittedMessageId === "string" && submittedMessageId
                  ? submittedMessageId
                  : "run-admission-run-failed",
            }
          : null;
        if (options.failureMessageIds && admittedFailedMessage) {
          options.failureMessageIds.admission = String(
            Reflect.get(admittedFailedMessage, "id"),
          );
        }
        return json(route, {
          data: admittedFailedMessage
            ? [
                {
                  run_id: "run-failed",
                  seq: "0",
                  content: admittedFailedMessage,
                  metadata: { source: "run_admission" },
                  created_at: "2026-07-15T03:00:00Z",
                },
              ]
            : [],
          has_more: false,
        });
      }
      const historyRun = historyRuns.find((run) =>
        path.endsWith(`/threads/${THREAD_ID}/runs/${run.runId}/messages`),
      );
      if (historyRun) {
        const createdAt = new Date(historyRun.createdAt).getTime();
        return json(route, {
          data: historyRun.messages.map((content, index) => ({
            run_id: historyRun.runId,
            seq: String(index + 1),
            content,
            metadata: {
              caller: "lead_agent",
              ...(index === 0 ? { source: "run_admission" } : {}),
            },
            created_at: new Date(createdAt + index * 1_000).toISOString(),
          })),
          has_more: false,
        });
      }
      if (/\/threads\/[^/]+\/runs(?:\?|$)/u.test(request.url())) {
        return json(
          route,
          hasStreamed && options.streamTerminalStatus
            ? [
                {
                  run_id: "run-failed",
                  thread_id: THREAD_ID,
                  assistant_id: null,
                  status: options.streamTerminalStatus,
                  metadata: {},
                  multitask_strategy: "reject",
                  error: "AGENT_EXECUTION_FAILED",
                  model_name: null,
                  created_at: "2026-07-15T03:00:00Z",
                  updated_at: "2026-07-15T03:00:01Z",
                },
              ]
            : historyRuns.length > 0
              ? historyRuns.map((run) => ({
                  run_id: run.runId,
                  thread_id: THREAD_ID,
                  assistant_id: null,
                  status: "success",
                  metadata: {},
                  multitask_strategy: "reject",
                  error: null,
                  model_name: "test-model",
                  created_at: run.createdAt,
                  updated_at: new Date(
                    new Date(run.createdAt).getTime() + 1_000,
                  ).toISOString(),
                }))
              : [],
        );
      }
      return json(route, { detail: "not found" }, 404);
    },
  );
  return requests;
}

type ProjectSidecarMockOptions = {
  existing?: boolean;
  messages?: unknown[];
  deleteGate?: Promise<void>;
};

async function mockProjectSidecar(
  page: Page,
  options: ProjectSidecarMockOptions = {},
) {
  let sidecarThreadId = options.existing ? SIDECAR_THREAD_ID : null;
  let searchVisible = options.existing ?? false;
  let messages = [...(options.messages ?? [])];
  const requests: string[] = [];
  const createBodies: Array<Record<string, unknown>> = [];
  const runBodies: unknown[] = [];
  const globalRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      path === "/api/threads" ||
      path.startsWith("/api/threads/") ||
      path.startsWith("/api/langgraph/threads") ||
      path.startsWith("/api/langgraph/runs")
    ) {
      globalRequests.push(`${request.method()} ${path}`);
    }
  });

  const sidecarMetadata = () => ({
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T02:00:00Z",
    deerflow_sidecar: true,
    parent_thread_id: THREAD_ID,
    sidecar_context_type: "referenced_message",
    sidecar_context_label: "Selected assistant text #1",
    sidecar_context_count: 1,
    referenced_message_id: "msg-project-sidecar-source",
    referenced_message_ids: ["msg-project-sidecar-source"],
    referenced_message_role: "assistant",
    referenced_message_roles: ["assistant"],
  });
  const sidecarThread = () => ({
    thread_id: sidecarThreadId,
    agent_asset_id: AGENT_ID,
    agent_scope: "project",
    display_name: "Project side chat",
    status: "idle",
    metadata: sidecarMetadata(),
    version: 1,
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T02:00:00Z",
  });

  await page.route(
    `**/api/projects/${PROJECT_ID}/private-work/**`,
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;

      if (request.method() === "POST" && path.endsWith("/threads/search")) {
        requests.push(`${request.method()} ${path}`);
        return json(route, {
          items:
            searchVisible && sidecarThreadId
              ? [sidecarThread(), privateThread]
              : [privateThread],
        });
      }

      if (request.method() === "POST" && path.endsWith("/threads")) {
        const body = request.postDataJSON() as Record<string, unknown>;
        createBodies.push(body);
        sidecarThreadId = String(body.thread_id);
        searchVisible = true;
        requests.push(`${request.method()} ${path}`);
        return json(route, {
          ...sidecarThread(),
          display_name:
            typeof body.display_name === "string" ? body.display_name : null,
          metadata: {
            ...sidecarMetadata(),
            ...(typeof body.metadata === "object" && body.metadata !== null
              ? body.metadata
              : {}),
          },
        });
      }

      if (!sidecarThreadId || !path.includes(`/threads/${sidecarThreadId}`)) {
        return route.fallback();
      }
      requests.push(`${request.method()} ${path}`);

      if (path.endsWith(`/threads/${sidecarThreadId}/state`)) {
        return json(route, {
          values: {
            title: "Project side chat",
            messages,
            artifacts: [],
            todos: [],
          },
          next: [],
          metadata: {},
          checkpoint: {},
          checkpoint_id: null,
          parent_checkpoint_id: null,
          created_at: "2026-07-15T02:00:00Z",
          tasks: [],
        });
      }
      if (path.endsWith(`/threads/${sidecarThreadId}/token-usage`)) {
        return json(route, {
          total_input_tokens: 0,
          total_output_tokens: 0,
          total_tokens: 0,
        });
      }
      if (path.endsWith(`/threads/${sidecarThreadId}/uploads/limits`)) {
        return json(route, {
          max_files: 10,
          max_file_size: 50 * 1024 * 1024,
          max_total_size: 100 * 1024 * 1024,
          project_storage: {
            policy: "project_quota",
            remaining_bytes: 5 * 1024 * 1024 * 1024,
          },
          request_id: "project-private-upload-limits",
        });
      }
      if (
        request.method() === "GET" &&
        path.endsWith(`/threads/${sidecarThreadId}/uploads`)
      ) {
        return json(route, []);
      }
      if (path.endsWith(`/threads/${sidecarThreadId}/runs/stream`)) {
        const body = request.postDataJSON() as {
          input?: { messages?: unknown[] };
        };
        runBodies.push(body);
        const submitted = body.input?.messages ?? [];
        messages = [
          ...messages,
          ...submitted,
          {
            type: "ai",
            id: `msg-sidecar-ai-${runBodies.length}`,
            content: "Scoped side answer.",
          },
        ];
        return handleRunStream(route, {}, submitted);
      }
      if (/\/threads\/[^/]+\/runs(?:\?|$)/u.test(request.url())) {
        return json(route, []);
      }
      if (path.endsWith(`/threads/${sidecarThreadId}`)) {
        if (request.method() === "DELETE") {
          await options.deleteGate;
          searchVisible = false;
          return route.fulfill({ status: 204 });
        }
        if (!searchVisible) return json(route, { detail: "not found" }, 404);
        return json(route, sidecarThread());
      }
      return json(route, { detail: "not found" }, 404);
    },
  );

  return {
    requests,
    createBodies,
    runBodies,
    globalRequests,
    hideFromSearch: () => {
      searchVisible = false;
    },
  };
}

async function selectProjectMessageText(page: Page, text: string) {
  await expect(
    page.getByTestId("main-message-list").getByText(text),
  ).toBeVisible();
  await page.evaluate((targetText) => {
    const root = document.querySelector('[data-testid="main-message-list"]');
    const walker = document.createTreeWalker(
      root ?? document.body,
      NodeFilter.SHOW_TEXT,
    );
    let node = walker.nextNode();
    while (node) {
      const value = node.textContent ?? "";
      const start = value.indexOf(targetText);
      if (start >= 0) {
        const range = document.createRange();
        range.setStart(node, start);
        range.setEnd(node, start + targetText.length);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
        node.parentElement?.dispatchEvent(
          new MouseEvent("mouseup", { bubbles: true }),
        );
        return;
      }
      node = walker.nextNode();
    }
    throw new Error(`Unable to find project message text: ${targetText}`);
  }, text);
  await expect(page.locator("[data-sidecar-selection-toolbar]")).toBeVisible();
}

async function openProjectSidecarDraft(page: Page, text: string) {
  await selectProjectMessageText(page, text);
  await page
    .locator("[data-sidecar-selection-toolbar]")
    .getByRole("button", { name: /ask in side chat/i })
    .click();
  await expect(page.getByTestId("sidecar-panel")).toBeVisible();
}

function deferredGate() {
  let release!: () => void;
  let markStarted!: () => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  const started = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  return { promise, release, started, markStarted };
}

type SidecarRaceOptions = {
  initialSidecars?: Record<string, string>;
  createGate?: ReturnType<typeof deferredGate>;
  restoreGate?: ReturnType<typeof deferredGate>;
  deleteGate?: ReturnType<typeof deferredGate>;
};

async function mockProjectSidecarRaces(
  page: Page,
  options: SidecarRaceOptions = {},
) {
  const sidecars = new Map(Object.entries(options.initialSidecars ?? {}));
  const sidecarParents = new Map(
    [...sidecars.entries()].map(([parentId, sidecarId]) => [
      sidecarId,
      parentId,
    ]),
  );
  const createBodies: Array<Record<string, unknown>> = [];
  const runBodies: Array<{ threadId: string; body: unknown }> = [];
  const globalRequests: string[] = [];
  let createCount = 0;
  let restoreCount = 0;
  let deleteCount = 0;

  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      path === "/api/threads" ||
      path.startsWith("/api/threads/") ||
      path.startsWith("/api/langgraph/threads") ||
      path.startsWith("/api/langgraph/runs")
    ) {
      globalRequests.push(`${request.method()} ${path}`);
    }
  });

  const parentThread = (threadId: string) => ({
    ...privateThread,
    thread_id: threadId,
    display_name:
      threadId === THREAD_ID ? "Owner research" : "Second project thread",
  });
  const sidecarThread = (threadId: string, parentId: string) => ({
    thread_id: threadId,
    agent_asset_id: AGENT_ID,
    agent_scope: "project",
    display_name: "Project side chat",
    status: "idle",
    metadata: {
      created_at: "2026-07-15T00:00:00Z",
      updated_at: "2026-07-15T02:00:00Z",
      deerflow_sidecar: true,
      parent_thread_id: parentId,
      sidecar_context_type: "referenced_message",
      sidecar_context_label: "Selected assistant text #1",
      sidecar_context_count: 1,
      referenced_message_id: `source-${parentId}`,
      referenced_message_ids: [`source-${parentId}`],
      referenced_message_role: "assistant",
      referenced_message_roles: ["assistant"],
    },
    version: 1,
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T02:00:00Z",
  });
  const state = (messages: unknown[]) => ({
    values: { title: "Project thread", messages, artifacts: [], todos: [] },
    next: [],
    metadata: {},
    checkpoint: {},
    checkpoint_id: null,
    parent_checkpoint_id: null,
    created_at: "2026-07-15T02:00:00Z",
    tasks: [],
  });

  await page.route(
    `**/api/projects/${PROJECT_ID}/private-work/**`,
    async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;

      if (request.method() === "POST" && path.endsWith("/threads/search")) {
        restoreCount += 1;
        if (options.restoreGate) {
          if (restoreCount === 1) options.restoreGate.markStarted();
          await options.restoreGate.promise;
        }
        return json(route, {
          items: [
            ...[...sidecars.entries()].map(([parentId, sidecarId]) =>
              sidecarThread(sidecarId, parentId),
            ),
            parentThread(THREAD_ID),
            parentThread(SECOND_THREAD_ID),
          ],
        });
      }

      if (request.method() === "POST" && path.endsWith("/threads")) {
        const body = request.postDataJSON() as Record<string, unknown>;
        createBodies.push(body);
        createCount += 1;
        if (createCount === 1 && options.createGate) {
          options.createGate.markStarted();
          await options.createGate.promise;
        }
        const threadId = String(body.thread_id);
        const metadata = body.metadata as Record<string, unknown>;
        const parentId = String(metadata.parent_thread_id);
        sidecars.set(parentId, threadId);
        sidecarParents.set(threadId, parentId);
        return json(route, {
          ...sidecarThread(threadId, parentId),
          metadata: {
            ...sidecarThread(threadId, parentId).metadata,
            ...metadata,
          },
        });
      }

      for (const parentId of [THREAD_ID, SECOND_THREAD_ID]) {
        if (path.endsWith(`/threads/${parentId}`)) {
          return json(route, parentThread(parentId));
        }
        if (path.endsWith(`/threads/${parentId}/state`)) {
          return json(
            route,
            state([
              {
                type: "ai",
                id: `source-${parentId}`,
                content:
                  parentId === THREAD_ID
                    ? "First parent race source."
                    : "Second parent race source.",
              },
            ]),
          );
        }
        if (path.endsWith(`/threads/${parentId}/token-usage`)) {
          return json(route, {
            total_input_tokens: 0,
            total_output_tokens: 0,
            total_tokens: 0,
          });
        }
        if (path.endsWith(`/threads/${parentId}/uploads`)) {
          return json(route, []);
        }
        if (path.endsWith(`/threads/${parentId}/runs`)) {
          return json(route, []);
        }
      }

      const sidecarEntry = [...sidecarParents.entries()].find(([threadId]) =>
        path.includes(`/threads/${threadId}`),
      );
      if (!sidecarEntry) return route.fallback();
      const [sidecarId, parentId] = sidecarEntry;

      if (path.endsWith(`/threads/${sidecarId}/state`)) {
        return json(route, state([]));
      }
      if (path.endsWith(`/threads/${sidecarId}/token-usage`)) {
        return json(route, {
          total_input_tokens: 0,
          total_output_tokens: 0,
          total_tokens: 0,
        });
      }
      if (path.endsWith(`/threads/${sidecarId}/uploads/limits`)) {
        return json(route, {
          max_files: 10,
          max_file_size: 50 * 1024 * 1024,
          max_total_size: 100 * 1024 * 1024,
          project_storage: {
            policy: "project_quota",
            remaining_bytes: 5 * 1024 * 1024 * 1024,
          },
          request_id: "project-private-sidecar-upload-limits",
        });
      }
      if (path.endsWith(`/threads/${sidecarId}/uploads`)) {
        return json(route, []);
      }
      if (path.endsWith(`/threads/${sidecarId}/runs/stream`)) {
        const body = request.postDataJSON();
        runBodies.push({ threadId: sidecarId, body });
        return handleRunStream(route);
      }
      if (/\/threads\/[^/]+\/runs(?:\?|$)/u.test(request.url())) {
        return json(route, []);
      }
      if (path.endsWith(`/threads/${sidecarId}`)) {
        if (request.method() === "DELETE") {
          deleteCount += 1;
          if (deleteCount === 1 && options.deleteGate) {
            options.deleteGate.markStarted();
            await options.deleteGate.promise;
          }
          sidecars.delete(parentId);
          sidecarParents.delete(sidecarId);
          return route.fulfill({ status: 204 });
        }
        return json(route, sidecarThread(sidecarId, parentId));
      }
      return json(route, { detail: "not found" }, 404);
    },
  );

  return { createBodies, runBodies, globalRequests };
}

async function switchProjectParent(page: Page, threadId: string) {
  const statePath = `/api/projects/${PROJECT_ID}/private-work/threads/${threadId}/state`;
  const stateResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && url.pathname === statePath;
  });
  await page.evaluate(async (nextThreadId) => {
    const appRouter = (
      window as Window & {
        next?: { router?: { push: (href: string) => Promise<void> } };
      }
    ).next?.router;
    if (!appRouter) {
      throw new Error("Next app router is unavailable");
    }
    await appRouter.push(`/projects/research-lab/chats/${nextThreadId}`);
  }, threadId);
  await expect(page).toHaveURL(
    new RegExp(`/projects/research-lab/chats/${threadId}$`, "u"),
  );
  await stateResponse;
  await expect(
    page.getByText(
      threadId === THREAD_ID
        ? "First parent race source."
        : "Second parent race source.",
    ),
  ).toBeVisible();
}

type MockSpeechResult = {
  transcript: string;
  isFinal: boolean;
};

type MockSpeechRecognitionSnapshot = {
  abortCalls: number;
  active: boolean;
  continuous: boolean;
  handlers: {
    end: boolean;
    error: boolean;
    result: boolean;
  };
  interimResults: boolean;
  lang: string;
  maxAlternatives: number;
  startCalls: number;
  stopCalls: number;
};

async function installControllableSpeechRecognition(page: Page) {
  await page.addInitScript(() => {
    type MockResult = {
      transcript: string;
      isFinal: boolean;
    };
    type MockResultEvent = {
      results: Array<{
        0: { transcript: string };
        isFinal: boolean;
        length: number;
      }>;
    };
    type ResultHandler = ((event: MockResultEvent) => void) | null;
    type ErrorHandler = ((event: { error?: string }) => void) | null;

    const instances: MockSpeechRecognition[] = [];

    class MockSpeechRecognition {
      continuous = false;
      interimResults = false;
      lang = "";
      maxAlternatives = 0;
      startCalls = 0;
      stopCalls = 0;
      abortCalls = 0;
      active = false;
      private endHandler: (() => void) | null = null;
      private errorHandler: ErrorHandler = null;
      private resultHandler: ResultHandler = null;
      private staleResultHandler: ResultHandler = null;

      constructor() {
        instances.push(this);
      }

      get onend() {
        return this.endHandler;
      }

      set onend(handler: (() => void) | null) {
        this.endHandler = handler;
      }

      get onerror() {
        return this.errorHandler;
      }

      set onerror(handler: ErrorHandler) {
        this.errorHandler = handler;
      }

      get onresult() {
        return this.resultHandler;
      }

      set onresult(handler: ResultHandler) {
        this.resultHandler = handler;
        if (handler) {
          this.staleResultHandler = handler;
        }
      }

      start() {
        this.startCalls += 1;
        this.active = true;
      }

      stop() {
        this.stopCalls += 1;
        this.emitEnd();
      }

      abort() {
        this.abortCalls += 1;
        this.emitEnd();
      }

      emitResult(results: MockResult[]) {
        this.resultHandler?.(this.resultEvent(results));
      }

      emitStaleResult(results: MockResult[]) {
        this.staleResultHandler?.(this.resultEvent(results));
      }

      emitError(error: string) {
        this.errorHandler?.({ error });
      }

      emitEnd() {
        this.active = false;
        this.endHandler?.();
      }

      private resultEvent(results: MockResult[]): MockResultEvent {
        return {
          results: results.map((result) => ({
            0: { transcript: result.transcript },
            isFinal: result.isFinal,
            length: 1,
          })),
        };
      }
    }

    function recognitionAt(instanceIndex?: number) {
      const recognition =
        instanceIndex === undefined
          ? instances.at(-1)
          : instances[instanceIndex];
      if (!recognition) {
        throw new Error("Speech recognition instance was not found");
      }
      return recognition;
    }

    const control = {
      emitEnd(instanceIndex?: number) {
        recognitionAt(instanceIndex).emitEnd();
      },
      emitError(error: string, instanceIndex?: number) {
        recognitionAt(instanceIndex).emitError(error);
      },
      emitResult(results: MockResult[], instanceIndex?: number) {
        recognitionAt(instanceIndex).emitResult(results);
      },
      emitStaleResult(instanceIndex: number, results: MockResult[]) {
        recognitionAt(instanceIndex).emitStaleResult(results);
      },
      snapshot() {
        return instances.map((recognition) => ({
          abortCalls: recognition.abortCalls,
          active: recognition.active,
          continuous: recognition.continuous,
          handlers: {
            end: recognition.onend !== null,
            error: recognition.onerror !== null,
            result: recognition.onresult !== null,
          },
          interimResults: recognition.interimResults,
          lang: recognition.lang,
          maxAlternatives: recognition.maxAlternatives,
          startCalls: recognition.startCalls,
          stopCalls: recognition.stopCalls,
        }));
      },
    };

    Object.defineProperty(window, "SpeechRecognition", {
      configurable: true,
      value: MockSpeechRecognition,
    });
    Object.defineProperty(window, "webkitSpeechRecognition", {
      configurable: true,
      value: MockSpeechRecognition,
    });
    Object.defineProperty(window, "__deerFlowSpeechRecognitionTest", {
      configurable: true,
      value: control,
    });
  });
}

async function emitMockSpeechResult(
  page: Page,
  results: MockSpeechResult[],
  instanceIndex?: number,
) {
  await page.evaluate(
    ({ emittedResults, index }) => {
      const control = Reflect.get(
        window,
        "__deerFlowSpeechRecognitionTest",
      ) as {
        emitResult: (
          results: MockSpeechResult[],
          instanceIndex?: number,
        ) => void;
      };
      control.emitResult(emittedResults, index);
    },
    { emittedResults: results, index: instanceIndex },
  );
}

async function emitMockSpeechError(
  page: Page,
  error: string,
  instanceIndex?: number,
) {
  await page.evaluate(
    ({ emittedError, index }) => {
      const control = Reflect.get(
        window,
        "__deerFlowSpeechRecognitionTest",
      ) as {
        emitError: (error: string, instanceIndex?: number) => void;
      };
      control.emitError(emittedError, index);
    },
    { emittedError: error, index: instanceIndex },
  );
}

async function emitMockSpeechEnd(page: Page, instanceIndex?: number) {
  await page.evaluate((index) => {
    const control = Reflect.get(window, "__deerFlowSpeechRecognitionTest") as {
      emitEnd: (instanceIndex?: number) => void;
    };
    control.emitEnd(index);
  }, instanceIndex);
}

async function emitStaleMockSpeechResult(
  page: Page,
  instanceIndex: number,
  results: MockSpeechResult[],
) {
  await page.evaluate(
    ({ index, emittedResults }) => {
      const control = Reflect.get(
        window,
        "__deerFlowSpeechRecognitionTest",
      ) as {
        emitStaleResult: (
          instanceIndex: number,
          results: MockSpeechResult[],
        ) => void;
      };
      control.emitStaleResult(index, emittedResults);
    },
    { index: instanceIndex, emittedResults: results },
  );
}

async function mockSpeechRecognitionSnapshot(page: Page) {
  return page.evaluate(() => {
    const control = Reflect.get(window, "__deerFlowSpeechRecognitionTest") as {
      snapshot: () => MockSpeechRecognitionSnapshot[];
    };
    return control.snapshot();
  });
}

async function mockSecondaryProjectThread(page: Page) {
  await page.route(
    new RegExp(
      `/api/projects/${PROJECT_ID}/private-work/threads/${SECOND_THREAD_ID}(?:/[^?]*)?(?:\\?.*)?$`,
      "u",
    ),
    async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith(`/threads/${SECOND_THREAD_ID}/state`)) {
        return json(route, {
          values: {
            title: "Second owner research",
            messages: [
              {
                type: "human",
                id: "msg-second-project-history",
                content: [{ type: "text", text: "Second thread history" }],
              },
            ],
            artifacts: [],
            todos: [],
          },
          next: [],
          metadata: {},
          checkpoint: {},
          checkpoint_id: null,
          parent_checkpoint_id: null,
          created_at: "2026-07-15T02:00:00Z",
          tasks: [],
        });
      }
      if (path.endsWith(`/threads/${SECOND_THREAD_ID}/token-usage`)) {
        return json(route, {
          total_input_tokens: 0,
          total_output_tokens: 0,
          total_tokens: 0,
        });
      }
      if (path.endsWith(`/threads/${SECOND_THREAD_ID}/goal`)) {
        return json(route, { goal: null });
      }
      if (path.endsWith(`/threads/${SECOND_THREAD_ID}/uploads/limits`)) {
        return json(route, {
          max_files: 10,
          max_file_size: 50 * 1024 * 1024,
          max_total_size: 100 * 1024 * 1024,
          project_storage: {
            policy: "project_quota",
            remaining_bytes: 5 * 1024 * 1024 * 1024,
          },
          request_id: "second-project-private-upload-limits",
        });
      }
      if (path.endsWith(`/threads/${SECOND_THREAD_ID}/uploads`)) {
        return json(route, []);
      }
      if (path.endsWith(`/threads/${SECOND_THREAD_ID}/runs`)) {
        return json(route, []);
      }
      if (path.endsWith(`/threads/${SECOND_THREAD_ID}`)) {
        return json(route, {
          ...privateThread,
          thread_id: SECOND_THREAD_ID,
          display_name: "Second owner research",
          metadata: {
            created_at: "2026-07-15T00:00:00Z",
            updated_at: "2026-07-15T02:00:00Z",
          },
          updated_at: "2026-07-15T02:00:00Z",
        });
      }
      return json(route, { detail: "not found" }, 404);
    },
  );
}

test.beforeEach(async ({ page }) => {
  mockLangGraphAPI(page, { suggestionsEnabled: true });
  await mockProjectContext(page);
  await page.route(
    `**/api/projects/${PROJECT_ID}/automations/readiness`,
    (route) =>
      json(route, {
        status: "ready",
        code: "AUTOMATION_READY",
        scheduler_enabled: true,
        scheduler_status: "running",
        project_private_work_ready: true,
        schema_ready: true,
        request_id: "req-automation-ready",
      }),
  );
  await page.route(`**/api/projects/${PROJECT_ID}/skills`, (route) =>
    json(route, {
      system_items: [],
      project_items: [],
      request_id: "req-project-skills",
    }),
  );
  await page.route(`**/api/projects/${PROJECT_ID}/mcp-servers`, (route) =>
    json(route, {
      system_items: [],
      project_items: [],
      request_id: "req-project-mcp-servers",
    }),
  );
  await page.route(`**/api/projects/${PROJECT_ID}/agents`, (route) =>
    json(route, {
      system_items: [
        {
          id: MAIN_AGENT_ID,
          scope: "system",
          project_id: null,
          slug: "project-assistant",
          display_name: "Main",
          status: "active",
          current_published_version_id: "31000000-0000-4000-8000-000000000001",
          version: 1,
          created_by_user_id: "system",
          created_at: "2026-07-15T00:00:00Z",
          updated_at: "2026-07-15T00:00:00Z",
          capabilities: ["shared_assets.read", "shared_assets.execute"],
          binding: {
            project_id: PROJECT_ID,
            kind: "agent",
            asset_id: MAIN_AGENT_ID,
            version_id: "31000000-0000-4000-8000-000000000001",
            enabled: true,
            version: 1,
            created_by_user_id: ACCOUNT_ID,
            updated_by_user_id: ACCOUNT_ID,
            created_at: "2026-07-15T00:00:00Z",
            updated_at: "2026-07-15T00:00:00Z",
          },
        },
      ],
      project_items: [],
      request_id: "req-project-agents",
    }),
  );
  await page.route(`**/api/projects/${PROJECT_ID}/default-agent`, (route) =>
    json(route, {
      agent_asset_id: null,
      revision: 0,
      request_id: "req-project-default-agent",
    }),
  );
  await page.route(
    `**/api/projects/${PROJECT_ID}/agents/${MAIN_AGENT_ID}/versions`,
    (route) =>
      json(route, {
        data: [
          {
            id: "31000000-0000-4000-8000-000000000001",
            agent_id: MAIN_AGENT_ID,
            version_number: 1,
            workflow_status: "published",
            description: "Main project assistant",
            agents_instructions: "",
            soul: "",
            identity: "",
            user_context: "",
            payload_schema_version: 1,
            model_ref: "mock-model",
            tool_groups: [],
            skill_version_ids: [],
            mcp_version_ids: [],
            supersedes_version_id: null,
            payload_checksum: "main-agent-v1",
            created_by_user_id: "system",
            created_at: "2026-07-15T00:00:00Z",
          },
        ],
        request_id: "req-project-main-agent-versions",
      }),
  );
});

test("project detail loads history and streams without legacy private-work calls", async ({
  page,
}) => {
  const controlBodies: Array<{ path: string; body: unknown }> = [];
  const projectRequests = await mockPrivateWork(page, true, { controlBodies });
  const legacyPrivateRequests: string[] = [];
  const legacySuggestionRequests: string[] = [];
  const legacyArtifactRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      path.startsWith("/api/langgraph/threads") ||
      path.startsWith("/api/threads/")
    ) {
      legacyPrivateRequests.push(`${request.method()} ${path}`);
    }
    if (path === `/api/threads/${THREAD_ID}/suggestions`) {
      legacySuggestionRequests.push(`${request.method()} ${path}`);
    }
    if (path.startsWith(`/api/threads/${THREAD_ID}/artifacts`)) {
      legacyArtifactRequests.push(`${request.method()} ${path}`);
    }
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);

  await expect(page.getByText("Previous project question")).toBeVisible();
  await expect(page.getByRole("button", { name: "Submit" })).toBeVisible();
  await expect(page.getByTestId("add-attachments-button")).toBeVisible();
  await expect(
    page.locator(
      `a[href="/projects/research-lab/automations?thread_id=${THREAD_ID}"]`,
    ),
  ).toHaveCount(0);
  await expect(page.getByText("Scheduled tasks")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /sidecar/i })).toHaveCount(0);

  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("Hello from project");
  await textarea.press("Enter");
  await expect(page.getByText("Hello from DeerFlow!")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Branch conversation" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Regenerate" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Review the project result?" }),
  ).toBeVisible();

  expect(projectRequests).toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/stream`,
  );
  expect(projectRequests).toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/suggestions`,
  );
  expect(controlBodies).toContainEqual({
    path: `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/suggestions`,
    body: { n: 3 },
  });
  expect(legacySuggestionRequests).toEqual([]);
  expect(legacyArtifactRequests).toEqual([]);
  expect(legacyPrivateRequests).toEqual([]);
});

test("project voice dictation merges browser transcripts and cleans up without submitting", async ({
  page,
}) => {
  await installControllableSpeechRecognition(page);

  const runBodies: unknown[] = [];
  const projectRequests = await mockPrivateWork(page, true, { runBodies });
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);

  const textarea = page.getByPlaceholder(/how can i assist you/i);
  const voiceButton = page.getByTestId("voice-input-button");
  await textarea.fill("Existing typed draft");
  await expect(voiceButton).toBeEnabled();
  await expect(voiceButton).toHaveAttribute("aria-label", "Dictate with voice");
  await expect(voiceButton).toHaveAttribute("aria-pressed", "false");

  await voiceButton.click();
  await expect(voiceButton).toHaveAttribute("aria-label", "Stop voice input");
  await expect(voiceButton).toHaveAttribute("aria-pressed", "true");
  await expect
    .poll(() =>
      page.evaluate(() => {
        const control = Reflect.get(
          window,
          "__deerFlowSpeechRecognitionTest",
        ) as {
          snapshot: () => Array<{ startCalls: number }>;
        };
        return control.snapshot()[0]?.startCalls;
      }),
    )
    .toBe(1);

  await emitMockSpeechResult(page, [
    { transcript: " interim phrase ", isFinal: false },
  ]);
  await expect(textarea).toHaveValue("Existing typed draft interim phrase");

  await emitMockSpeechResult(page, [
    { transcript: " final words ", isFinal: true },
    { transcript: " interim tail ", isFinal: false },
  ]);
  await expect(textarea).toHaveValue(
    "Existing typed draft final words interim tail",
  );

  await voiceButton.click();
  await expect(voiceButton).toHaveAttribute("aria-label", "Dictate with voice");
  await expect(voiceButton).toHaveAttribute("aria-pressed", "false");
  await expect(textarea).toHaveValue(
    "Existing typed draft final words interim tail",
  );

  const recognitionState = await mockSpeechRecognitionSnapshot(page);
  expect(recognitionState).toEqual([
    {
      abortCalls: 0,
      active: false,
      continuous: true,
      handlers: { end: false, error: false, result: false },
      interimResults: true,
      lang: "en-US",
      maxAlternatives: 1,
      startCalls: 1,
      stopCalls: 1,
    },
  ]);
  expect(runBodies).toEqual([]);
  expect(projectRequests).not.toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/stream`,
  );
});

test("project voice dictation restarts after natural end and ignores the old recognizer", async ({
  page,
}) => {
  await installControllableSpeechRecognition(page);
  const runBodies: unknown[] = [];
  const projectRequests = await mockPrivateWork(page, true, { runBodies });
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);

  const textarea = page.getByPlaceholder(/how can i assist you/i);
  const voiceButton = page.getByTestId("voice-input-button");
  await textarea.fill("Restart baseline");
  await voiceButton.click();
  await emitMockSpeechResult(
    page,
    [{ transcript: " first segment ", isFinal: true }],
    0,
  );
  await expect(textarea).toHaveValue("Restart baseline first segment");

  await emitMockSpeechEnd(page, 0);
  await expect(voiceButton).toHaveAttribute("aria-label", "Stop voice input");
  await expect(voiceButton).toHaveAttribute("aria-pressed", "true");
  await expect
    .poll(async () => (await mockSpeechRecognitionSnapshot(page)).length, {
      timeout: 2_000,
    })
    .toBe(2);

  let recognitionState = await mockSpeechRecognitionSnapshot(page);
  expect(recognitionState[0]).toMatchObject({
    active: false,
    handlers: { end: false, error: false, result: false },
    startCalls: 1,
    stopCalls: 0,
  });
  expect(recognitionState[1]).toMatchObject({
    active: true,
    handlers: { end: true, error: true, result: true },
    startCalls: 1,
  });
  await expect(textarea).toHaveValue("Restart baseline first segment");

  await emitStaleMockSpeechResult(page, 0, [
    { transcript: " stale old transcript ", isFinal: true },
  ]);
  await expect(textarea).toHaveValue("Restart baseline first segment");

  await emitMockSpeechResult(
    page,
    [{ transcript: " second segment ", isFinal: false }],
    1,
  );
  await expect(textarea).toHaveValue(
    "Restart baseline first segment second segment",
  );

  await voiceButton.click();
  await expect(voiceButton).toHaveAttribute("aria-label", "Dictate with voice");
  await expect(voiceButton).toHaveAttribute("aria-pressed", "false");
  recognitionState = await mockSpeechRecognitionSnapshot(page);
  expect(recognitionState[1]).toMatchObject({
    active: false,
    handlers: { end: false, error: false, result: false },
    stopCalls: 1,
  });
  expect(runBodies).toEqual([]);
  expect(projectRequests).not.toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/stream`,
  );
});

test("project voice permission denial returns to idle without restarting or submitting", async ({
  page,
}) => {
  await installControllableSpeechRecognition(page);
  const runBodies: unknown[] = [];
  const projectRequests = await mockPrivateWork(page, true, { runBodies });
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);

  const voiceButton = page.getByTestId("voice-input-button");
  await voiceButton.click();
  await expect(voiceButton).toHaveAttribute("aria-label", "Stop voice input");
  await emitMockSpeechError(page, "not-allowed", 0);
  await expect(
    page.getByText(
      "Microphone access was denied. Allow microphone access and try again.",
    ),
  ).toBeVisible();
  await emitMockSpeechEnd(page, 0);

  await expect(voiceButton).toHaveAttribute("aria-label", "Dictate with voice");
  await expect(voiceButton).toHaveAttribute("aria-pressed", "false");
  await page.waitForTimeout(250);
  expect(await mockSpeechRecognitionSnapshot(page)).toEqual([
    {
      abortCalls: 0,
      active: false,
      continuous: true,
      handlers: { end: false, error: false, result: false },
      interimResults: true,
      lang: "en-US",
      maxAlternatives: 1,
      startCalls: 1,
      stopCalls: 0,
    },
  ]);
  expect(runBodies).toEqual([]);
  expect(projectRequests).not.toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/stream`,
  );
});

test("project thread navigation aborts dictation and isolates stale callbacks from the next composer", async ({
  page,
}) => {
  await installControllableSpeechRecognition(page);
  const runBodies: unknown[] = [];
  const projectRequests = await mockPrivateWork(page, true, { runBodies });
  await mockSecondaryProjectThread(page);
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);

  let textarea = page.getByPlaceholder(/how can i assist you/i);
  let voiceButton = page.getByTestId("voice-input-button");
  await textarea.fill("First thread baseline");
  await voiceButton.click();
  await emitMockSpeechResult(
    page,
    [{ transcript: " active voice ", isFinal: false }],
    0,
  );
  await expect(textarea).toHaveValue("First thread baseline active voice");

  const secondStateResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === "GET" &&
      url.pathname.endsWith(`/threads/${SECOND_THREAD_ID}/state`)
    );
  });
  await page.evaluate(async (nextThreadId) => {
    const appRouter = (
      window as Window & {
        next?: { router?: { push: (href: string) => Promise<void> } };
      }
    ).next?.router;
    if (!appRouter) {
      throw new Error("Next app router is unavailable");
    }
    await appRouter.push(`/projects/research-lab/chats/${nextThreadId}`);
  }, SECOND_THREAD_ID);
  await expect(page).toHaveURL(
    new RegExp(`/projects/research-lab/chats/${SECOND_THREAD_ID}$`, "u"),
  );
  await secondStateResponse;
  await expect(page.getByText("Second thread history")).toBeVisible();
  await expect
    .poll(
      async () =>
        (await mockSpeechRecognitionSnapshot(page))[0]?.abortCalls ?? 0,
    )
    .toBeGreaterThan(0);

  let recognitionState = await mockSpeechRecognitionSnapshot(page);
  expect(recognitionState[0]).toMatchObject({
    active: false,
    handlers: { end: false, error: false, result: false },
    stopCalls: 0,
  });
  textarea = page.getByPlaceholder(/how can i assist you/i);
  voiceButton = page.getByTestId("voice-input-button");
  await expect(textarea).toHaveValue("");
  await expect(voiceButton).toHaveAttribute("aria-label", "Dictate with voice");
  await expect(voiceButton).toHaveAttribute("aria-pressed", "false");

  await emitStaleMockSpeechResult(page, 0, [
    { transcript: " stale after navigation ", isFinal: true },
  ]);
  await expect(textarea).toHaveValue("");

  await voiceButton.click();
  await emitMockSpeechResult(
    page,
    [{ transcript: " second thread voice ", isFinal: true }],
    1,
  );
  await expect(textarea).toHaveValue("second thread voice");
  await emitStaleMockSpeechResult(page, 0, [
    { transcript: " stale against new composer ", isFinal: false },
  ]);
  await expect(textarea).toHaveValue("second thread voice");

  await voiceButton.click();
  recognitionState = await mockSpeechRecognitionSnapshot(page);
  expect(recognitionState[1]).toMatchObject({
    active: false,
    handlers: { end: false, error: false, result: false },
    stopCalls: 1,
  });
  expect(runBodies).toEqual([]);
  expect(projectRequests).not.toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/stream`,
  );
});

test("project goal and compact commands use only scoped control routes", async ({
  page,
}) => {
  const controlBodies: Array<{ path: string; body: unknown }> = [];
  const goalGate = deferredGate();
  const compactGate = deferredGate();
  const projectRequests = await mockPrivateWork(page, true, {
    controlBodies,
    goalGate,
    compactGate,
  });
  const globalRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path === "/api/threads" || path.startsWith("/api/threads/")) {
      globalRequests.push(`${request.method()} ${path}`);
    }
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  const goalResponse = page.waitForResponse((response) => {
    const request = response.request();
    return (
      request.method() === "PUT" &&
      new URL(response.url()).pathname.endsWith(`/threads/${THREAD_ID}/goal`)
    );
  });
  await textarea.fill("/goal Finish the project-scoped repair");
  await textarea.press("Enter");
  await goalGate.started;

  await textarea.fill("/compact");
  goalGate.release();
  await goalResponse;
  await expect(
    page.getByText("Hello from DeerFlow!", { exact: true }),
  ).toBeVisible();
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await expect(textarea).toHaveValue("/compact");
  const submit = page.getByRole("button", { name: "Submit" });
  await expect(submit).toBeEnabled();
  const compactResponse = page.waitForResponse((response) => {
    const request = response.request();
    return (
      request.method() === "POST" &&
      new URL(response.url()).pathname.endsWith(`/threads/${THREAD_ID}/compact`)
    );
  });
  await submit.click();
  await compactGate.started;
  await textarea.fill("Draft typed while compacting");
  compactGate.release();
  await compactResponse;
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await expect(textarea).toHaveValue("Draft typed while compacting");
  await expect
    .poll(() =>
      projectRequests.includes(
        `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/compact`,
      ),
    )
    .toBe(true);

  expect(controlBodies).toContainEqual({
    path: `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/goal`,
    body: {
      objective: "Finish the project-scoped repair",
    },
  });
  expect(controlBodies).toContainEqual({
    path: `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/compact`,
    body: { force: true },
  });
  expect(globalRequests).toEqual([]);
});

test("compaction keeps the full Sentinel timeline across refresh and continuation", async ({
  page,
}) => {
  const earlySentinel = "COMPACTION-EARLY-SENTINEL-8e71";
  const middleSentinel = "COMPACTION-MIDDLE-SENTINEL-4c29";
  const lateSentinel = "COMPACTION-LATE-SENTINEL-a6d3";
  const historyMessages = [
    {
      type: "human",
      id: "msg-sentinel-early-human",
      content: [{ type: "text", text: earlySentinel }],
    },
    {
      type: "ai",
      id: "msg-sentinel-early-ai",
      content: "Early Sentinel recorded.",
    },
    {
      type: "human",
      id: "msg-sentinel-middle-human",
      content: [{ type: "text", text: middleSentinel }],
    },
    {
      type: "ai",
      id: "msg-sentinel-middle-ai",
      content: "Middle Sentinel recorded.",
    },
    {
      type: "human",
      id: "msg-sentinel-late-human",
      content: [{ type: "text", text: lateSentinel }],
    },
    {
      type: "ai",
      id: "msg-sentinel-late-ai",
      content: "Late Sentinel recorded.",
    },
  ];
  const compactedCheckpointTail = historyMessages.slice(-2);
  const recallHuman = {
    type: "human",
    id: "msg-sentinel-recall-human",
    content: [
      {
        type: "text",
        text: "Recall all three compaction Sentinels in chronological order.",
      },
    ],
  };
  const recallAnswer = [earlySentinel, middleSentinel, lateSentinel].join(
    " -> ",
  );
  const recallAi = {
    type: "ai",
    id: "msg-sentinel-recall-ai",
    content: recallAnswer,
  };
  const projectRequests = await mockPrivateWork(page, true, {
    stateMessages: historyMessages,
    stateMessagesAfterCompact: compactedCheckpointTail,
    stateMessagesAfterStream: [recallHuman, recallAi],
    historyRuns: [
      {
        runId: "run-sentinel-late",
        messages: historyMessages.slice(4, 6),
        createdAt: "2026-07-15T02:02:00Z",
      },
      {
        runId: "run-sentinel-middle",
        messages: historyMessages.slice(2, 4),
        createdAt: "2026-07-15T02:01:00Z",
      },
      {
        runId: "run-sentinel-early",
        messages: historyMessages.slice(0, 2),
        createdAt: "2026-07-15T02:00:00Z",
      },
    ],
    streamValueSequence: [
      {
        title: "Owner research",
        messages: [...compactedCheckpointTail, recallHuman, recallAi],
        artifacts: [],
        todos: [],
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  for (const sentinel of [earlySentinel, middleSentinel, lateSentinel]) {
    await expect(page.getByText(sentinel, { exact: true })).toHaveCount(1);
  }

  const compactResponse = page.waitForResponse((response) => {
    const request = response.request();
    return (
      request.method() === "POST" &&
      new URL(response.url()).pathname.endsWith(`/threads/${THREAD_ID}/compact`)
    );
  });
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("/compact");
  const submit = page.getByRole("button", { name: "Submit" });
  await expect(submit).toBeEnabled();
  await submit.click();
  await compactResponse;
  await expect
    .poll(() =>
      projectRequests.includes(
        `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/compact`,
      ),
    )
    .toBe(true);

  await page.reload();
  const sentinelLocators = [earlySentinel, middleSentinel, lateSentinel].map(
    (sentinel) => page.getByText(sentinel, { exact: true }),
  );
  for (const locator of sentinelLocators) {
    await expect(locator).toHaveCount(1);
    await expect(locator).toBeVisible();
  }
  const sentinelTopPositions = await Promise.all(
    sentinelLocators.map(async (locator) => (await locator.boundingBox())?.y),
  );
  expect(sentinelTopPositions.every((position) => position !== undefined)).toBe(
    true,
  );
  expect(sentinelTopPositions[0]).toBeLessThan(sentinelTopPositions[1]!);
  expect(sentinelTopPositions[1]).toBeLessThan(sentinelTopPositions[2]!);
  await sentinelLocators[0]!.scrollIntoViewIfNeeded();
  await captureReasoningEvidence(
    page,
    "context-compaction-sentinel-history-after-refresh",
    page.locator("#chat"),
  );

  await textarea.fill(
    "Recall all three compaction Sentinels in chronological order.",
  );
  await textarea.press("Enter");
  const recallAnswerLocator = page.getByText(recallAnswer, { exact: true });
  await expect(recallAnswerLocator).toBeVisible();
  for (const locator of sentinelLocators) {
    await expect(locator).toHaveCount(1);
  }
  await recallAnswerLocator.scrollIntoViewIfNeeded();
  await captureReasoningEvidence(
    page,
    "context-compaction-sentinel-continuation",
    page.locator("#chat"),
  );
});

test("project branch action stays on the scoped thread endpoint", async ({
  page,
}) => {
  const controlBodies: Array<{ path: string; body: unknown }> = [];
  const projectRequests = await mockPrivateWork(page, true, {
    controlBodies,
    stateMessages: [
      {
        type: "human",
        id: "msg-branch-human",
        content: [{ type: "text", text: "Create a branchable answer" }],
      },
      {
        type: "ai",
        id: "msg-branch-ai",
        content: "This answer can be branched.",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await page.getByRole("button", { name: "Branch conversation" }).click();
  await expect
    .poll(() =>
      projectRequests.includes(
        `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/branches`,
      ),
    )
    .toBe(true);
  expect(controlBodies).toContainEqual({
    path: `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/branches`,
    body: {
      message_id: "msg-branch-ai",
      message_ids: ["msg-branch-ai"],
    },
  });
  await expect(page).toHaveURL(
    `/projects/research-lab/chats/${SECOND_THREAD_ID}`,
  );
});

test("project regenerate prepares from scoped history before a scoped run", async ({
  page,
}) => {
  const controlBodies: Array<{ path: string; body: unknown }> = [];
  const runBodies: unknown[] = [];
  const projectRequests = await mockPrivateWork(page, true, {
    controlBodies,
    runBodies,
    stateMessages: [
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
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await page.getByRole("button", { name: "Regenerate" }).click();
  await expect
    .poll(() =>
      projectRequests.includes(
        `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/regenerate/prepare`,
      ),
    )
    .toBe(true);
  await expect.poll(() => runBodies).toHaveLength(1);
  expect(controlBodies).toContainEqual({
    path: `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/regenerate/prepare`,
    body: { message_id: "msg-ai-1" },
  });
  expect(JSON.stringify(runBodies[0])).toContain("checkpoint-before-human");
});

test("project edit and rerun stays scoped and submits the server-issued replacement", async ({
  page,
}) => {
  const controlBodies: Array<{ path: string; body: unknown }> = [];
  const runBodies: unknown[] = [];
  const projectRequests = await mockPrivateWork(page, true, {
    controlBodies,
    runBodies,
    stateMessages: [
      {
        type: "human",
        id: "msg-edit-human",
        content: [{ type: "text", text: "Original project question" }],
      },
      {
        type: "ai",
        id: "msg-edit-ai",
        content: "Original project answer",
      },
    ],
    stateMessagesAfterStream: [
      {
        type: "human",
        id: "msg-project-edited",
        content: [{ type: "text", text: "Edited project question" }],
      },
      {
        type: "ai",
        id: "msg-edit-ai-new",
        content: "Edited project answer",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const original = page.getByText("Original project question");
  await expect(original).toBeVisible();
  await original.hover();
  await page.getByRole("button", { name: "Edit and rerun" }).click();

  const editor = page.getByTestId("message-edit-textarea");
  await expect(editor).toHaveValue("Original project question");
  await expect(page.getByRole("button", { name: "Regenerate" })).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Branch conversation" }),
  ).toBeDisabled();
  await editor.fill("Edited project question");
  await page.getByRole("button", { name: "Update and rerun" }).click();

  await expect
    .poll(() =>
      projectRequests.includes(
        `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/edit-regenerate/prepare`,
      ),
    )
    .toBe(true);
  expect(controlBodies).toContainEqual({
    path: `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/edit-regenerate/prepare`,
    body: {
      human_message_id: "msg-edit-human",
      replacement_text: "Edited project question",
    },
  });
  await expect.poll(() => runBodies).toHaveLength(1);
  expect(runBodies[0]).toMatchObject({
    input: {
      messages: [
        {
          id: "msg-project-edited",
          content: [{ type: "text", text: "Edited project question" }],
        },
      ],
    },
    checkpoint: { checkpoint_id: "checkpoint-before-human" },
    metadata: {
      replay_kind: "edit",
      edit_from_message_id: "msg-edit-human",
      edit_message_id: "msg-project-edited",
    },
  });
  await expect(page.getByText("Edited project question")).toBeVisible();
  expect(
    projectRequests.some((request) => request.includes("/api/threads/")),
  ).toBe(false);
});

test("project replay controls wait for the SDK initial state request to settle", async ({
  page,
}) => {
  let releaseInitialState!: () => void;
  const initialStateGate = new Promise<void>((resolve) => {
    releaseInitialState = resolve;
  });
  const initialStateGateRequests: string[] = [];
  const runBodies: unknown[] = [];
  const historyMessages = [
    {
      type: "human",
      id: "msg-history-human",
      content: [{ type: "text", text: "History loaded before SDK state" }],
    },
    {
      type: "ai",
      id: "msg-history-ai",
      content: "History answer loaded independently",
    },
  ];
  await mockPrivateWork(page, true, {
    initialStateGate,
    initialStateGateRequests,
    historyRunMessages: historyMessages,
    runBodies,
    stateMessages: historyMessages,
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect.poll(() => initialStateGateRequests.length).toBeGreaterThan(0);
  const historyHuman = page.getByText("History loaded before SDK state");
  await expect(historyHuman).toBeVisible();
  await historyHuman.hover();
  await expect(
    page.getByRole("button", { name: "Edit and rerun" }),
  ).toHaveCount(0);
  expect(runBodies).toHaveLength(0);

  releaseInitialState();

  await historyHuman.hover();
  await expect(
    page.getByRole("button", { name: "Edit and rerun" }),
  ).toBeVisible();
});

test("project edit replay restores its source and draft when run creation fails before metadata", async ({
  page,
}) => {
  const runBodies: unknown[] = [];
  await mockPrivateWork(page, true, {
    runBodies,
    streamResponseStatus: 400,
    stateMessages: [
      {
        type: "human",
        id: "msg-edit-human",
        content: [{ type: "text", text: "Original project question" }],
      },
      {
        type: "ai",
        id: "msg-edit-ai",
        content: "Original project answer",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const original = page.getByText("Original project question");
  await original.hover();
  await page.getByRole("button", { name: "Edit and rerun" }).click();
  const editor = page.getByTestId("message-edit-textarea");
  await editor.fill("Replacement after pre-create failure");
  await page.getByRole("button", { name: "Update and rerun" }).click();

  await expect.poll(() => runBodies).toHaveLength(1);
  await expect(page.getByTestId("message-edit-textarea")).toHaveValue(
    "Replacement after pre-create failure",
  );
  await expect(page.getByText("Original project answer")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Update and rerun" }),
  ).toBeEnabled();
});

test("successful project edit survives a post-stream history refetch failure", async ({
  page,
}) => {
  const runBodies: unknown[] = [];
  const stateStatusAfterStreamRequests: string[] = [];
  await mockPrivateWork(page, true, {
    runBodies,
    stateStatusAfterStream: 503,
    stateStatusAfterStreamRequests,
    stateMessages: [
      {
        type: "human",
        id: "msg-edit-human",
        content: [{ type: "text", text: "Original project question" }],
      },
      {
        type: "ai",
        id: "msg-edit-ai",
        content: "Original project answer",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const original = page.getByText("Original project question");
  await original.hover();
  await page.getByRole("button", { name: "Edit and rerun" }).click();
  await page
    .getByTestId("message-edit-textarea")
    .fill("Successful replacement");
  await page.getByRole("button", { name: "Update and rerun" }).click();

  await expect.poll(() => runBodies).toHaveLength(1);
  await expect
    .poll(() => stateStatusAfterStreamRequests.length)
    .toBeGreaterThan(0);
  await expect(
    page.getByText("post-stream state temporarily unavailable"),
  ).toBeVisible();
  await expect(page.getByText("Successful replacement")).toBeVisible();
  await expect(original).not.toBeVisible();
  await expect(page.getByTestId("run-failure-alert")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Update and rerun" }),
  ).toHaveCount(0);
});

test("failed project edit replay restores the original turn and removes its optimistic replacement", async ({
  page,
}) => {
  const runBodies: unknown[] = [];
  await mockPrivateWork(page, true, {
    runBodies,
    streamTerminalStatus: "failed",
    failurePartialMessages: [
      {
        type: "ai",
        id: "msg-edit-ai-partial-failed",
        content: "Partial replacement answer that must disappear",
      },
    ],
    stateMessages: [
      {
        type: "human",
        id: "msg-edit-human",
        content: [{ type: "text", text: "Original project question" }],
      },
      {
        type: "ai",
        id: "msg-edit-ai",
        content: "Original project answer",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const original = page.getByText("Original project question");
  await original.hover();
  await page.getByRole("button", { name: "Edit and rerun" }).click();
  const editor = page.getByTestId("message-edit-textarea");
  await editor.fill("Replacement that must roll back");
  await page.getByRole("button", { name: "Update and rerun" }).click();

  await expect.poll(() => runBodies).toHaveLength(1);
  await expect(page.getByTestId("run-failure-alert")).toBeVisible();
  await expect(page.getByText("Replacement that must roll back")).toHaveCount(
    1,
  );
  await expect(page.getByText("Original project answer")).toBeVisible();
  await expect(
    page.getByText("Partial replacement answer that must disappear"),
  ).toHaveCount(0);
  await expect(page.getByTestId("message-edit-textarea")).toHaveValue(
    "Replacement that must roll back",
  );
  await expect(
    page.getByRole("button", { name: "Update and rerun" }),
  ).toBeEnabled();
});

test("project chat keeps quoted references inside the scoped conversation", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "ai",
        id: "msg-project-reference",
        content: "Keep this project-only reference attached.",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(
    page.getByText("Keep this project-only reference attached."),
  ).toBeVisible();
  await page.evaluate(() => {
    const target = Array.from(document.querySelectorAll("p")).find((element) =>
      element.textContent?.includes(
        "Keep this project-only reference attached.",
      ),
    );
    const text = target?.firstChild;
    if (!text) throw new Error("project reference text was not found");
    const range = document.createRange();
    range.selectNodeContents(text);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
  await expect(page.locator("[data-sidecar-selection-toolbar]")).toBeVisible();
  await page
    .locator("[data-sidecar-selection-toolbar]")
    .getByRole("button", { name: /add to conversation/i })
    .click();
  await expect(page.getByTestId("conversation-quote-attachment")).toContainText(
    "1 selected text fragment",
  );
});

test("project chat opens and closes a scoped side-chat draft", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "ai",
        id: "msg-project-sidecar",
        content: "Investigate this project-scoped detail.",
      },
    ],
  });
  const sidecar = await mockProjectSidecar(page);
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await page.getByText("Investigate this project-scoped detail.").waitFor();
  await page.evaluate(() => {
    const target = Array.from(document.querySelectorAll("p")).find((element) =>
      element.textContent?.includes("Investigate this project-scoped detail."),
    );
    if (!target) throw new Error("side-chat source was not found");
    const range = document.createRange();
    range.selectNodeContents(target);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
  const toolbar = page.locator("[data-sidecar-selection-toolbar]");
  await toolbar.getByRole("button", { name: /ask in side chat/i }).click();
  await expect(page.getByTestId("sidecar-panel")).toBeVisible();
  await expect(
    page
      .getByTestId("sidecar-panel")
      .getByText("1 selected text fragment")
      .first(),
  ).toBeVisible();
  await page.getByTestId("sidecar-close-button").click();
  await expect(page.getByTestId("sidecar-panel")).toHaveCount(0);
  expect(sidecar.requests.some((request) => request.startsWith("DELETE"))).toBe(
    false,
  );
  expect(sidecar.globalRequests).toEqual([]);
});

test("project side chat creates, sends, and renders references only through private-work", async ({
  page,
}) => {
  const sourceText = "Investigate this scoped side conversation.";
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "ai",
        id: "msg-project-sidecar-source",
        content: sourceText,
      },
    ],
  });
  const sidecar = await mockProjectSidecar(page);

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await page.getByText(sourceText).waitFor();
  await openProjectSidecarDraft(page, sourceText);
  const sidecarInput = page.getByPlaceholder(/deeper follow-up/i);
  await sidecarInput.fill("What should the project do next?");
  await sidecarInput.press("Enter");

  await expect.poll(() => sidecar.createBodies).toHaveLength(1);
  await expect.poll(() => sidecar.runBodies).toHaveLength(1);
  await expect(
    page.getByTestId("sidecar-message-list").getByText("Scoped side answer."),
  ).toBeVisible();
  await expect(
    page
      .getByTestId("sidecar-message-list")
      .getByTestId("message-reference-attachment"),
  ).toContainText("1 selected text fragment");

  expect(sidecar.createBodies[0]?.metadata).toMatchObject({
    deerflow_sidecar: true,
    parent_thread_id: THREAD_ID,
    referenced_message_id: "msg-project-sidecar-source",
  });
  expect(sidecar.requests).toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads`,
  );
  expect(sidecar.requests).toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/${String(sidecar.createBodies[0]?.thread_id)}/runs/stream`,
  );
  expect(sidecar.globalRequests).toEqual([]);
});

test("project side chat restores scoped history", async ({ page }) => {
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "ai",
        id: "msg-project-sidecar-source",
        content: "The parent project history remains here.",
      },
    ],
  });
  const sidecar = await mockProjectSidecar(page, {
    existing: true,
    messages: [
      {
        type: "human",
        id: "msg-sidecar-restored-human",
        content: [{ type: "text", text: "Restored project follow-up" }],
      },
      {
        type: "ai",
        id: "msg-sidecar-restored-ai",
        content: "Restored project side answer.",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(page.getByTestId("sidecar-header-trigger")).toBeVisible();
  await page.getByTestId("sidecar-header-trigger").click();
  await expect(
    page
      .getByTestId("sidecar-message-list")
      .getByText("Restored project side answer."),
  ).toBeVisible();

  expect(sidecar.requests).toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/search`,
  );
  expect(sidecar.requests).toContain(
    `GET /api/projects/${PROJECT_ID}/private-work/threads/${SIDECAR_THREAD_ID}/state`,
  );
  expect(sidecar.globalRequests).toEqual([]);
});

test("project side chat self-heals a stale scoped trigger", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "ai",
        id: "msg-project-sidecar-source",
        content: "The scoped sidecar may be deleted elsewhere.",
      },
    ],
  });
  const sidecar = await mockProjectSidecar(page, {
    existing: true,
    messages: [
      {
        type: "ai",
        id: "msg-sidecar-stale",
        content: "Soon to be deleted.",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(page.getByTestId("sidecar-header-trigger")).toBeVisible();
  sidecar.hideFromSearch();
  await page.getByTestId("sidecar-header-trigger").click();
  await expect(page.getByTestId("sidecar-panel")).toBeHidden();
  await expect(page.getByTestId("sidecar-header-trigger")).toBeHidden();
  expect(sidecar.globalRequests).toEqual([]);
});

test("project side chat deletes only its scoped persisted thread", async ({
  page,
}) => {
  await mockPrivateWork(page);
  const sidecar = await mockProjectSidecar(page, { existing: true });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(page.getByTestId("sidecar-header-trigger")).toBeVisible();
  await page.getByTestId("sidecar-header-trigger").click();
  await page.getByTestId("sidecar-delete-button").click();
  await page.getByTestId("sidecar-delete-confirm-button").click();

  await expect(page.getByTestId("sidecar-panel")).toBeHidden();
  expect(sidecar.requests).toContain(
    `DELETE /api/projects/${PROJECT_ID}/private-work/threads/${SIDECAR_THREAD_ID}`,
  );
  expect(sidecar.globalRequests).toEqual([]);
});

test("project side chat keeps its delete dialog locked while scoped delete is in flight", async ({
  page,
}) => {
  let releaseDelete!: () => void;
  const deleteGate = new Promise<void>((resolve) => {
    releaseDelete = resolve;
  });
  await mockPrivateWork(page);
  const sidecar = await mockProjectSidecar(page, {
    existing: true,
    deleteGate,
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(page.getByTestId("sidecar-header-trigger")).toBeVisible();
  await page.getByTestId("sidecar-header-trigger").click();
  await page.getByTestId("sidecar-delete-button").click();
  const dialogTitle = page.getByRole("heading", { name: "Delete side chat" });
  await page.getByTestId("sidecar-delete-confirm-button").click();

  await expect(
    page.getByTestId("sidecar-delete-confirm-button"),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialogTitle).toBeVisible();
  expect(sidecar.requests).toContain(
    `DELETE /api/projects/${PROJECT_ID}/private-work/threads/${SIDECAR_THREAD_ID}`,
  );
  expect(sidecar.globalRequests).toEqual([]);

  releaseDelete();
  await expect(dialogTitle).toBeHidden();
  await expect(page.getByTestId("sidecar-panel")).toBeHidden();
});

test("project side chat drops a deferred create and queued send after parent switch", async ({
  page,
}) => {
  const createGate = deferredGate();
  await mockPrivateWork(page);
  const race = await mockProjectSidecarRaces(page, { createGate });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await openProjectSidecarDraft(page, "First parent race source.");
  const sidecarInput = page.getByPlaceholder(/deeper follow-up/i);
  await sidecarInput.fill("Old parent queued message");
  await sidecarInput.press("Enter");
  await createGate.started;

  await switchProjectParent(page, SECOND_THREAD_ID);
  createGate.release();
  await page.waitForTimeout(350);

  expect(race.runBodies).toEqual([]);
  await expect(page.getByTestId("sidecar-header-trigger")).toBeHidden();

  await openProjectSidecarDraft(page, "Second parent race source.");
  await page
    .getByPlaceholder(/deeper follow-up/i)
    .fill("New parent normal message");
  await page.getByPlaceholder(/deeper follow-up/i).press("Enter");
  await expect.poll(() => race.runBodies).toHaveLength(1);
  expect(race.runBodies[0]?.threadId).not.toBe(
    String(race.createBodies[0]?.thread_id),
  );
  expect(JSON.stringify(race.runBodies[0]?.body)).toContain(
    "New parent normal message",
  );
  expect(JSON.stringify(race.runBodies)).not.toContain(
    "Old parent queued message",
  );
  expect(race.globalRequests).toEqual([]);
});

test("project side chat invalidates a deferred create when its draft closes", async ({
  page,
}) => {
  const createGate = deferredGate();
  await mockPrivateWork(page);
  const race = await mockProjectSidecarRaces(page, { createGate });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await openProjectSidecarDraft(page, "First parent race source.");
  await page
    .getByPlaceholder(/deeper follow-up/i)
    .fill("Closed create message");
  await page.getByPlaceholder(/deeper follow-up/i).press("Enter");
  await createGate.started;
  await page.getByTestId("sidecar-close-button").click();
  createGate.release();
  await page.waitForTimeout(350);

  expect(race.runBodies).toEqual([]);
  await expect(page.getByTestId("sidecar-header-trigger")).toBeHidden();

  await openProjectSidecarDraft(page, "First parent race source.");
  await page
    .getByPlaceholder(/deeper follow-up/i)
    .fill("Fresh generation after create close");
  await page.getByPlaceholder(/deeper follow-up/i).press("Enter");
  await expect.poll(() => race.runBodies).toHaveLength(1);
  expect(JSON.stringify(race.runBodies[0]?.body)).toContain(
    "Fresh generation after create close",
  );
  expect(JSON.stringify(race.runBodies)).not.toContain("Closed create message");
});

test("project side chat invalidates a deferred restore when its draft closes", async ({
  page,
}) => {
  const restoreGate = deferredGate();
  await mockPrivateWork(page);
  const race = await mockProjectSidecarRaces(page, {
    initialSidecars: { [THREAD_ID]: SIDECAR_THREAD_ID },
    restoreGate,
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await restoreGate.started;
  await openProjectSidecarDraft(page, "First parent race source.");
  await page
    .getByPlaceholder(/deeper follow-up/i)
    .fill("Closed restore message");
  await page.getByPlaceholder(/deeper follow-up/i).press("Enter");
  await page.getByTestId("sidecar-close-button").click();
  restoreGate.release();
  await page.waitForTimeout(350);

  expect(race.runBodies).toEqual([]);
  await expect(page.getByTestId("sidecar-header-trigger")).toBeHidden();

  await openProjectSidecarDraft(page, "First parent race source.");
  await page
    .getByPlaceholder(/deeper follow-up/i)
    .fill("Fresh generation after restore close");
  await page.getByPlaceholder(/deeper follow-up/i).press("Enter");
  await expect.poll(() => race.runBodies).toHaveLength(1);
  expect(JSON.stringify(race.runBodies[0]?.body)).toContain(
    "Fresh generation after restore close",
  );
  expect(JSON.stringify(race.runBodies)).not.toContain(
    "Closed restore message",
  );
});

test("project side chat keeps the new parent identity after an old delete completes", async ({
  page,
}) => {
  const deleteGate = deferredGate();
  await mockPrivateWork(page);
  const race = await mockProjectSidecarRaces(page, {
    initialSidecars: {
      [THREAD_ID]: SIDECAR_THREAD_ID,
      [SECOND_THREAD_ID]: SECOND_SIDECAR_THREAD_ID,
    },
    deleteGate,
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(page.getByTestId("sidecar-header-trigger")).toBeVisible();
  await page.getByTestId("sidecar-header-trigger").click();
  await page.getByTestId("sidecar-delete-button").click();
  await page.getByTestId("sidecar-delete-confirm-button").click();
  await deleteGate.started;

  await switchProjectParent(page, SECOND_THREAD_ID);
  await expect(page.getByTestId("sidecar-header-trigger")).toBeVisible();
  deleteGate.release();

  await expect(page.getByTestId("sidecar-header-trigger")).toBeVisible();
  await page.getByTestId("sidecar-header-trigger").click();
  await expect(page.getByTestId("sidecar-panel")).toBeVisible();
  expect(race.globalRequests).toEqual([]);
});

test("project history renders Mermaid and stopped subtask state", async ({
  page,
}) => {
  const projectRequests = await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "ai",
        id: "msg-project-mermaid",
        content:
          '```mermaid\nflowchart TD\n A[Project] -- "scoped" -.-> B[Thread]\n```',
      },
      {
        type: "ai",
        id: "msg-project-subtask",
        run_id: "run-retained",
        content: "",
        tool_calls: [
          {
            id: "call-project-subtask",
            name: "task",
            args: {
              subagent_type: "general-purpose",
              description: "Scoped stopped subtask",
              prompt: "Investigate scoped state",
            },
          },
        ],
      },
    ],
  });
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(page.getByLabel("Mermaid chart")).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText("Mermaid Error:")).toHaveCount(0);
  await expect(page.getByText("Scoped stopped subtask")).toBeVisible();
  await expect(page.getByText("Subtask failed")).toBeVisible();
  await page.getByText("Scoped stopped subtask").click();
  await expect
    .poll(() =>
      projectRequests.includes(
        `GET /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/run-retained/events`,
      ),
    )
    .toBe(true);
});

test("completed project history preserves reasoning and mixed task tool order", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "human",
        id: "msg-mixed-process-request",
        content: "Delegate two checks and verify between them",
      },
      {
        type: "ai",
        id: "msg-mixed-process",
        run_id: "run-mixed-process",
        content: "",
        additional_kwargs: {
          reasoning_content: "Plan the delegated checks in order.",
          reasoning_duration_ms: 1_000,
        },
        tool_calls: [
          {
            id: "call-first-delegated-check",
            name: "task",
            args: {
              subagent_type: "general-purpose",
              description: "First delegated check",
              prompt: "Run the first delegated check",
            },
          },
          {
            id: "call-between-checks-search",
            name: "web_search",
            args: { query: "delegated verification" },
          },
          {
            id: "call-second-delegated-check",
            name: "task",
            args: {
              subagent_type: "general-purpose",
              description: "Second delegated check",
              prompt: "Run the second delegated check",
            },
          },
        ],
      },
      {
        type: "tool",
        id: "msg-first-delegated-check-result",
        name: "task",
        tool_call_id: "call-first-delegated-check",
        content: "Task Succeeded. Result: first check complete",
      },
      {
        type: "tool",
        id: "msg-between-checks-search-result",
        name: "web_search",
        tool_call_id: "call-between-checks-search",
        content: "[]",
      },
      {
        type: "tool",
        id: "msg-second-delegated-check-result",
        name: "task",
        tool_call_id: "call-second-delegated-check",
        content: "Task Succeeded. Result: second check complete",
      },
      {
        type: "ai",
        id: "msg-mixed-process-final",
        run_id: "run-mixed-process",
        content: "Both delegated checks are complete.",
        additional_kwargs: {
          reasoning_content: "Confirm both delegated checks succeeded.",
          reasoning_duration_ms: 3_000,
        },
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const completedTurn = page
    .locator("[data-assistant-turn]")
    .filter({ hasText: "Both delegated checks are complete." });
  const processDisclosure = completedTurn.getByTestId(
    "assistant-process-disclosure",
  );
  await processDisclosure
    .getByRole("button", { name: /Execution details.*5 steps/ })
    .click();

  const processText = await processDisclosure.innerText();
  const reasoningIndex = processText.indexOf(
    "Plan the delegated checks in order.",
  );
  const firstTaskIndex = processText.indexOf("First delegated check");
  const searchIndex = processText.indexOf("Search on the web for");
  const secondTaskIndex = processText.indexOf("Second delegated check");
  const finalReasoningIndex = processText.indexOf(
    "Confirm both delegated checks succeeded.",
  );
  expect(reasoningIndex).toBeGreaterThanOrEqual(0);
  expect(reasoningIndex).toBeLessThan(firstTaskIndex);
  expect(firstTaskIndex).toBeLessThan(searchIndex);
  expect(searchIndex).toBeLessThan(secondTaskIndex);
  expect(secondTaskIndex).toBeLessThan(finalReasoningIndex);
  await expect(processDisclosure).toContainText("Thought (1 second)");
  await expect(processDisclosure).toContainText(
    "Confirm both delegated checks succeeded.",
  );
  await expect(
    processDisclosure
      .getByTestId("thinking-disclosure")
      .filter({ hasText: "Thought (3 seconds)" }),
  ).toHaveCount(1);
  await expect(
    completedTurn
      .getByTestId("thinking-disclosure")
      .filter({ hasText: "Thought (3 seconds)" }),
  ).toHaveCount(1);
  await captureReasoningEvidence(page, "reasoning-task-tool-order");
});

test("present-files history keeps its own reasoning before a terminal answer", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "human",
        id: "msg-present-reasoning-request",
        content: "Present the checked report",
      },
      {
        type: "ai",
        id: "msg-present-reasoning",
        content: "The checked report is ready.",
        additional_kwargs: {
          reasoning_content: "Select only the checked report for delivery.",
          reasoning_duration_ms: 2_000,
        },
        tool_calls: [
          {
            id: "call-present-reasoning",
            name: "present_files",
            args: { filepaths: [PRESENTED_ARTIFACT_PATH] },
          },
        ],
      },
      {
        type: "tool",
        id: "msg-present-reasoning-result",
        name: "present_files",
        tool_call_id: "call-present-reasoning",
        content: "Successfully presented files",
      },
    ],
    stateArtifacts: [PRESENTED_ARTIFACT_PATH],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const reasoningDisclosure = page
    .getByTestId("thinking-disclosure")
    .filter({ hasText: "Thought (2 seconds)" });
  await expect(reasoningDisclosure).toHaveCount(1);
  await reasoningDisclosure.getByRole("button").click();
  await expect(
    reasoningDisclosure.getByText(
      "Select only the checked report for delivery.",
      { exact: true },
    ),
  ).toBeVisible();
  await captureReasoningEvidence(page, "reasoning-present-files");
});

test("project history preserves plain-text edge cases", async ({ page }) => {
  const source = "const price = '$5';\n> > > nested marker";
  await mockPrivateWork(page, true, {
    stateMessages: [
      { type: "human", id: "msg-project-plain", content: source },
      {
        type: "ai",
        id: "msg-project-nested",
        content: "- > > > deeply nested response",
      },
    ],
  });
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(page.getByText(source, { exact: true })).toBeVisible();
  await expect(page.getByText(/deeply nested response/u)).toBeVisible();
});

test("project write-file artifacts retain preview and survive artifact-less stream values", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    stateMessages: projectArtifactMessages,
    stateMessagesAfterStream: [
      {
        type: "human",
        id: "msg-project-artifact-submitted",
        content: "Continue scoped artifact work",
      },
      {
        type: "ai",
        id: "msg-project-artifact-stream",
        content: "Artifact-less project stream completed.",
      },
    ],
    stateArtifacts: [WRITE_ARTIFACT_PATH, PRESENTED_ARTIFACT_PATH],
    streamValues: { todos: [] },
  });
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await page.getByText(WRITE_ARTIFACT_PATH, { exact: true }).click();
  await expect(
    page.locator("#artifacts").getByText("Project report"),
  ).toBeVisible();
  const artifactTrigger = page.getByTestId("artifact-trigger");
  await expect(artifactTrigger).toBeVisible();
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("Continue scoped artifact work");
  await textarea.press("Enter");
  await expect(
    page.getByText("Artifact-less project stream completed."),
  ).toBeVisible();
  await expect(artifactTrigger).toBeVisible();
});

test("a live file write collapses project navigation, opens preview, and presents the finished file inline", async ({
  page,
}) => {
  const submitted = {
    type: "human",
    id: "msg-live-artifact-request",
    content: [{ type: "text", text: "Write a live project report" }],
  };
  const write = {
    type: "ai",
    id: "msg-live-artifact-write",
    content: "",
    additional_kwargs: {
      reasoning_content: "Plan the requested project report.",
      reasoning_duration_ms: 4_000,
    },
    tool_calls: [
      {
        id: "write-live-project-file",
        name: "write_file",
        args: {
          description: "Writing live project report",
          path: WRITE_ARTIFACT_PATH,
          content: "# Live project report",
        },
      },
    ],
  };
  const writeResult = {
    type: "tool",
    id: "msg-live-artifact-write-result",
    name: "write_file",
    tool_call_id: "write-live-project-file",
    content: "OK",
  };
  const present = {
    type: "ai",
    id: "msg-live-artifact-present",
    content: "The live report is ready.",
    additional_kwargs: {
      reasoning_content: "Publish only the completed project report.",
      reasoning_duration_ms: 2_000,
    },
    tool_calls: [
      {
        id: "present-live-project-file",
        name: "present_files",
        args: { filepaths: [WRITE_ARTIFACT_PATH] },
      },
    ],
  };
  const presentResult = {
    type: "tool",
    id: "msg-live-artifact-present-result",
    name: "present_files",
    tool_call_id: "present-live-project-file",
    content: "Successfully presented files",
  };
  const finalAnswer = {
    type: "ai",
    id: "msg-live-artifact-final",
    run_id: "run-live-artifact",
    content: "Project report completed.",
    additional_kwargs: {
      reasoning_content: "Confirm the requested file is ready.",
      reasoning_duration_ms: 19_000,
      turn_duration: 45,
    },
  };
  const finalMessages = [
    submitted,
    write,
    writeResult,
    present,
    presentResult,
    finalAnswer,
  ];
  await mockPrivateWork(page, true, {
    stateMessages: [],
    stateMessagesAfterStream: finalMessages,
    streamValueSequence: [
      {
        title: "Owner research",
        messages: [submitted, write],
        artifacts: [],
        todos: [],
      },
      {
        title: "Owner research",
        messages: finalMessages,
        artifacts: [WRITE_ARTIFACT_PATH],
        todos: [],
      },
    ],
    uploadedFiles: [],
    uploadedFilesAfterStream: [
      {
        id: PROJECT_FILE_ID,
        logical_path: "outputs/project-report.md",
        display_name: "project-report.md",
        kind: "output",
        media_type: "text/markdown",
        size: 21,
        sha256: "live-project-file-sha",
        status: "ready",
        created_at: "2026-07-15T00:00:00Z",
        updated_at: "2026-07-15T00:00:01Z",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const menu = page.getByRole("complementary", { name: "项目菜单栏" });
  await expect(menu).toHaveAttribute("data-state", "expanded");

  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("Write a live project report");
  await textarea.press("Enter");

  await expect(menu).toHaveAttribute("data-state", "collapsed");
  await expect(
    page.getByText("Project report completed.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("The live report is ready.", { exact: true }),
  ).toHaveCount(0);
  await expect(page.locator("#artifacts").getByRole("combobox")).toContainText(
    "project-report.md",
  );
  await expect(
    page
      .getByTestId("chat-message-content")
      .getByText("project-report.md", { exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("assistant-delivered-files")).toHaveCount(1);
  const processDisclosure = page.getByTestId("assistant-process-disclosure");
  await expect(processDisclosure).toHaveCount(1);
  const processTrigger = processDisclosure.getByRole("button", {
    name: /Execution details.*4 steps/,
  });
  await expect(processTrigger).toHaveAttribute("aria-expanded", "false");
  await processTrigger.click();
  await expect(processTrigger).toHaveAttribute("aria-expanded", "true");
  await expect(
    processDisclosure.getByText("Plan the requested project report.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    processDisclosure.getByText("Writing live project report", { exact: true }),
  ).toBeVisible();
  await expect(
    processDisclosure.getByText("Publish only the completed project report.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    processDisclosure.getByText("Confirm the requested file is ready.", {
      exact: true,
    }),
  ).toBeVisible();
  const processReasoningDisclosures = processDisclosure.getByTestId(
    "thinking-disclosure",
  );
  await expect(processReasoningDisclosures).toHaveCount(3);
  await expect(processReasoningDisclosures.nth(0)).toContainText(
    "Thought (4 seconds)",
  );
  await expect(processReasoningDisclosures.nth(1)).toContainText(
    "Thought (2 seconds)",
  );
  await expect(processReasoningDisclosures.nth(2)).toContainText(
    "Thought (19 seconds)",
  );
  const processText = await processDisclosure.innerText();
  expect(
    processText.indexOf("Plan the requested project report."),
  ).toBeLessThan(processText.indexOf("Writing live project report"));
  expect(processText.indexOf("Writing live project report")).toBeLessThan(
    processText.indexOf("Publish only the completed project report."),
  );
  expect(
    processText.indexOf("Publish only the completed project report."),
  ).toBeLessThan(processText.indexOf("Confirm the requested file is ready."));
  const completedAssistantTurn = page
    .locator("[data-assistant-turn]")
    .filter({ hasText: "Project report completed." });
  const finalProcessThinkingDisclosure = processDisclosure
    .getByTestId("thinking-disclosure")
    .filter({ hasText: "Thought (19 seconds)" });
  await expect(finalProcessThinkingDisclosure).toHaveCount(1);
  await expect(finalProcessThinkingDisclosure).toHaveAttribute(
    "data-state",
    "open",
  );
  await expect(finalProcessThinkingDisclosure).not.toContainText("45s");
  await expect(
    completedAssistantTurn.getByTestId("thinking-disclosure"),
  ).toHaveCount(3);
  await expect(page.getByTestId("run-duration")).toContainText(
    "Completed in 45s",
  );
  await expect(
    processDisclosure.getByText("Confirm the requested file is ready.", {
      exact: true,
    }),
  ).toHaveCount(1);
  await completedAssistantTurn.evaluate((turn) =>
    turn.scrollIntoView({ block: "center" }),
  );
  await captureReasoningEvidence(
    page,
    "reasoning-completed-file-turn",
    completedAssistantTurn,
  );
  const completedTurnOrder = await completedAssistantTurn.evaluate((turn) => {
    const process = turn.querySelector(
      '[data-testid="assistant-process-disclosure"]',
    );
    const answer = Array.from(turn.querySelectorAll("p")).find((element) =>
      element.textContent?.includes("Project report completed."),
    );
    const deliveredFiles = turn.querySelector(
      '[data-testid="assistant-delivered-files"]',
    );
    const runDuration = turn.parentElement?.querySelector(
      '[data-testid="run-duration"]',
    );

    return {
      answerTop: answer?.getBoundingClientRect().top ?? null,
      deliveredFilesTop: deliveredFiles?.getBoundingClientRect().top ?? null,
      processTop: process?.getBoundingClientRect().top ?? null,
      runDurationTop: runDuration?.getBoundingClientRect().top ?? null,
      thinkingOutsideProcess: Array.from(
        turn.querySelectorAll('[data-testid="thinking-disclosure"]'),
      ).filter(
        (element) =>
          !element.closest('[data-testid="assistant-process-disclosure"]'),
      ).length,
    };
  });
  expect(completedTurnOrder.processTop).not.toBeNull();
  expect(completedTurnOrder.thinkingOutsideProcess).toBe(0);
  expect(completedTurnOrder.answerTop).not.toBeNull();
  expect(completedTurnOrder.deliveredFilesTop).not.toBeNull();
  expect(completedTurnOrder.runDurationTop).not.toBeNull();
  expect(completedTurnOrder.processTop!).toBeLessThan(
    completedTurnOrder.answerTop!,
  );
  expect(completedTurnOrder.answerTop!).toBeLessThan(
    completedTurnOrder.deliveredFilesTop!,
  );
  expect(completedTurnOrder.deliveredFilesTop!).toBeLessThan(
    completedTurnOrder.runDurationTop!,
  );
  await page.reload();
  await expect(processTrigger).toHaveAttribute("aria-expanded", "false");
  await processTrigger.click();
  await expect(processTrigger).toHaveAttribute("aria-expanded", "true");
  await expect(processReasoningDisclosures).toHaveCount(3);
  await expect(processReasoningDisclosures.nth(0)).toContainText(
    "Thought (4 seconds)",
  );
  await expect(processReasoningDisclosures.nth(1)).toContainText(
    "Thought (2 seconds)",
  );
  await expect(processReasoningDisclosures.nth(2)).toContainText(
    "Thought (19 seconds)",
  );
  await expect(page.getByTestId("run-duration")).toContainText(
    "Completed in 45s",
  );
  await expect(
    page.getByRole("link", { name: /download/i }).first(),
  ).toHaveAttribute(
    "href",
    new RegExp(
      `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/files/${PROJECT_FILE_ID}$`,
      "u",
    ),
  );
  await page
    .getByRole("button", { name: "Open file: project-report.md", exact: true })
    .click();
  await expect(page.locator("#artifacts").getByRole("combobox")).toContainText(
    "project-report.md",
  );
  if (process.env.CAPTURE_ARTIFACT_FLOW_SCREENSHOT === "1") {
    await page.screenshot({
      path: "test-results/artifact-file-flow.png",
      fullPage: false,
    });
  }

  await page
    .locator("#artifacts")
    .getByRole("button", { name: /close/i })
    .click();
  await page.getByTestId("artifact-trigger").click();
  const durableFileButton = page.locator("#artifacts").getByRole("button", {
    name: /open file.*project-report\.md/i,
  });
  await expect(durableFileButton).toHaveCount(1);
  await durableFileButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#artifacts").getByRole("combobox")).toContainText(
    "project-report.md",
  );
});

test("a finalized workspace file can be reopened from the file list after closing its preview", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "human",
        id: "msg-java-request",
        content: "Write a Java quicksort implementation",
      },
      {
        type: "ai",
        id: "msg-java-write",
        content: "QuickSort.java is ready.",
        tool_calls: [
          {
            id: "write-java-file",
            name: "write_file",
            args: {
              path: "/mnt/user-data/workspace/QuickSort.java",
              content: "public class QuickSort {}",
            },
          },
        ],
      },
      {
        type: "tool",
        id: "msg-java-write-result",
        name: "write_file",
        tool_call_id: "write-java-file",
        content: "OK",
      },
    ],
    stateArtifacts: [],
    uploadedFiles: [
      {
        id: PROJECT_FILE_ID,
        logical_path: "workspace/QuickSort.java",
        display_name: "QuickSort.java",
        kind: "workspace",
        media_type: "text/x-java-source",
        size: 25,
        sha256: "java-file-sha",
        status: "ready",
        created_at: "2026-07-15T00:00:00Z",
        updated_at: "2026-07-15T00:00:00Z",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const fileTrigger = page.getByTestId("artifact-trigger");
  await fileTrigger.click();
  const openFile = page.getByRole("button", {
    name: /open file.*QuickSort\.java/i,
  });
  await expect(openFile).toBeVisible();
  await openFile.click();
  await expect(
    page.locator("#artifacts").getByText("QuickSort.java", { exact: true }),
  ).toBeVisible();

  await page
    .locator("#artifacts")
    .getByRole("button", { name: /close/i })
    .click();
  await fileTrigger.click();
  await expect(openFile).toBeVisible();
  await openFile.click();
  await expect(
    page.locator("#artifacts").getByText("QuickSort.java", { exact: true }),
  ).toBeVisible();
});

test("project workspace changes use only the private-work route", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "ai",
        id: "msg-project-changes",
        run_id: "run-retained",
        content: "Updated scoped files.",
      },
    ],
    workspaceChanges: {
      available: true,
      version: 1,
      summary: {
        created: 0,
        modified: 1,
        deleted: 0,
        additions: 1,
        deletions: 1,
        truncated: false,
      },
      files: [
        {
          path: "/mnt/user-data/outputs/report.md",
          root: "outputs",
          status: "modified",
          binary: false,
          sensitive: false,
          size_before: 5,
          size_after: 5,
          sha256_before: "before",
          sha256_after: "after",
          diff: "-Draft\n+Ready",
          diff_truncated: false,
          diff_unavailable_reason: null,
          additions: 1,
          deletions: 1,
        },
      ],
      limits: {},
    },
  });
  const globalRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith(`/api/threads/${THREAD_ID}/runs/`)) {
      globalRequests.push(path);
    }
  });
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(page.getByText("Edited 1 file")).toBeVisible();
  await page.getByRole("button", { name: "View changes" }).click();
  await expect(page.getByText("+Ready")).toBeVisible();
  expect(globalRequests).toEqual([]);
});

test("project chat polishes draft through only the scoped endpoint", async ({
  page,
}) => {
  const runBodies: unknown[] = [];
  await mockPrivateWork(page, true, { runBodies });
  let polishRequest: { text?: string; model_name?: string } | undefined;
  let finishPolish!: () => void;
  const polishCanFinish = new Promise<void>((resolve) => {
    finishPolish = resolve;
  });
  const globalPolishRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/input-polish") {
      globalPolishRequests.push(request.method());
    }
  });
  await page.route(
    `**/api/projects/${PROJECT_ID}/private-work/input-polish`,
    async (route) => {
      polishRequest = route.request().postDataJSON() as {
        text?: string;
        model_name?: string;
      };
      await polishCanFinish;
      return json(route, {
        rewritten_text: "Please summarize the uploaded report clearly.",
        changed: true,
      });
    },
  );

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill("summarize report");
  await page.getByTestId("polish-input-button").click();

  await expect
    .poll(() => polishRequest?.text, { timeout: 10_000 })
    .toBe("summarize report");
  expect(polishRequest?.model_name).toBeUndefined();
  await expect(textarea).toBeDisabled();
  await expect(page.getByText("Polishing input...")).toBeVisible();

  finishPolish();
  await expect(textarea).toHaveValue(
    "Please summarize the uploaded report clearly.",
  );
  await expect(textarea).toBeEnabled();
  await expect(page.getByTestId("polish-input-button")).toHaveAccessibleName(
    "Undo polish",
  );

  await textarea.press("Enter");
  await expect.poll(() => runBodies.length, { timeout: 10_000 }).toBe(1);
  const runBody = runBodies[0] as {
    input?: { messages?: Array<{ content?: unknown }> };
  };
  expect(runBody.input?.messages?.at(-1)?.content).toEqual([
    {
      type: "text",
      text: "Please summarize the uploaded report clearly.",
    },
  ]);
  expect(globalPolishRequests).toEqual([]);
});

test("project chat undoes a scoped polished draft", async ({ page }) => {
  await mockPrivateWork(page);
  const globalPolishRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/input-polish") {
      globalPolishRequests.push(request.method());
    }
  });
  await page.route(
    `**/api/projects/${PROJECT_ID}/private-work/input-polish`,
    (route) =>
      json(route, {
        rewritten_text: "Please summarize the uploaded report clearly.",
        changed: true,
      }),
  );

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("summarize report");
  await page.getByTestId("polish-input-button").click();
  await expect(textarea).toHaveValue(
    "Please summarize the uploaded report clearly.",
  );

  const polishButton = page.getByTestId("polish-input-button");
  await expect(polishButton).toHaveAccessibleName("Undo polish");
  await polishButton.click();
  await expect(textarea).toHaveValue("summarize report");
  await expect(polishButton).toHaveAccessibleName("Polish input");
  expect(globalPolishRequests).toEqual([]);
});

test("project chat cancels a scoped in-flight polish request", async ({
  page,
}) => {
  await mockPrivateWork(page);
  let releasePolish!: () => void;
  const polishHeld = new Promise<void>((resolve) => {
    releasePolish = resolve;
  });
  const globalPolishRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/input-polish") {
      globalPolishRequests.push(request.method());
    }
  });
  await page.route(
    `**/api/projects/${PROJECT_ID}/private-work/input-polish`,
    async (route) => {
      await polishHeld;
      return json(route, {
        rewritten_text: "Please summarize the uploaded report clearly.",
        changed: true,
      });
    },
  );

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("summarize report");
  await page.getByTestId("polish-input-button").click();
  await expect(page.getByText("Polishing input...")).toBeVisible();
  await expect(textarea).toBeDisabled();

  await page.getByTestId("cancel-polish-input-button").click();
  await expect(page.getByText("Polishing input...")).toBeHidden();
  await expect(textarea).toBeEnabled();
  await expect(textarea).toHaveValue("summarize report");
  await expect(page.getByTestId("polish-input-button")).toHaveAccessibleName(
    "Polish input",
  );
  expect(globalPolishRequests).toEqual([]);
  releasePolish();
});

test("project list is owner-scoped and direct metadata misses show one public not-found", async ({
  page,
}) => {
  await mockPrivateWork(page);
  await page.goto("/projects/research-lab/chats");
  await expect(page.getByText("Owner research")).toBeVisible();
  await expect(page.getByRole("button", { name: "新建对话" })).toBeEnabled();

  await page.goto(`/projects/research-lab/chats/${MISSING_THREAD_ID}`);
  await expect(
    page.getByRole("heading", { name: "找不到这个对话" }),
  ).toBeVisible();
  await expect(
    page
      .locator("main")
      .getByText(/owner_user_id|project_id|跨项目|其他用户/iu),
  ).toHaveCount(0);
});

test("new conversation lets the Gateway resolve the default Agent without opening a selector", async ({
  page,
}) => {
  await mockPrivateWork(page);
  let createBody: Record<string, unknown> | null = null;
  await page.route(
    `**/api/projects/${PROJECT_ID}/private-work/threads`,
    async (route) => {
      createBody = route.request().postDataJSON() as Record<string, unknown>;
      await json(route, {
        thread_id: createBody.thread_id,
        agent_asset_id: MAIN_AGENT_ID,
        agent_scope: "system",
        display_name: createBody.display_name,
        status: "idle",
        metadata: {},
        version: 1,
        created_at: "2026-07-15T04:00:00Z",
        updated_at: "2026-07-15T04:00:00Z",
      });
    },
  );

  await page.goto("/projects/research-lab/chats");
  await page.getByRole("button", { name: "新建对话" }).click();

  await expect
    .poll(() => createBody)
    .toMatchObject({
      display_name: "新对话",
    });
  expect(createBody).not.toHaveProperty("agent_asset_id");
  expect(createBody).not.toHaveProperty("agent_scope");
  await expect(page.getByRole("dialog", { name: "选择 Agent" })).toHaveCount(0);
  await expect(page).toHaveURL(
    new RegExp("/projects/research-lab/chats/[0-9a-f-]+$", "u"),
  );
});

test("project thread list paginates inside the selected project scope", async ({
  page,
}) => {
  const searchThreads = Array.from({ length: 51 }, (_, index) => ({
    ...privateThread,
    thread_id: `20000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
    display_name: `Scoped thread ${index + 1}`,
  }));
  await mockPrivateWork(page, true, { searchThreads });
  await page.goto("/projects/research-lab/chats");
  await expect(
    page.getByText("Scoped thread 1", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Scoped thread 51", { exact: true })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "加载更多" }).click();
  await expect(
    page.getByText("Scoped thread 51", { exact: true }),
  ).toBeVisible();
});

test("project artifacts load only through the scoped project file surface", async ({
  page,
}) => {
  const legacyArtifactRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith(`/api/threads/${THREAD_ID}/artifacts`)) {
      legacyArtifactRequests.push(`${request.method()} ${path}`);
    }
  });
  const projectRequests = await mockPrivateWork(page, true, {
    stateMessages: projectArtifactMessages,
    stateArtifacts: [WRITE_ARTIFACT_PATH, PRESENTED_ARTIFACT_PATH],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(
    page.getByText(WRITE_ARTIFACT_PATH, { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("presented-report.md")).toBeVisible();
  await page.getByText("presented-report.md").click();
  await expect(page.locator("#artifacts")).toHaveCount(1);
  await expect
    .poll(() => projectRequests)
    .toContain(
      `GET /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/files/${PROJECT_FILE_ID}`,
    );

  expect(legacyArtifactRequests).toEqual([]);
});

test("project artifact failures show a public error instead of the response body", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    stateMessages: projectArtifactMessages,
    stateArtifacts: [PRESENTED_ARTIFACT_PATH],
    artifactFileStatus: 503,
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await page.getByText("presented-report.md").click();

  await expect(page.getByText("Unable to load file.")).toBeVisible();
  await expect(page.getByText("private artifact storage failure")).toHaveCount(
    0,
  );
});

test("project .skill artifacts are download-only on the UUID file route", async ({
  page,
}) => {
  const fileRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.includes(`/threads/${THREAD_ID}/files/`)) {
      fileRequests.push(`${request.method()} ${path}`);
    }
  });
  await mockPrivateWork(page, true, {
    stateMessages: [
      {
        type: "ai",
        id: "msg-project-skill-present",
        content: "The project Skill archive is ready.",
        tool_calls: [
          {
            id: "present-project-skill",
            name: "present_files",
            args: { filepaths: [PRESENTED_SKILL_PATH] },
          },
        ],
      },
    ],
    stateArtifacts: [PRESENTED_SKILL_PATH],
    uploadedFiles: [
      {
        id: PROJECT_SKILL_FILE_ID,
        logical_path: "outputs/reviewer.skill",
        display_name: "reviewer.skill",
        kind: "output",
        media_type: "application/octet-stream",
        size: 512,
        sha256: "project-skill-sha",
        status: "ready",
        created_at: "2026-07-15T00:00:00Z",
        updated_at: "2026-07-15T00:00:00Z",
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await page.getByText("reviewer.skill", { exact: true }).click();

  await expect(
    page.getByText("This file type cannot be previewed in the browser."),
  ).toBeVisible();
  const downloadURL = `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/files/${PROJECT_SKILL_FILE_ID}`;
  await expect(
    page.locator("#artifacts").getByRole("link", { name: "Download" }),
  ).toHaveAttribute("href", new RegExp(`${downloadURL}$`, "u"));
  await page.waitForTimeout(200);
  expect(fileRequests).toEqual([]);
});

test("project Skill catalog suggestions submit through the scoped run", async ({
  page,
}) => {
  const runBodies: unknown[] = [];
  const projectRequests = await mockPrivateWork(page, true, { runBodies });
  const globalPrivateRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      path === "/api/skills" ||
      path.startsWith("/api/langgraph/threads") ||
      path.startsWith("/api/threads/")
    ) {
      globalPrivateRequests.push(`${request.method()} ${path}`);
    }
  });
  await page.route(`**/api/projects/${PROJECT_ID}/skills`, (route) =>
    json(route, {
      system_items: [],
      project_items: [
        {
          id: PROJECT_SKILL_ID,
          scope: "project",
          project_id: PROJECT_ID,
          slug: "review-skill",
          display_name: "Review Skill",
          status: "active",
          current_published_version_id: "51000000-0000-4000-8000-000000000001",
          version: 1,
          created_by_user_id: ACCOUNT_ID,
          created_at: "2026-07-15T00:00:00Z",
          updated_at: "2026-07-15T00:00:00Z",
          capabilities: [
            "shared_assets.read",
            "shared_assets.execute",
            "shared_assets.edit",
          ],
          binding: null,
        },
      ],
      request_id: "req-project-skills-suggestion",
    }),
  );

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("/");
  const suggestion = page.getByRole("option", { name: /review-skill/i });
  await expect(suggestion).toBeVisible();
  await textarea.press("ArrowDown");
  await textarea.press("ArrowUp");
  await textarea.press("Enter");
  await expect(page.getByText("/review-skill")).toBeVisible();
  const inlineInput = page.getByRole("textbox", {
    name: /how can i assist you/i,
  });
  await inlineInput.fill("inspect this project");
  await inlineInput.press("Enter");

  await expect.poll(() => runBodies).toHaveLength(1);
  expect(JSON.stringify(runBodies[0])).toContain(
    "/review-skill inspect this project",
  );
  expect(projectRequests).toContain(
    `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/stream`,
  );
  expect(globalPrivateRequests).toEqual([]);
});

test("viewer can delete an owned thread but cannot create or run project work", async ({
  page,
}) => {
  const viewerProject: Project = {
    ...project,
    role: "viewer",
    capabilities: ["project.read", "project.enter", "private_work.read_own"],
  };
  await mockProjectContext(page, viewerProject);
  const projectRequests = await mockPrivateWork(page);

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(page.getByPlaceholder(/how can i assist you/i)).toBeDisabled();
  await expect(page.getByTestId("add-attachments-button")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Branch conversation" }),
  ).toHaveCount(0);

  await page.goto("/projects/research-lab/chats");
  await expect(page.getByRole("button", { name: "新建对话" })).toHaveCount(0);
  await page.getByRole("button", { name: "删除 Owner research" }).click();
  const deleteDialog = page.getByRole("dialog", { name: "删除对话？" });
  await deleteDialog.getByRole("button", { name: "确认删除" }).click();
  await expect(deleteDialog).toHaveCount(0);
  await expect(page.getByText("Owner research")).toHaveCount(0);
  expect(projectRequests).toContain(
    `DELETE /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}`,
  );
});

test("project chat stop aborts the scoped in-flight stream", async ({
  page,
}) => {
  let releaseStream!: () => void;
  const streamGate = new Promise<void>((resolve) => {
    releaseStream = resolve;
  });
  const runBodies: unknown[] = [];
  await mockPrivateWork(page, true, { runBodies, streamGate });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("Hold this project stream");
  await textarea.press("Enter");
  await expect.poll(() => runBodies).toHaveLength(1);

  await page.getByLabel("Submit").click();
  releaseStream();
  await expect(textarea).toBeEnabled();
  await expect(page.getByText("Hello from DeerFlow!")).toHaveCount(0);
});

test("project chat keeps a failed durable Run visible with recovery guidance", async ({
  page,
}) => {
  const runBodies: unknown[] = [];
  const failureMessageIds: Partial<
    Record<"submitted" | "live" | "admission", string>
  > = {};
  const priorMessages = [
    {
      type: "human",
      id: "msg-project-first-question",
      content: [{ type: "text", text: "First project question" }],
    },
    {
      type: "ai",
      id: "msg-project-first-answer",
      content: "First project answer",
    },
    {
      type: "human",
      id: "msg-project-second-question",
      content: [{ type: "text", text: "Second project question" }],
    },
    {
      type: "ai",
      id: "msg-project-second-answer",
      content: "Second project answer",
    },
  ];
  const requests = await mockPrivateWork(page, true, {
    runBodies,
    failureMessageIds,
    stateMessages: priorMessages,
    streamTerminalStatus: "error",
  });
  const expectedMessageOrder = [
    "First project question",
    "First project answer",
    "Second project question",
    "Second project answer",
    "Trigger project failure",
  ];
  const expectSingleOrderedTranscript = async () => {
    const messageList = page.getByTestId("main-message-list");
    for (const text of expectedMessageOrder) {
      await expect(messageList.getByText(text, { exact: true })).toHaveCount(1);
    }
    const transcript = await messageList.innerText();
    const positions = expectedMessageOrder.map((text) =>
      transcript.indexOf(text),
    );
    expect(positions.every((position) => position >= 0)).toBe(true);
    expect(positions).toEqual(
      [...positions].sort((left, right) => left - right),
    );
  };

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("Trigger project failure");
  await textarea.press("Enter");

  await expect.poll(() => runBodies).toHaveLength(1);
  const submittedBody = runBodies[0] as {
    input?: { messages?: unknown[] };
  };
  const submittedMessage = latestVisibleSubmittedHumanMessage(
    submittedBody.input?.messages ?? [],
  );
  const submittedMessageId = submittedMessage
    ? Reflect.get(submittedMessage, "id")
    : null;
  expect(typeof submittedMessageId).toBe("string");
  expect((submittedMessageId as string).length).toBeGreaterThan(0);

  const alert = page.getByTestId("run-failure-alert");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("Run did not finish");
  await expect(alert).toContainText("send the message again");
  await expect(page.getByRole("button", { name: "Submit" })).toBeEnabled();
  await expect
    .poll(() =>
      requests.includes(
        `GET /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/run-failed/messages`,
      ),
    )
    .toBe(true);
  expect(failureMessageIds).toEqual({
    submitted: submittedMessageId,
    live: submittedMessageId,
    admission: submittedMessageId,
  });
  await expectSingleOrderedTranscript();

  await page.reload();
  await expect(page.getByTestId("run-failure-alert")).toBeVisible();
  await expect
    .poll(
      () =>
        requests.filter(
          (request) =>
            request ===
            `GET /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/run-failed/messages`,
        ).length,
    )
    .toBeGreaterThan(1);
  await expectSingleOrderedTranscript();
});

test("project human-input answer stays hidden and scoped in the run body", async ({
  page,
}) => {
  const runBodies: unknown[] = [];
  await mockPrivateWork(page, true, {
    runBodies,
    stateMessages: [
      {
        type: "human",
        id: "msg-human-input-question",
        content: [{ type: "text", text: "Prepare deployment" }],
      },
      {
        type: "ai",
        id: "msg-human-input-call",
        content: "",
        additional_kwargs: {
          reasoning_content: "I need the target environment before deployment.",
          reasoning_duration_ms: 2_000,
        },
        tool_calls: [
          {
            id: "call-project-clarification",
            name: "ask_clarification",
            args: { question: "Which environment?" },
          },
        ],
      },
      {
        type: "tool",
        id: "msg-human-input-tool",
        name: "ask_clarification",
        tool_call_id: "call-project-clarification",
        content: "Which environment?",
        artifact: {
          human_input: {
            version: 1,
            kind: "human_input_request",
            source: "ask_clarification",
            request_id: "clarification:call-project-clarification",
            tool_call_id: "call-project-clarification",
            clarification_type: "approach_choice",
            question: "Which environment should I deploy to?",
            input_mode: "single_choice",
            options: [
              {
                id: "option-development",
                label: "development",
                value: "development",
              },
              {
                id: "option-staging",
                label: "staging",
                value: "staging",
              },
            ],
          },
        },
      },
    ],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  const humanInputCard = page.getByTestId("human-input-card");
  await expect(humanInputCard).toBeVisible();
  await expect(
    humanInputCard.getByText("1 item needs attention", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Need your help", { exact: true })).toHaveCount(
    1,
  );
  const clarificationReasoning = page
    .getByTestId("thinking-disclosure")
    .filter({ hasText: "Thought (2 seconds)" });
  await expect(clarificationReasoning).toHaveCount(1);
  await clarificationReasoning.getByRole("button").click();
  await expect(
    clarificationReasoning.getByText(
      "I need the target environment before deployment.",
      { exact: true },
    ),
  ).toBeVisible();
  await captureReasoningEvidence(page, "reasoning-clarification");

  const staging = humanInputCard.getByRole("radio", { name: "staging" });
  await expect(staging).toHaveAttribute("aria-checked", "false");
  await staging.click();
  await expect(staging).toHaveAttribute("aria-checked", "true");
  expect(runBodies).toHaveLength(0);

  await humanInputCard.getByRole("button", { name: "Submit answer" }).click();

  await expect.poll(() => runBodies).toHaveLength(1);
  const serialized = JSON.stringify(runBodies[0]);
  expect(serialized).toContain('"hide_from_ui":true');
  expect(serialized).toContain('"kind":"human_input_response"');
  expect(serialized).toContain('"value":"staging"');
});

test("project upload is sent only through the scoped upload route", async ({
  page,
}) => {
  const runBodies: unknown[] = [];
  const uploadRequests: string[] = [];
  const requests = await mockPrivateWork(page, true, {
    runBodies,
    uploadRequests,
  });
  const legacyUploads: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path === `/api/threads/${THREAD_ID}/uploads`) {
      legacyUploads.push(`${request.method()} ${path}`);
    }
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await page.getByLabel("Upload files").setInputFiles({
    name: "release.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("release upload\n"),
  });
  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await textarea.fill("Use the project upload");
  await textarea.press("Enter");

  await expect
    .poll(() => uploadRequests)
    .toEqual([
      `POST /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/uploads`,
    ]);
  expect(requests).toContain(
    `GET /api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/uploads/limits`,
  );
  await expect.poll(() => runBodies).toHaveLength(1);
  expect(JSON.stringify(runBodies[0])).toContain(
    "/mnt/user-data/uploads/release.txt",
  );
  expect(legacyUploads).toEqual([]);
});

test("metadata 5xx keeps usable project history instead of showing not-found", async ({
  page,
}) => {
  await mockPrivateWork(page, true, { metadataStatus: 503 });
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);

  await expect(page.getByText("Previous project question")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "找不到这个对话" }),
  ).toHaveCount(0);
});

test("metadata 5xx without history shows a retryable error", async ({
  page,
}) => {
  await mockPrivateWork(page, true, {
    metadataStatus: 503,
    stateMessages: [],
  });
  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);

  await expect(
    page.getByRole("heading", { name: "无法加载这个对话" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "重试" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "找不到这个对话" }),
  ).toHaveCount(0);
});
