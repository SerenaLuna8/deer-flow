import { expect, test, type Page, type Route } from "@playwright/test";

import type { Automation } from "@/core/project-automations/types";
import type { Project } from "@/core/projects/types";

import { mockLangGraphAPI } from "./utils/mock-api";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const SECOND_PROJECT_ID = "10000000-0000-4000-8000-000000000002";
const AGENT_ID = "20000000-0000-4000-8000-000000000001";
const AGENT_VERSION_ID = "30000000-0000-4000-8000-000000000001";
const NOW = "2026-07-16T00:00:00Z";

function project(id: string, slug: string, displayName: string): Project {
  return {
    id,
    slug,
    display_name: displayName,
    description: "Automation project",
    icon: "folder",
    role: "admin",
    capabilities: [
      "project.read",
      "project.enter",
      "private_work.read_own",
      "private_work.create",
      "shared_assets.read",
      "shared_assets.execute",
      "automation.manage_own",
    ],
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

const PROJECT = project(PROJECT_ID, "research-lab", "Research Lab");
const SECOND_PROJECT = project(SECOND_PROJECT_ID, "second-lab", "Second Lab");

function automation(
  id: string,
  status: Automation["status"],
  title: string,
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

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type AutomationFixture = {
  createdBodies: Array<Record<string, unknown>>;
  triggerIds: string[];
  failUpdate: boolean;
  failDelete: boolean;
  failTrigger: boolean;
};

async function installAutomationFixture(
  page: Page,
  initial: Automation[],
): Promise<AutomationFixture> {
  mockLangGraphAPI(page);
  const state: AutomationFixture = {
    createdBodies: [],
    triggerIds: [],
    failUpdate: false,
    failDelete: false,
    failTrigger: false,
  };
  const projects = [PROJECT, SECOND_PROJECT];
  const automationsByProject = new Map<string, Automation[]>([
    [PROJECT_ID, [...initial]],
    [SECOND_PROJECT_ID, []],
  ]);

  await page.route("**/api/v1/auth/me", (route) =>
    json(route, {
      id: ACCOUNT_ID,
      email: "owner@example.test",
      system_role: "user",
      needs_setup: false,
    }),
  );
  await page.route(/\/api\/projects(?:\?.*)?$/, (route) =>
    json(route, { items: projects, next_cursor: null }),
  );
  await page.route("**/api/projects/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const currentProject = projects.find((item) =>
      path.startsWith(`/api/projects/${item.id}/`),
    );
    if (!currentProject) return json(route, { detail: "not found" }, 404);
    const current = automationsByProject.get(currentProject.id)!;
    const base = `/api/projects/${currentProject.id}/automations`;

    if (path.endsWith("/enter")) return json(route, currentProject);
    if (path.endsWith("/private-work/readiness")) {
      return json(route, {
        status: "ready",
        code: "PRIVATE_WORK_READY",
        request_id: `private-ready-${currentProject.slug}`,
      });
    }
    if (path.endsWith("/agents") && method === "GET") {
      return json(route, {
        system_items: [],
        project_items: [
          {
            id: AGENT_ID,
            scope: "project",
            project_id: currentProject.id,
            slug: "automation-agent",
            display_name: "Automation Agent",
            status: "active",
            current_published_version_id: AGENT_VERSION_ID,
            version: 1,
            created_by_user_id: ACCOUNT_ID,
            created_at: NOW,
            updated_at: NOW,
            capabilities: ["shared_assets.read", "shared_assets.execute"],
            binding: null,
          },
        ],
        request_id: `agents-${currentProject.slug}`,
      });
    }
    if (path === `${base}/readiness` && method === "GET") {
      return json(route, {
        status: "ready",
        code: "AUTOMATION_READY",
        scheduler_enabled: true,
        scheduler_status: "running",
        project_private_work_ready: true,
        automation_cutover_ready: true,
        request_id: `automation-ready-${currentProject.slug}`,
      });
    }
    if (path === base && method === "GET") {
      return json(route, { items: current });
    }
    if (path === base && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.createdBodies.push(body);
      const created: Automation = {
        id: `created-${state.createdBodies.length}`,
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
        created_at: NOW,
        updated_at: NOW,
      };
      current.push(created);
      return json(route, created, 201);
    }

    const suffix = path.slice(base.length + 1);
    const [taskId, action] = suffix.split("/");
    const selected = current.find(({ id }) => id === taskId);
    if (!selected) return json(route, { detail: "not found" }, 404);

    if (action === "runs" && method === "GET") {
      return json(route, { items: [] });
    }
    if (action === "trigger" && method === "POST") {
      state.triggerIds.push(taskId!);
      if (state.failTrigger) {
        return json(
          route,
          {
            detail: {
              code: "AUTOMATION_VERSION_CONFLICT",
              message: "internal trigger details",
              request_id: "trigger-conflict",
            },
          },
          409,
        );
      }
      return json(route, {
        id: `run-${taskId}`,
        automation_id: taskId,
        automation_version: selected.version,
        scheduled_for: NOW,
        trigger: "manual",
        status: "queued",
        thread_id: null,
        run_id: null,
        error_code: null,
        started_at: null,
        finished_at: null,
        created_at: NOW,
        updated_at: NOW,
      });
    }
    if (method === "PATCH") {
      if (state.failUpdate) {
        return json(
          route,
          {
            detail: {
              code: "AUTOMATION_VERSION_CONFLICT",
              message: "internal update details",
              request_id: "update-conflict",
            },
          },
          409,
        );
      }
      return json(route, selected);
    }
    if (method === "DELETE") {
      if (state.failDelete) {
        return json(
          route,
          {
            detail: {
              code: "AUTOMATION_VERSION_CONFLICT",
              message: "internal delete details",
              request_id: "delete-conflict",
            },
          },
          409,
        );
      }
      return json(route, { id: taskId, deleted: true });
    }
    return json(route, { detail: "not found" }, 404);
  });

  return state;
}

test("recipe schedule stays synchronized with visible controls and submission", async ({
  page,
}) => {
  const state = await installAutomationFixture(page, []);
  await page.goto("/projects/research-lab/automations");

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
  expect(state.createdBodies[0]).toEqual(
    expect.objectContaining({
      schedule_spec: { cron: "0 9 * * *" },
      timezone: dailyTimezone,
    }),
  );

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
  expect(state.createdBodies[1]).toEqual(
    expect.objectContaining({
      title: "Custom weekly report",
      schedule_spec: { cron: "15 6 * * 2" },
      timezone: customTimezone,
    }),
  );
});

test("manual trigger is available only for enabled and paused automations", async ({
  page,
}) => {
  const tasks = [
    automation("enabled-task", "enabled", "Enabled task"),
    automation("paused-task", "paused", "Paused task"),
    automation("completed-task", "completed", "Completed task"),
    automation("failed-task", "failed", "Failed task"),
    automation("cancelled-task", "cancelled", "Cancelled task"),
  ];
  const state = await installAutomationFixture(page, tasks);
  await page.goto("/projects/research-lab/automations");

  for (const title of ["Enabled task", "Paused task"]) {
    await page.getByRole("button", { name: new RegExp(title) }).click();
    await page.getByRole("button", { name: "立即运行" }).click();
  }
  await expect
    .poll(() => state.triggerIds)
    .toEqual(["enabled-task", "paused-task"]);

  for (const title of ["Completed task", "Failed task", "Cancelled task"]) {
    await page.getByRole("button", { name: new RegExp(title) }).click();
    await expect(page.getByRole("button", { name: "立即运行" })).toHaveCount(0);
  }
  expect(state.triggerIds).toEqual(["enabled-task", "paused-task"]);
});

test("failed dialog feedback clears on close, action change, and project change", async ({
  page,
}) => {
  const state = await installAutomationFixture(page, [
    automation("enabled-task", "enabled", "Lifecycle task"),
  ]);
  state.failUpdate = true;
  state.failDelete = true;
  state.failTrigger = true;
  await page.goto("/projects/research-lab/automations");

  await page.getByRole("button", { name: "编辑" }).click();
  let dialog = page.getByRole("dialog", { name: "编辑 Automation" });
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
    .filter({ hasText: SECOND_PROJECT.display_name })
    .getByRole("link", { name: "进入项目" })
    .click();
  await page.goto("/projects/second-lab/automations");
  await expect(page.getByText("状态已更新，请刷新后重试。")).toHaveCount(0);
});
