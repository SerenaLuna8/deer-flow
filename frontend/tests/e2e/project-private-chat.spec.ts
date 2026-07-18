import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

import { handleRunStream, mockLangGraphAPI } from "./utils/mock-api";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const THREAD_ID = "20000000-0000-4000-8000-000000000001";
const MISSING_THREAD_ID = "20000000-0000-4000-8000-000000000099";
const AGENT_ID = "30000000-0000-4000-8000-000000000001";
const PROJECT_FILE_ID = "40000000-0000-4000-8000-000000000001";
const WRITE_ARTIFACT_PATH = "/mnt/user-data/outputs/project-report.md";
const PRESENTED_ARTIFACT_PATH = "/mnt/user-data/outputs/presented-report.md";

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
  stateArtifacts?: string[];
  artifactFileStatus?: number;
  runBodies?: unknown[];
  streamGate?: Promise<void>;
  uploadRequests?: string[];
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

async function mockPrivateWork(
  page: Page,
  includeThread = true,
  options: MockPrivateWorkOptions = {},
) {
  const requests: string[] = [];
  let threadExists = includeThread;
  let hasStreamed = false;
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
        return json(route, { items: threadExists ? [privateThread] : [] });
      }
      if (path.endsWith(`/threads/${THREAD_ID}/state`)) {
        if (!threadExists) return json(route, { detail: "not found" }, 404);
        return json(route, {
          values: {
            title: "Owner research",
            messages: options.stateMessages ?? [
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
        return json(route, [
          {
            id: PROJECT_FILE_ID,
            logical_path: "outputs/presented-report.md",
            display_name: "presented-report.md",
            kind: "artifact",
            media_type: "text/markdown",
            size: 26,
            sha256: "project-file-sha",
            status: "ready",
            created_at: "2026-07-15T00:00:00Z",
            updated_at: "2026-07-15T00:00:00Z",
          },
        ]);
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
        options.runBodies?.push(request.postDataJSON());
        await options.streamGate;
        hasStreamed = true;
        return handleRunStream(route);
      }
      if (/\/threads\/[^/]+\/runs(?:\?|$)/u.test(request.url())) {
        return json(route, []);
      }
      return json(route, { detail: "not found" }, 404);
    },
  );
  return requests;
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
});

test("project detail loads history and streams without legacy private-work calls", async ({
  page,
}) => {
  const projectRequests = await mockPrivateWork(page);
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
  const automationLink = page.getByLabel("Automations");
  await expect(automationLink).toHaveAttribute(
    "href",
    `/projects/research-lab/automations?thread_id=${THREAD_ID}`,
  );
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
  await page.waitForTimeout(200);
  expect(legacySuggestionRequests).toEqual([]);
  expect(legacyArtifactRequests).toEqual([]);
  expect(legacyPrivateRequests).toEqual([]);
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
  await expect(page.getByText(/owner|跨项目|其他用户/iu)).toHaveCount(0);
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
  await expect(page.getByTestId("human-input-card")).toBeVisible();
  await page.getByRole("button", { name: "staging" }).click();

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
  await mockPrivateWork(page, true, { runBodies, uploadRequests });
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
