import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import type { Automation } from "@/core/project-automations/types";
import { PROJECT_AUTOMATION } from "@/core/projects/features";
import type { Capability, Project } from "@/core/projects/types";

import { mockProjectAutomationAPI } from "./utils/mock-api";

const ACCOUNT_A = "90000000-0000-4000-8000-000000000001";
const ACCOUNT_B = "90000000-0000-4000-8000-000000000002";
const PROJECT_ALPHA = "10000000-0000-4000-8000-000000000001";
const PROJECT_BETA = "10000000-0000-4000-8000-000000000002";
const AGENT_ID = "20000000-0000-4000-8000-000000000001";
const TASK_ALPHA = "40000000-0000-4000-8000-000000000001";
const TASK_BETA = "40000000-0000-4000-8000-000000000002";
const NOW = "2026-07-16T00:00:00Z";

const OWNER_CAPABILITIES: Capability[] = [
  "project.read",
  "project.enter",
  "private_work.read_own",
  "private_work.create",
  "shared_assets.read",
  "shared_assets.execute",
  "automation.manage_own",
];

function project(
  id: string,
  slug: string,
  displayName: string,
  capabilities: Capability[] = OWNER_CAPABILITIES,
  role: Project["role"] = "admin",
): Project {
  return {
    id,
    slug,
    display_name: displayName,
    description: "Automation release project",
    icon: "folder",
    role,
    capabilities,
    is_pinned: false,
    last_entered_at: null,
    member_count: 1,
    agent_count: 1,
    skill_count: 0,
    mcp_count: 0,
    status: "active",
    is_suspended: false,
    membership_version: 1,
    request_id: `request-${slug}`,
  };
}

function automation(
  id: string,
  title: string,
  status: Automation["status"] = "enabled",
): Automation {
  return {
    id,
    thread_id: null,
    context_mode: "fresh_thread_per_run",
    agent_asset_id: AGENT_ID,
    agent_scope: "project",
    title,
    prompt: `Prompt for ${title}`,
    schedule_type: "cron",
    schedule_spec: { cron: "0 9 * * *" },
    timezone: "UTC",
    status,
    next_run_at: status === "enabled" ? "2026-07-17T09:00:00Z" : null,
    last_run_at: null,
    last_outcome: null,
    last_error_code: null,
    run_count: 0,
    version: 1,
    created_at: NOW,
    updated_at: NOW,
  };
}

const ALPHA = project(PROJECT_ALPHA, "alpha", "Alpha Project");
const BETA = project(PROJECT_BETA, "beta", "Beta Project");

function ownerAccount(
  projects: Project[] = [ALPHA],
  automations: Record<string, Automation[]> = {},
) {
  return {
    id: ACCOUNT_A,
    email: "owner-a@example.test",
    projects,
    automations,
  };
}

async function createAutomation(page: Page) {
  await page.getByRole("button", { name: "创建 Automation" }).first().click();
  const dialog = page.getByRole("dialog", { name: "创建 Automation" });
  await dialog
    .getByTestId("automation-agent")
    .selectOption(`project:${AGENT_ID}`);
  await dialog.getByRole("textbox", { name: "Title" }).fill("Daily release");
  await dialog
    .getByRole("textbox", { name: "Prompt" })
    .fill("Prepare the daily project release report.");
  await dialog.getByRole("button", { name: "创建 Automation" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Daily release" }),
  ).toBeVisible();
}

test("owner lifecycle uses only project URLs for create, edit, pause, resume, manual, delete and history", async ({
  page,
}) => {
  const state = await mockProjectAutomationAPI(page, [ownerAccount()]);
  await page.goto("/projects/alpha/automations");

  await createAutomation(page);

  await page.getByRole("button", { name: "编辑" }).click();
  let dialog = page.getByRole("dialog", { name: "编辑 Automation" });
  await dialog
    .getByRole("textbox", { name: "Title" })
    .fill("Daily release edited");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Daily release edited" }),
  ).toBeVisible();

  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.getByRole("button", { name: "恢复" })).toBeVisible();
  await page.getByRole("button", { name: "恢复" }).click();
  await expect(page.getByRole("button", { name: "暂停" })).toBeVisible();

  await page.getByRole("button", { name: "立即运行" }).click();
  await expect(page.getByText("手动 · 排队中")).toBeVisible();

  await page.getByRole("button", { name: "删除" }).click();
  dialog = page.getByRole("dialog", { name: "删除 Automation" });
  await dialog.getByRole("button", { name: "确认删除" }).click();
  await expect(page.getByTestId("automation-empty")).toBeVisible();

  const writes = state.requests.filter(({ method }) => method !== "GET");
  expect(writes.map(({ method, path }) => `${method} ${path}`)).toEqual([
    `POST /api/projects/${PROJECT_ALPHA}/automations`,
    `PATCH /api/projects/${PROJECT_ALPHA}/automations/automation-1`,
    `POST /api/projects/${PROJECT_ALPHA}/automations/automation-1/pause`,
    `POST /api/projects/${PROJECT_ALPHA}/automations/automation-1/resume`,
    `POST /api/projects/${PROJECT_ALPHA}/automations/automation-1/trigger`,
    `DELETE /api/projects/${PROJECT_ALPHA}/automations/automation-1`,
  ]);
  expect(
    state.requests.some(
      ({ method, path }) =>
        method === "GET" &&
        path === `/api/projects/${PROJECT_ALPHA}/automations/automation-1/runs`,
    ),
  ).toBe(true);
  expect(writes.every(({ accountId }) => accountId === ACCOUNT_A)).toBe(true);
  expect(
    state.requestedPaths.some((path) =>
      path.startsWith("/api/scheduled-tasks"),
    ),
  ).toBe(false);
});

test("Viewer can list and read history but receives no mutation controls", async ({
  page,
}) => {
  const viewer = project(
    PROJECT_ALPHA,
    "viewer-project",
    "Viewer Project",
    ["project.read", "project.enter", "private_work.read_own"],
    "viewer",
  );
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([viewer], {
      [PROJECT_ALPHA]: [automation(TASK_ALPHA, "Viewer task")],
    }),
  ]);
  await page.goto("/projects/viewer-project/automations");

  await expect(
    page.getByRole("heading", { name: "Viewer task" }),
  ).toBeVisible();
  await expect(page.getByText("运行历史", { exact: true })).toBeVisible();
  for (const name of [
    "创建 Automation",
    "编辑",
    "暂停",
    "恢复",
    "立即运行",
    "删除",
  ]) {
    await expect(page.getByRole("button", { name })).toHaveCount(0);
  }
  expect(state.requests.every(({ method }) => method === "GET")).toBe(true);
});

test("manual retry reuses one Idempotency-Key after a safe 503 response", async ({
  page,
}) => {
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA], {
      [PROJECT_ALPHA]: [automation(TASK_ALPHA, "Retryable task")],
    }),
  ]);
  state.failNext(ACCOUNT_A, PROJECT_ALPHA, "trigger", {
    status: 503,
    code: "AUTOMATION_UNAVAILABLE",
  });
  await page.goto("/projects/alpha/automations");

  await page.getByRole("button", { name: "立即运行" }).click();
  await expect(
    page.getByText("Automation 暂时不可用，请稍后重试。"),
  ).toBeVisible();
  await page.getByRole("button", { name: "立即运行" }).click();
  await expect(page.getByText("手动 · 排队中")).toBeVisible();

  const triggers = state.requests.filter(({ path }) =>
    path.endsWith(`/${TASK_ALPHA}/trigger`),
  );
  expect(triggers).toHaveLength(2);
  const keys = triggers.map(({ headers }) => headers["idempotency-key"]);
  expect(keys[0]).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu,
  );
  expect(keys[1]).toBe(keys[0]);
});

test("scheduler disabled keeps manual execution while migration required never lists", async ({
  page,
}) => {
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA, BETA], {
      [PROJECT_ALPHA]: [automation(TASK_ALPHA, "Manual only")],
      [PROJECT_BETA]: [automation(TASK_BETA, "Migration blocked")],
    }),
  ]);
  state.setReadiness(ACCOUNT_A, PROJECT_ALPHA, {
    scheduler_enabled: false,
    scheduler_status: "disabled",
  });
  state.setReadiness(ACCOUNT_A, PROJECT_BETA, {
    status: "migration_required",
    code: "AUTOMATION_MIGRATION_REQUIRED",
    scheduler_enabled: false,
    scheduler_status: "stopped",
    project_private_work_ready: true,
    automation_cutover_ready: false,
  });

  await page.goto("/projects/alpha/automations");
  await expect(page.getByTestId("automation-scheduler-disabled")).toBeVisible();
  await page.getByRole("button", { name: "立即运行" }).click();
  await expect(page.getByText("手动 · 排队中")).toBeVisible();

  await page.goto("/projects/beta/automations");
  await expect(page.getByTestId("automation-migration-required")).toBeVisible();
  expect(
    state.requests.some(
      ({ projectId, method, path }) =>
        projectId === PROJECT_BETA &&
        method === "GET" &&
        path === `/api/projects/${PROJECT_BETA}/automations`,
    ),
  ).toBe(false);
});

test("409 refreshes the version and 429 leaves a safe explicit retry", async ({
  page,
}) => {
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA], {
      [PROJECT_ALPHA]: [automation(TASK_ALPHA, "Concurrent task")],
    }),
  ]);
  await page.goto("/projects/alpha/automations");
  await expect(page.getByText("版本 1")).toBeVisible();

  state.getAutomations(ACCOUNT_A, PROJECT_ALPHA)[0]!.version = 2;
  state.failNext(ACCOUNT_A, PROJECT_ALPHA, "update", {
    status: 409,
    code: "AUTOMATION_VERSION_CONFLICT",
  });
  await page.getByRole("button", { name: "编辑" }).click();
  const dialog = page.getByRole("dialog", { name: "编辑 Automation" });
  await dialog
    .getByRole("textbox", { name: "Title" })
    .fill("Concurrent task edited");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog.getByText("状态已更新，请刷新后重试。")).toBeVisible();
  await dialog.getByRole("button", { name: "刷新" }).click();
  await dialog
    .getByRole("textbox", { name: "Title" })
    .fill("Concurrent task edited");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog).toHaveCount(0);

  const patches = state.requests.filter(
    ({ method, path }) => method === "PATCH" && path.endsWith(TASK_ALPHA),
  );
  expect(patches.map(({ body }) => body?.expected_version)).toEqual([1, 2]);

  state.failNext(ACCOUNT_A, PROJECT_ALPHA, "pause", {
    status: 429,
    code: "AUTOMATION_CONCURRENCY_LIMIT",
  });
  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.getByText("当前并发已达上限，请稍后重试。")).toBeVisible();
  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.getByRole("button", { name: "恢复" })).toBeVisible();
});

test("direct URL without read capability is stable forbidden and sends no list", async ({
  page,
}) => {
  const forbidden = project(
    PROJECT_ALPHA,
    "forbidden",
    "Forbidden Project",
    ["project.read", "project.enter"],
    "viewer",
  );
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([forbidden]),
  ]);
  await page.goto("/projects/forbidden/automations");

  await expect(page.getByText("AUTOMATION_FORBIDDEN")).toBeVisible();
  expect(
    state.requests.some(
      ({ method, path }) =>
        method === "GET" &&
        path === `/api/projects/${PROJECT_ALPHA}/automations`,
    ),
  ).toBe(false);
});

test("project transition aborts the old list and account transition cannot reuse old cache", async ({
  page,
}) => {
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA, BETA], {
      [PROJECT_ALPHA]: [automation(TASK_ALPHA, "Late Alpha task")],
      [PROJECT_BETA]: [automation(TASK_BETA, "Account A Beta task")],
    }),
    {
      id: ACCOUNT_B,
      email: "owner-b@example.test",
      projects: [BETA],
      automations: {
        [PROJECT_BETA]: [automation(TASK_ALPHA, "Account B Beta task")],
      },
    },
  ]);
  const held = state.holdNextList(ACCOUNT_A, PROJECT_ALPHA);
  await page.goto("/projects/alpha/automations");
  await held.started;

  await page.getByRole("link", { name: "返回工作空间" }).click();
  await page
    .getByTestId("project-card")
    .filter({ hasText: "Beta Project" })
    .getByRole("link", { name: "进入项目" })
    .click();
  await page.getByRole("link", { name: "Automations" }).click();
  await expect(
    page.getByRole("heading", { name: "Account A Beta task" }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const trackedWindow = window as typeof window & {
          __projectAbortPaths?: string[];
        };
        return trackedWindow.__projectAbortPaths ?? [];
      }),
    )
    .toContain(`/api/projects/${PROJECT_ALPHA}/automations`);
  expect(
    state.requests.some(
      ({ accountId, projectId }) =>
        accountId === ACCOUNT_A && projectId === PROJECT_ALPHA,
    ),
  ).toBe(true);
  held.release();
  await expect(page.getByText("Late Alpha task", { exact: true })).toHaveCount(
    0,
  );

  state.switchAccount(ACCOUNT_B);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Account B Beta task" }),
  ).toBeVisible();
  await expect(
    page.getByText("Account A Beta task", { exact: true }),
  ).toHaveCount(0);
  expect(
    state.requests.some(
      ({ accountId, projectId }) =>
        accountId === ACCOUNT_B && projectId === PROJECT_BETA,
    ),
  ).toBe(true);
});

test("static demo gates every Automation entry and project Chat never falls back to legacy scheduled tasks", () => {
  expect(PROJECT_AUTOMATION).toBe(true);
  const chatSource = readFileSync(
    resolve(
      process.cwd(),
      "src/components/projects/private-work/project-chat-page.tsx",
    ),
    "utf8",
  );
  expect(chatSource).toContain("staticWebsiteOnly");
  expect(chatSource).toContain("/automations?thread_id=");
  expect(chatSource).not.toContain("/workspace/scheduled-tasks");
});
