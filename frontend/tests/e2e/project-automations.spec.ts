import { expect, test, type Page } from "@playwright/test";

import type { Automation } from "@/core/project-automations/types";
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

test("recipe schedule stays synchronized with visible controls and submission", async ({
  page,
}) => {
  const state = await mockProjectAutomationAPI(page, [ownerAccount()]);
  await page.goto("/projects/alpha/automations");

  await page.getByRole("button", { name: "创建 Automation" }).first().click();
  let dialog = page.getByRole("dialog", { name: "创建 Automation" });
  await dialog
    .getByTestId("automation-agent")
    .selectOption(`project:${AGENT_ID}`);
  await dialog.getByRole("button", { name: /GitHub Trending/ }).click();
  await expect(dialog.getByTestId("schedule-preset")).toContainText("Daily");
  const dailyTimezone = (
    await dialog.getByTestId("schedule-timezone").innerText()
  ).trim();
  expect(dailyTimezone).not.toBe("");

  await dialog.getByRole("button", { name: "创建 Automation" }).click();
  await expect(dialog).toHaveCount(0);

  await page.getByRole("button", { name: "创建 Automation" }).click();
  dialog = page.getByRole("dialog", { name: "创建 Automation" });
  await dialog
    .getByTestId("automation-agent")
    .selectOption(`project:${AGENT_ID}`);
  await dialog.getByRole("button", { name: /每周项目报告/ }).click();
  await expect(dialog.getByTestId("schedule-preset")).toContainText("Weekly");

  await dialog.getByTestId("schedule-preset").click();
  await page.getByRole("option", { name: "Custom cron" }).click();
  await dialog
    .getByRole("textbox", { name: "Cron expression" })
    .fill("15 6 * * 2");
  await dialog
    .getByRole("textbox", { name: "Title" })
    .fill("Custom weekly report");
  await expect(dialog.getByTestId("schedule-preset")).toContainText(
    "Custom cron",
  );
  await expect(
    dialog.getByRole("textbox", { name: "Cron expression" }),
  ).toHaveValue("15 6 * * 2");
  const customTimezone = (
    await dialog.getByTestId("schedule-timezone").innerText()
  ).trim();

  await dialog.getByRole("button", { name: "创建 Automation" }).click();
  await expect(dialog).toHaveCount(0);

  const creates = state.requests.filter(
    ({ method, path }) =>
      method === "POST" &&
      path === `/api/projects/${PROJECT_ALPHA}/automations`,
  );
  expect(creates).toHaveLength(2);
  expect(creates[0]).toEqual(
    expect.objectContaining({
      accountId: ACCOUNT_A,
      projectId: PROJECT_ALPHA,
      body: expect.objectContaining({
        schedule_spec: { cron: "0 9 * * *" },
        timezone: dailyTimezone,
      }),
    }),
  );
  expect(creates[1]).toEqual(
    expect.objectContaining({
      accountId: ACCOUNT_A,
      projectId: PROJECT_ALPHA,
      body: expect.objectContaining({
        title: "Custom weekly report",
        schedule_spec: { cron: "15 6 * * 2" },
        timezone: customTimezone,
      }),
    }),
  );
});

test("manual trigger is available only for enabled and paused automations", async ({
  page,
}) => {
  const tasks = [
    automation("enabled-task", "Enabled task", "enabled"),
    automation("paused-task", "Paused task", "paused"),
    automation("completed-task", "Completed task", "completed"),
    automation("failed-task", "Failed task", "failed"),
    automation("cancelled-task", "Cancelled task", "cancelled"),
  ];
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA], { [PROJECT_ALPHA]: tasks }),
  ]);
  await page.goto("/projects/alpha/automations");

  for (const title of ["Enabled task", "Paused task"]) {
    await page.getByRole("button", { name: new RegExp(title) }).click();
    await page.getByRole("button", { name: "立即运行" }).click();
  }
  await expect
    .poll(() =>
      state.requests
        .filter(
          ({ method, path }) => method === "POST" && path.endsWith("/trigger"),
        )
        .map(({ path }) => path),
    )
    .toEqual([
      `/api/projects/${PROJECT_ALPHA}/automations/enabled-task/trigger`,
      `/api/projects/${PROJECT_ALPHA}/automations/paused-task/trigger`,
    ]);

  for (const title of ["Completed task", "Failed task", "Cancelled task"]) {
    await page.getByRole("button", { name: new RegExp(title) }).click();
    await expect(page.getByRole("button", { name: "立即运行" })).toHaveCount(0);
  }
  expect(
    state.requests
      .filter(
        ({ method, path }) => method === "POST" && path.endsWith("/trigger"),
      )
      .every(
        ({ accountId, projectId }) =>
          accountId === ACCOUNT_A && projectId === PROJECT_ALPHA,
      ),
  ).toBe(true);
});

test("failed dialog feedback clears on close, action change, and project change", async ({
  page,
}) => {
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA, BETA], {
      [PROJECT_ALPHA]: [automation(TASK_ALPHA, "Lifecycle task")],
    }),
  ]);
  state.failNext(ACCOUNT_A, PROJECT_ALPHA, "update", {
    status: 409,
    code: "AUTOMATION_VERSION_CONFLICT",
  });
  state.failNext(ACCOUNT_A, PROJECT_ALPHA, "delete", {
    status: 409,
    code: "AUTOMATION_VERSION_CONFLICT",
  });
  state.failNext(ACCOUNT_A, PROJECT_ALPHA, "trigger", {
    status: 409,
    code: "AUTOMATION_VERSION_CONFLICT",
  });
  await page.goto("/projects/alpha/automations");

  await page.getByRole("button", { name: "编辑" }).click();
  let dialog = page.getByRole("dialog", { name: "编辑 Automation" });
  await dialog
    .getByRole("textbox", { name: "Title" })
    .fill("Lifecycle task edited");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog.getByText("状态已更新，请刷新后重试。")).toBeVisible();
  await dialog.getByRole("button", { name: "取消" }).click();

  await page.getByRole("button", { name: "创建 Automation" }).click();
  dialog = page.getByRole("dialog", { name: "创建 Automation" });
  await expect(dialog.getByText("状态已更新，请刷新后重试。")).toHaveCount(0);
  await dialog.getByRole("button", { name: "取消" }).click();

  await page.getByRole("button", { name: "删除" }).click();
  dialog = page.getByRole("dialog", { name: "删除 Automation" });
  await dialog.getByRole("button", { name: "确认删除" }).click();
  await expect(dialog.getByText("状态已更新，请刷新后重试。")).toBeVisible();
  await dialog.getByRole("button", { name: "取消" }).click();

  await page.getByRole("button", { name: "编辑" }).click();
  dialog = page.getByRole("dialog", { name: "编辑 Automation" });
  await expect(dialog.getByText("状态已更新，请刷新后重试。")).toHaveCount(0);
  await dialog.getByRole("button", { name: "取消" }).click();

  await page.getByRole("button", { name: "立即运行" }).click();
  await expect(page.getByText("状态已更新，请刷新后重试。")).toBeVisible();
  await page.getByRole("link", { name: "返回工作空间" }).click();
  await page
    .getByTestId("project-card")
    .filter({ hasText: BETA.display_name })
    .getByRole("link", { name: "进入项目" })
    .click();
  await page.getByRole("link", { name: "Automations" }).click();
  await expect(page.getByText("状态已更新，请刷新后重试。")).toHaveCount(0);
  await expect(page.getByTestId("automation-empty")).toBeVisible();
});

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

test("near-execution once title edit submits a sparse PATCH", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-07-16T13:50:30Z") });
  const runAt = "2026-07-16T13:51:00Z";
  const nearOnce: Automation = {
    ...automation(TASK_ALPHA, "Near once"),
    schedule_type: "once",
    schedule_spec: { run_at: runAt },
    timezone: "UTC",
    next_run_at: runAt,
  };
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA], { [PROJECT_ALPHA]: [nearOnce] }),
  ]);
  await page.goto("/projects/alpha/automations");

  await page.getByRole("button", { name: "编辑" }).click();
  const dialog = page.getByRole("dialog", { name: "编辑 Automation" });
  await dialog.getByRole("textbox", { name: "Title" }).fill("Near once edited");
  await dialog.getByRole("button", { name: "保存修改" }).click();

  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Near once edited" }),
  ).toBeVisible();
  const patch = state.requests.find(
    ({ method, path }) =>
      method === "PATCH" &&
      path === `/api/projects/${PROJECT_ALPHA}/automations/${TASK_ALPHA}`,
  );
  expect(patch?.body).toEqual({
    expected_version: 1,
    title: "Near once edited",
  });
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

test("scheduler disabled keeps manual execution while unavailable schema never lists", async ({
  page,
}) => {
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA, BETA], {
      [PROJECT_ALPHA]: [automation(TASK_ALPHA, "Manual only")],
      [PROJECT_BETA]: [automation(TASK_BETA, "Schema blocked")],
    }),
  ]);
  state.setReadiness(ACCOUNT_A, PROJECT_ALPHA, {
    scheduler_enabled: false,
    scheduler_status: "disabled",
  });
  state.setReadiness(ACCOUNT_A, PROJECT_BETA, {
    status: "unavailable",
    code: "AUTOMATION_UNAVAILABLE",
    scheduler_enabled: false,
    scheduler_status: "stopped",
    project_private_work_ready: false,
    schema_ready: false,
  });

  await page.goto("/projects/alpha/automations");
  await expect(page.getByTestId("automation-scheduler-disabled")).toBeVisible();
  await page.getByRole("button", { name: "立即运行" }).click();
  await expect(page.getByText("手动 · 排队中")).toBeVisible();

  await page.goto("/projects/beta/automations");
  await expect(page.getByTestId("automation-unavailable")).toBeVisible();
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
  await expect(page.getByText("版本 2")).toBeVisible();
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

test("direct URL without read capability uses not-found and the API rejects an explicit probe", async ({
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

  await expect(page.getByText("This page could not be found.")).toBeVisible();
  const automationApiBase = `/api/projects/${PROJECT_ALPHA}/automations`;
  expect(
    state.requestedPaths.some((path) => path.startsWith(automationApiBase)),
  ).toBe(false);
  expect(
    state.requests.some(
      ({ method, path }) => method === "GET" && path === automationApiBase,
    ),
  ).toBe(false);

  const probe = await page.evaluate(async (projectId) => {
    const response = await fetch(`/api/projects/${projectId}/automations`);
    return { status: response.status, body: await response.text() };
  }, PROJECT_ALPHA);
  expect(probe.status).toBe(403);
  expect(probe.body).not.toContain("items");
  expect(probe.body).not.toContain("private mock detail");
});

test("project transition aborts the old Automation list", async ({ page }) => {
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA, BETA], {
      [PROJECT_ALPHA]: [automation(TASK_ALPHA, "Late Alpha task")],
      [PROJECT_BETA]: [automation(TASK_BETA, "Account A Beta task")],
    }),
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
});

test("AuthProvider account transition aborts the old list and cannot reuse its cache", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-07-16T00:00:00Z") });
  const state = await mockProjectAutomationAPI(page, [
    ownerAccount([ALPHA], {
      [PROJECT_ALPHA]: [automation(TASK_ALPHA, "Account A Alpha task")],
    }),
    {
      id: ACCOUNT_B,
      email: "owner-b@example.test",
      projects: [ALPHA],
      automations: {
        [PROJECT_ALPHA]: [automation(TASK_BETA, "Account B Alpha task")],
      },
    },
  ]);
  await page.goto("/projects/alpha/automations");
  await expect(
    page.getByRole("heading", { name: "Account A Alpha task" }),
  ).toBeVisible();

  const accountARefresh = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/auth/me" &&
      response.request().method() === "GET",
  );
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  expect((await accountARefresh).status()).toBe(200);
  await expect(
    page.getByText("owner-a@example.test", { exact: true }).first(),
  ).toBeVisible();

  await page.getByRole("link", { name: "项目概览" }).click();
  await expect(page.getByTestId("project-home")).toBeVisible();
  const held = state.holdNextList(ACCOUNT_A, PROJECT_ALPHA);
  await page.getByRole("link", { name: "Automations" }).click();
  await held.started;
  await expect(
    page.getByRole("heading", { name: "Account A Alpha task" }),
  ).toBeVisible();
  await page.evaluate(() => {
    const trackedWindow = window as typeof window & {
      __projectAbortPaths?: string[];
    };
    trackedWindow.__projectAbortPaths = [];
  });

  state.switchAccount(ACCOUNT_B);
  await page.clock.fastForward(61_000);
  const accountBRefresh = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/auth/me" &&
      response.request().method() === "GET",
  );
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  const accountBResponse = await accountBRefresh;
  expect((await accountBResponse.json()).id).toBe(ACCOUNT_B);
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

  held.release();
  await expect(
    page.getByText("owner-b@example.test", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Account B Alpha task" }),
  ).toBeVisible();
  await expect(
    page.getByText("Account A Alpha task", { exact: true }),
  ).toHaveCount(0);
  expect(
    state.requests.some(
      ({ accountId, projectId }) =>
        accountId === ACCOUNT_B && projectId === PROJECT_ALPHA,
    ),
  ).toBe(true);
});
