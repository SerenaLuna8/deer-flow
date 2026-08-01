import { expect, test, type Page, type Route } from "@playwright/test";

import type {
  AgentBuilderBlueprint,
  AgentBuilderSession,
} from "@/core/agent-builder";
import type { Project } from "@/core/projects/types";
import type { AssetVersion, ProjectAssetItem } from "@/core/shared-assets";

import { mockLangGraphAPI } from "./utils/mock-api";

const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const SESSION_ID = "20000000-0000-4000-8000-000000000001";
const THREAD_ID = "30000000-0000-4000-8000-000000000001";
const AGENT_ID = "40000000-0000-4000-8000-000000000001";
const VERSION_ID = "50000000-0000-4000-8000-000000000001";
const NOW = "2026-07-26T09:00:00Z";
const BLUEPRINT_CHECKSUM = "builder-blueprint-checksum-v1";

const project: Project = {
  id: PROJECT_ID,
  slug: "research-lab",
  display_name: "Research Lab",
  description: "Shared research",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "project.pin",
    "shared_assets.read",
    "shared_assets.execute",
    "shared_assets.edit",
    "shared_assets.manage_bindings",
    "private_work.create",
    "private_work.read_own",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 2,
  agent_count: 0,
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
  request_id: "request-project",
};

const blueprint: AgentBuilderBlueprint = {
  description: "A pragmatic testing Agent that produces executable test cases.",
  model_ref: "default",
  tool_groups: ["file", "shell"],
  skill_version_ids: [],
  mcp_version_ids: [],
  agents_instructions:
    "# Working rules\n\nWrite focused tests first, run them, and report exact evidence.",
  soul: "# Soul\n\nPragmatic, direct, and calm under failing tests.",
  identity:
    "# Identity\n\nYou are a test engineer responsible for regression safety.",
  user_context:
    "# User context\n\nThe user prefers concise evidence and reproducible commands.",
};

function session(
  slug: string,
  overrides: Partial<AgentBuilderSession> = {},
): AgentBuilderSession {
  return {
    id: SESSION_ID,
    project_id: PROJECT_ID,
    owner_user_id: "default",
    thread_id: THREAD_ID,
    slug,
    display_name: slug,
    status: "interviewing",
    revision: 1,
    blueprint: null,
    blueprint_checksum: null,
    messages: [],
    active_clarification: null,
    progress: [
      {
        id: "understand",
        label: "理解用途与边界",
        status: "pending",
      },
    ],
    error_code: null,
    error_message: null,
    created_agent_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function createdAgentSummary(slug: string) {
  return {
    id: AGENT_ID,
    scope: "project" as const,
    project_id: PROJECT_ID,
    slug,
    display_name: slug,
    status: "suspended" as const,
    current_published_version_id: VERSION_ID,
    version: 1,
    created_by_user_id: "default",
    created_at: NOW,
    updated_at: NOW,
  };
}

function createdAgentItem(slug: string): ProjectAssetItem {
  return {
    ...createdAgentSummary(slug),
    description: blueprint.description,
    capabilities: [
      "shared_assets.read",
      "shared_assets.execute",
      "shared_assets.edit",
      "shared_assets.manage_bindings",
    ],
    binding: null,
  };
}

function createdAgentVersion(): Extract<AssetVersion, { agent_id: string }> {
  return {
    id: VERSION_ID,
    agent_id: AGENT_ID,
    version_number: 1,
    workflow_status: "published",
    description: blueprint.description,
    agents_instructions: blueprint.agents_instructions,
    soul: blueprint.soul,
    identity: blueprint.identity,
    user_context: blueprint.user_context,
    payload_schema_version: 2,
    model_ref: blueprint.model_ref,
    tool_groups: blueprint.tool_groups,
    skill_version_ids: blueprint.skill_version_ids,
    mcp_version_ids: blueprint.mcp_version_ids,
    supersedes_version_id: null,
    payload_checksum: "agent-version-payload-checksum",
    created_by_user_id: "default",
    created_at: NOW,
  };
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type BuilderMockState = {
  builderProjectIds: string[];
  createRequests: Array<Record<string, unknown>>;
  turnRequests: Array<Record<string, unknown>>;
  commitRequests: Array<Record<string, unknown>>;
  cancelRequests: Array<Record<string, unknown>>;
  directAgentCreates: number;
};

async function mockAgentBuilder(page: Page): Promise<BuilderMockState> {
  mockLangGraphAPI(page);

  const state: BuilderMockState = {
    builderProjectIds: [],
    createRequests: [],
    turnRequests: [],
    commitRequests: [],
    cancelRequests: [],
    directAgentCreates: 0,
  };
  let currentSession: AgentBuilderSession | null = null;
  let committedAgent: ProjectAssetItem | null = null;
  const version = createdAgentVersion();
  const builderBase = `/api/projects/${PROJECT_ID}/agent-builder/sessions`;

  await page.route(/\/api\/projects(?:\/.*)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path === "/api/projects" && method === "GET") {
      await json(route, { items: [project], next_cursor: null });
      return;
    }
    if (path === `/api/projects/${PROJECT_ID}/enter` && method === "POST") {
      await json(route, project);
      return;
    }

    const builderMatch =
      /^\/api\/projects\/([^/]+)\/agent-builder\/sessions(?:\/|$)/u.exec(path);
    if (builderMatch?.[1]) {
      state.builderProjectIds.push(builderMatch[1]);
    }

    if (path === builderBase && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.createRequests.push(body);
      const slug = String(body.slug);
      currentSession = session(slug);
      await json(route, {
        data: currentSession,
        request_id: "request-builder-create",
      });
      return;
    }
    if (path === builderBase && method === "GET") {
      const resumable =
        currentSession &&
        !["completed", "cancelled"].includes(currentSession.status)
          ? [
              {
                id: currentSession.id,
                slug: currentSession.slug,
                display_name: currentSession.display_name,
                status: currentSession.status,
                updated_at: currentSession.updated_at,
              },
            ]
          : [];
      await json(route, {
        data: resumable,
        request_id: "request-builder-list",
      });
      return;
    }
    if (
      path === `${builderBase}/${SESSION_ID}` &&
      method === "GET" &&
      currentSession
    ) {
      await json(route, {
        data: currentSession,
        request_id: "request-builder-get",
      });
      return;
    }
    if (
      path === `${builderBase}/${SESSION_ID}/turns` &&
      method === "POST" &&
      currentSession
    ) {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.turnRequests.push(body);
      const input = body.input as { kind?: string; message?: string };
      currentSession = session(currentSession.slug, {
        status: "proposal_ready",
        revision: 2,
        blueprint,
        blueprint_checksum: BLUEPRINT_CHECKSUM,
        messages: [
          {
            id: "message-user-1",
            role: "user",
            content: input.message ?? "",
            created_at: NOW,
          },
          {
            id: "message-assistant-1",
            role: "assistant",
            content: "设计稿已生成。请检查四项设置，确认后再创建 Agent。",
            created_at: NOW,
          },
        ],
        progress: [
          {
            id: "understand",
            label: "理解用途与边界",
            status: "completed",
          },
          {
            id: "documents",
            label: "生成四项 Agent 设置",
            status: "completed",
          },
        ],
      });
      await json(route, {
        data: currentSession,
        request_id: "request-builder-turn",
      });
      return;
    }
    if (
      path === `${builderBase}/${SESSION_ID}/commit` &&
      method === "POST" &&
      currentSession
    ) {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.commitRequests.push(body);
      committedAgent = createdAgentItem(currentSession.slug);
      currentSession = session(currentSession.slug, {
        status: "completed",
        revision: 3,
        blueprint,
        blueprint_checksum: BLUEPRINT_CHECKSUM,
        messages: currentSession.messages,
        progress: currentSession.progress,
        created_agent_id: AGENT_ID,
      });
      await json(route, {
        data: {
          session: currentSession,
          agent: createdAgentSummary(currentSession.slug),
        },
        request_id: "request-builder-commit",
      });
      return;
    }
    if (
      path === `${builderBase}/${SESSION_ID}/cancel` &&
      method === "POST" &&
      currentSession
    ) {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.cancelRequests.push(body);
      currentSession = session(currentSession.slug, {
        status: "cancelled",
        revision: currentSession.revision + 1,
        messages: currentSession.messages,
        progress: currentSession.progress,
      });
      await json(route, {
        data: currentSession,
        request_id: "request-builder-cancel",
      });
      return;
    }

    if (path === `/api/projects/${PROJECT_ID}/agents` && method === "POST") {
      state.directAgentCreates += 1;
      await json(
        route,
        { detail: "Direct Agent creation is not allowed" },
        409,
      );
      return;
    }
    if (
      path === `/api/projects/${PROJECT_ID}/default-agent` &&
      method === "GET"
    ) {
      await json(route, {
        agent_asset_id: null,
        revision: 0,
        request_id: "request-default-agent",
      });
      return;
    }
    if (path === `/api/projects/${PROJECT_ID}/agents` && method === "GET") {
      await json(route, {
        system_items: [],
        project_items: committedAgent ? [committedAgent] : [],
        request_id: "request-agents",
      });
      return;
    }
    if (
      path === `/api/projects/${PROJECT_ID}/agents/${AGENT_ID}/versions` &&
      method === "GET"
    ) {
      await json(route, {
        data: [version],
        request_id: "request-agent-versions",
      });
      return;
    }
    if (
      (path === `/api/projects/${PROJECT_ID}/skills` ||
        path === `/api/projects/${PROJECT_ID}/mcp-servers`) &&
      method === "GET"
    ) {
      await json(route, {
        system_items: [],
        project_items: [],
        request_id: "request-empty-dependencies",
      });
      return;
    }

    await route.fallback();
  });

  return state;
}

test("Agent Builder creates a suspended Agent from four generated documents", async ({
  page,
}) => {
  const mock = await mockAgentBuilder(page);

  await page.goto("/projects/research-lab/agents/new");
  await page.getByLabel("Agent 名称").fill("Code Tester");
  await page.getByRole("button", { name: "继续", exact: true }).click();

  await expect(page).toHaveURL(
    new RegExp(`/projects/research-lab/agents/new/${SESSION_ID}$`, "u"),
  );
  expect(mock.createRequests).toHaveLength(1);
  expect(mock.createRequests[0]).toMatchObject({
    slug: "code-tester",
    display_name: "code-tester",
  });
  expect(mock.directAgentCreates).toBe(0);

  await page
    .getByLabel("描述想要的 Agent")
    .fill("创建一个测试工程师，先写测试、运行回归并报告精确证据。");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(
    page.getByRole("heading", { name: "Agent 设计稿" }),
  ).toBeVisible();
  const progress = page.getByLabel("四项 Agent 设置进度");
  for (const filename of ["AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md"]) {
    await expect(progress.getByText(filename, { exact: true })).toBeVisible();
    await expect(
      page
        .getByLabel("Agent 指令文件")
        .getByRole("button", { name: filename, exact: true }),
    ).toBeVisible();
  }
  await expect(page.getByText("Write focused tests first")).toBeVisible();
  await page.getByRole("button", { name: "SOUL.md", exact: true }).click();
  await expect(page.getByText("Pragmatic, direct")).toBeVisible();
  await page.getByRole("button", { name: "IDENTITY.md", exact: true }).click();
  await expect(page.getByText("test engineer")).toBeVisible();
  await page.getByRole("button", { name: "USER.md", exact: true }).click();
  await expect(page.getByText("concise evidence")).toBeVisible();
  await expect(page.getByText("创建后默认停用，需手动启用")).toBeVisible();
  expect(mock.turnRequests).toHaveLength(1);
  expect(mock.turnRequests[0]).toMatchObject({
    input: {
      kind: "message",
      message: "创建一个测试工程师，先写测试、运行回归并报告精确证据。",
    },
    expected_revision: 1,
  });
  expect(mock.directAgentCreates).toBe(0);

  await page.getByRole("button", { name: "创建 Agent" }).click();

  await expect(page).toHaveURL(
    new RegExp(`/projects/research-lab/agents\\?agent_id=${AGENT_ID}$`, "u"),
  );
  const detail = page.getByRole("dialog", {
    name: "code-tester",
    exact: true,
  });
  await expect(detail.locator(`time[datetime="${NOW}"]`)).toBeVisible();
  await expect(detail.getByText("版本 1", { exact: true })).toHaveCount(0);
  await expect(
    detail.getByText("运行配置版本", { exact: true }),
  ).toHaveCount(0);
  await detail.getByRole("button", { name: "Close" }).click();
  const card = page.getByRole("listitem").filter({ hasText: "code-tester" });
  await expect(card.getByText("已停用", { exact: true })).toBeVisible();
  await expect(
    card.getByRole("button", { name: "启用 code-tester" }),
  ).toBeVisible();
  await expect(
    card.getByRole("button", {
      name: "与 code-tester 对话，该 Agent 当前不可用",
    }),
  ).toBeDisabled();

  expect(mock.commitRequests).toHaveLength(1);
  expect(mock.commitRequests[0]).toMatchObject({
    expected_revision: 2,
    expected_blueprint_checksum: BLUEPRINT_CHECKSUM,
  });
  expect(mock.directAgentCreates).toBe(0);
  expect(new Set(mock.builderProjectIds)).toEqual(new Set([PROJECT_ID]));
});

test("Agent Builder cancellation stays scoped and removes the resumable draft", async ({
  page,
}) => {
  const mock = await mockAgentBuilder(page);

  await page.goto("/projects/research-lab/agents/new");
  await page.getByLabel("Agent 名称").fill("Draft Agent");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await expect(page).toHaveURL(
    new RegExp(`/projects/research-lab/agents/new/${SESSION_ID}$`, "u"),
  );

  await page.getByRole("button", { name: "更多操作" }).click();
  await page
    .getByRole("menuitem", { name: "放弃本次设计", exact: true })
    .click();
  await page.getByRole("button", { name: "确认放弃" }).click();

  await expect(page).toHaveURL("/projects/research-lab/agents");
  await expect(
    page.getByRole("heading", { name: "继续设计未完成的 Agent" }),
  ).toHaveCount(0);
  await expect(page.getByText("暂无项目自建的 Agent。")).toBeVisible();

  expect(mock.cancelRequests).toHaveLength(1);
  expect(mock.cancelRequests[0]).toMatchObject({
    expected_revision: 1,
  });
  expect(mock.commitRequests).toHaveLength(0);
  expect(mock.directAgentCreates).toBe(0);
  expect(new Set(mock.builderProjectIds)).toEqual(new Set([PROJECT_ID]));
});
