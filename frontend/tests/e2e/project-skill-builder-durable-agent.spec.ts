import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";
import type { SkillBuilderSession } from "@/core/skill-builder";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const SESSION_ID = "20000000-0000-4000-8000-000000000001";
const THREAD_ID = "30000000-0000-4000-8000-000000000001";
const RUN_ID = "40000000-0000-4000-8000-000000000001";
const SKILL_ID = "50000000-0000-4000-8000-000000000001";
const SKILL_VERSION_ID = "60000000-0000-4000-8000-000000000001";
const MCP_ID = "70000000-0000-4000-8000-000000000001";
const MCP_VERSION_ID = "80000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-13T00:00:00Z";
const DRAFT_CHECKSUM = "a".repeat(64);

const skillContent = `---\nname: research-helper\ndescription: Research a topic with the project catalog.\n---\n\n# Research helper\n`;

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Skill Builder durable Agent browser coverage",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "private_work.read_own",
    "private_work.create",
    "shared_assets.read",
    "shared_assets.edit",
    "shared_assets.execute",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 1,
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

type Lifecycle = "interviewing" | "pending" | "running" | "draft_ready";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function session(lifecycle: Lifecycle): SkillBuilderSession {
  const activeRun =
    lifecycle === "pending" || lifecycle === "running"
      ? {
          runId: RUN_ID,
          status: lifecycle,
          streamUrl: `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/${RUN_ID}/stream`,
        }
      : undefined;
  const complete = lifecycle === "draft_ready";

  return {
    id: SESSION_ID,
    project_id: PROJECT_ID,
    owner_user_id: ACCOUNT_ID,
    thread_id: THREAD_ID,
    slug: "research-helper",
    display_name: "research-helper",
    status:
      lifecycle === "interviewing"
        ? "interviewing"
        : complete
          ? "draft_ready"
          : "generating",
    revision: complete ? 3 : lifecycle === "interviewing" ? 1 : 2,
    messages: complete
      ? [
          {
            id: "user-1",
            role: "user",
            content: "创建一个能检索项目资料的 Skill",
            created_at: TIMESTAMP,
          },
          {
            id: "assistant-1",
            role: "assistant",
            content: "候选文件和精确依赖已准备好。",
            created_at: TIMESTAMP,
          },
        ]
      : [],
    active_clarification: null,
    progress: complete
      ? [{ id: "draft", label: "生成候选文件", status: "completed" }]
      : [{ id: "draft", label: "生成候选文件", status: "running" }],
    files: complete
      ? [
          {
            path: "SKILL.md",
            media_type: "text/markdown",
            size_bytes: new TextEncoder().encode(skillContent).byteLength,
            sha256: "b".repeat(64),
            encoding: "utf-8",
            content: skillContent,
          },
        ]
      : [],
    draft_checksum: complete ? DRAFT_CHECKSUM : null,
    validation: null,
    error_code: null,
    error_message: null,
    created_skill_id: null,
    session_kind: "create",
    target_skill_id: null,
    base_version_id: null,
    base_version_number: null,
    base_payload_checksum: null,
    target_skill_deleted: false,
    base_files: [],
    ...(complete
      ? {
          authoring_dependencies: {
            version: 1 as const,
            draft_checksum: DRAFT_CHECKSUM,
            requirements: [
              {
                kind: "skill" as const,
                reference: "skill:system:research-catalog:v2",
                scope: "system" as const,
                skill_id: SKILL_ID,
                version_id: SKILL_VERSION_ID,
                version_number: 2,
                slug: "research-catalog",
                display_name: "Research catalog",
                payload_checksum: "c".repeat(64),
                authoring_only: true as const,
                runtime_authorized: false as const,
              },
              {
                kind: "mcp_tool" as const,
                reference: "mcp:project:knowledge-base:v4:search_documents",
                scope: "project" as const,
                mcp_server_id: MCP_ID,
                version_id: MCP_VERSION_ID,
                version_number: 4,
                server_slug: "knowledge-base",
                server_name: "Knowledge Base",
                tool_name: "search_documents",
                payload_checksum: "d".repeat(64),
                inventory_status: "ready" as const,
                inventory_error_code: null,
                last_success_at: TIMESTAMP,
                authoring_only: true as const,
                runtime_authorized: false as const,
              },
            ],
          },
        }
      : {}),
    ...(activeRun ? { activeRun } : {}),
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
  };
}

async function mockSkillBuilderDurableAgent(page: Page) {
  let lifecycle: Lifecycle = "interviewing";
  const createBodies: unknown[] = [];
  const turnBodies: unknown[] = [];
  let sessionReads = 0;
  const unexpectedRequests: string[] = [];
  const sessionsBase = `/api/projects/${PROJECT_ID}/skill-builder/sessions`;

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
      return json(route, { needs_setup: false, registration_enabled: true });
    }
    if (path === "/api/projects" && method === "GET") {
      return json(route, { items: [project], next_cursor: null });
    }
    if (path === `/api/projects/${PROJECT_ID}/enter` && method === "POST") {
      return json(route, project);
    }
    if (
      path === `/api/projects/${PROJECT_ID}/private-work/readiness` &&
      method === "GET"
    ) {
      return json(route, {
        status: "ready",
        code: "READY",
        request_id: "private-work-ready",
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
        request_id: "automations-ready",
      });
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
    if (path === sessionsBase && method === "POST") {
      createBodies.push(request.postDataJSON());
      lifecycle = "interviewing";
      return json(route, { data: session(lifecycle), request_id: "create-1" });
    }
    if (path === sessionsBase && method === "GET") {
      return json(route, {
        data: [
          {
            id: SESSION_ID,
            slug: "research-helper",
            display_name: "research-helper",
            status: session(lifecycle).status,
            revision: session(lifecycle).revision,
            updated_at: TIMESTAMP,
          },
        ],
        request_id: "sessions-1",
      });
    }
    if (path === `${sessionsBase}/${SESSION_ID}` && method === "GET") {
      sessionReads += 1;
      return json(route, {
        data: session(lifecycle),
        request_id: `session-${sessionReads}`,
      });
    }
    if (path === `${sessionsBase}/${SESSION_ID}/turns` && method === "POST") {
      turnBodies.push(request.postDataJSON());
      lifecycle = "pending";
      return json(
        route,
        {
          runId: RUN_ID,
          status: "pending",
          streamUrl: `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/${RUN_ID}/stream`,
        },
        202,
      );
    }

    unexpectedRequests.push(`${method} ${path}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return {
    createBodies,
    turnBodies,
    unexpectedRequests,
    sessionReads: () => sessionReads,
    setLifecycle(next: Lifecycle) {
      lifecycle = next;
    },
  };
}

test("Skill Builder durable Agent admits, recovers a Run after refresh, and displays candidate files directly", async ({
  page,
}) => {
  const builder = await mockSkillBuilderDurableAgent(page);

  await page.goto("/projects/alpha/skills/new");
  await page.getByLabel("Skill name").fill("Research Helper");
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page).toHaveURL(`/projects/alpha/skills/new/${SESSION_ID}`);
  expect(builder.createBodies).toEqual([
    {
      slug: "research-helper",
      display_name: "research-helper",
      idempotency_key: expect.any(String),
    },
  ]);

  const composer = page.getByLabel("Describe the Skill you want");
  await expect(composer).toBeVisible();
  await composer.fill("创建一个能检索项目资料的 Skill");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.getByTestId("skill-builder-run-activity")).toContainText(
    "Queued, waiting to run",
  );
  expect(builder.turnBodies).toEqual([
    {
      input: {
        kind: "message",
        message: "创建一个能检索项目资料的 Skill",
      },
      expected_revision: 1,
      idempotency_key: expect.any(String),
    },
  ]);

  builder.setLifecycle("running");
  const readsBeforeRefresh = builder.sessionReads();
  await page.reload();
  await expect(page.getByTestId("skill-builder-run-activity")).toContainText(
    "Running",
  );
  await expect(
    page.getByRole("heading", { name: "Candidate files" }),
  ).toHaveCount(0);
  expect(builder.sessionReads()).toBeGreaterThan(readsBeforeRefresh);

  builder.setLifecycle("draft_ready");
  await expect(
    page.getByRole("treeitem", { name: "文件 SKILL.md" }),
  ).toBeVisible({
    timeout: 8_000,
  });
  await expect(page.getByTestId("skill-builder-run-activity")).toContainText(
    "This turn completed",
  );
  await expect(
    page.getByRole("heading", { name: "Candidate files" }),
  ).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(0);
  await expect(page.getByTestId("skill-builder-dependencies")).toHaveCount(0);

  await page.getByRole("button", { name: "Close candidate files" }).click();
  await expect(
    page.getByRole("heading", { name: "Candidate files" }),
  ).toHaveCount(0);
  const filesTrigger = page.getByRole("button", {
    name: "View candidate files",
  });
  await expect(filesTrigger).toBeVisible();

  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Candidate files" }),
  ).toHaveCount(0);
  await expect(filesTrigger).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await filesTrigger.click();
  await expect(
    page.getByRole("heading", { name: "Candidate files" }),
  ).toBeVisible();
  await expect(composer).toBeHidden();
  await page.getByRole("button", { name: "Close candidate files" }).click();
  await expect(composer).toBeVisible();
  expect(builder.unexpectedRequests).toEqual([]);
});
