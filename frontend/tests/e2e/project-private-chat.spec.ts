import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

import { handleRunStream, mockLangGraphAPI } from "./utils/mock-api";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const THREAD_ID = "20000000-0000-4000-8000-000000000001";
const MISSING_THREAD_ID = "20000000-0000-4000-8000-000000000099";
const AGENT_ID = "30000000-0000-4000-8000-000000000001";
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
  mockLangGraphAPI(page, { suggestionsEnabled: true });
  await mockProjectContext(page);
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
  await expect(page.getByRole("button", { name: "新建对话" })).toBeDisabled();

  await page.goto(`/projects/research-lab/chats/${MISSING_THREAD_ID}`);
  await expect(
    page.getByRole("heading", { name: "找不到这个对话" }),
  ).toBeVisible();
  await expect(page.getByText(/owner|跨项目|其他用户/iu)).toHaveCount(0);
});

test("project artifact tool calls never open or request the legacy artifact surface", async ({
  page,
}) => {
  const legacyArtifactRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith(`/api/threads/${THREAD_ID}/artifacts`)) {
      legacyArtifactRequests.push(`${request.method()} ${path}`);
    }
  });
  await mockPrivateWork(page, true, {
    stateMessages: projectArtifactMessages,
    stateArtifacts: [WRITE_ARTIFACT_PATH, PRESENTED_ARTIFACT_PATH],
  });

  await page.goto(`/projects/research-lab/chats/${THREAD_ID}`);
  await expect(
    page.getByText(WRITE_ARTIFACT_PATH, { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("presented-report.md")).toHaveCount(0);
  await page.getByText(WRITE_ARTIFACT_PATH, { exact: true }).click();
  await page.waitForTimeout(200);

  await expect(page.locator("#artifacts")).toHaveCount(0);
  await expect(page.locator('iframe[title="Artifact preview"]')).toHaveCount(0);
  expect(legacyArtifactRequests).toEqual([]);
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
