import { expect, test, type Page } from "@playwright/test";

import { MOCK_THREAD_ID, mockLangGraphAPI } from "./utils/mock-api";

test.describe.configure({ mode: "serial" });

const LONG_TOKEN = `long-token-${"x".repeat(320)}`;

async function expectNoHorizontalOverflow(page: Page) {
  const dimensions = await page.evaluate(() => {
    const workbench = document.querySelector(
      '[data-testid="scheduled-task-workbench"]',
    );
    if (!(workbench instanceof HTMLElement)) {
      throw new Error("Scheduled task workbench not found");
    }
    return {
      document: {
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      },
      body: {
        clientWidth: document.body.clientWidth,
        scrollWidth: document.body.scrollWidth,
      },
      scrollContainer: {
        clientWidth: document.querySelector("main")?.clientWidth ?? 0,
        scrollWidth: document.querySelector("main")?.scrollWidth ?? 0,
      },
      workbench: {
        clientWidth: workbench.clientWidth,
        scrollWidth: workbench.scrollWidth,
      },
    };
  });

  expect(dimensions.document.scrollWidth).toBeLessThanOrEqual(
    dimensions.document.clientWidth,
  );
  expect(dimensions.body.scrollWidth).toBeLessThanOrEqual(
    dimensions.body.clientWidth,
  );
  expect(dimensions.scrollContainer.clientWidth).toBeGreaterThan(0);
  expect(dimensions.scrollContainer.scrollWidth).toBeLessThanOrEqual(
    dimensions.scrollContainer.clientWidth,
  );
  expect(dimensions.workbench.scrollWidth).toBeLessThanOrEqual(
    dimensions.workbench.clientWidth,
  );
}

test("scheduled tasks page is reachable from sidebar", async ({ page }) => {
  mockLangGraphAPI(page, {
    threads: [],
    scheduledTasks: [
      {
        id: "task-1",
        thread_id: "thread-1",
        title: "Daily summary",
        prompt: "Summarize thread",
        schedule_type: "cron",
        schedule_spec: { cron: "0 9 * * *" },
        timezone: "UTC",
        status: "enabled",
        next_run_at: "2026-07-02T01:00:00+00:00",
        last_run_at: null,
        last_run_id: null,
        last_error: null,
        run_count: 0,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
    ],
  });

  await page.goto("/workspace/chats/new");
  await page.getByRole("link", { name: /scheduled tasks/i }).click();
  await page.waitForURL("**/workspace/scheduled-tasks");
  await expect(page).toHaveURL(/workspace\/scheduled-tasks/);
  await expect(
    page.getByRole("button", { name: /Daily summary/i }),
  ).toBeVisible();
  await expect(page.getByTestId("scheduled-task-list")).toBeVisible();
  await expect(page.getByTestId("scheduled-task-detail")).toBeVisible();
  await expect(page.getByTestId("scheduled-task-runs")).toContainText("0 runs");
});

test("workbench exposes semantic state and the task's total run count", async ({
  page,
}) => {
  mockLangGraphAPI(page, {
    threads: [],
    scheduledTasks: [
      {
        id: "task-total",
        thread_id: "thread-total",
        context_mode: "reuse_thread",
        title: "Daily total",
        prompt: "Summarize all activity",
        schedule_type: "cron",
        schedule_spec: { cron: "0 9 * * *" },
        timezone: "UTC",
        status: "enabled",
        next_run_at: "2026-07-02T01:00:00+00:00",
        last_run_at: "2026-07-01T01:00:00+00:00",
        last_run_id: "run-latest",
        last_error: null,
        run_count: 57,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
      {
        id: "task-once",
        thread_id: null,
        context_mode: "fresh_thread_per_run",
        title: "One-time cleanup",
        prompt: "Clean up once",
        schedule_type: "once",
        schedule_spec: { run_at: "2026-07-03T01:00:00+00:00" },
        timezone: "UTC",
        status: "paused",
        next_run_at: "2026-07-03T01:00:00+00:00",
        last_run_at: null,
        last_run_id: null,
        last_error: null,
        run_count: 0,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
    ],
  });
  await page.route("**/api/scheduled-tasks/*/runs", (route) => {
    const taskId = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").at(-2) ?? "",
    );
    const runs =
      taskId === "task-total"
        ? [
            {
              id: "history-1",
              task_id: taskId,
              thread_id: "thread-total",
              run_id: "run-1",
              scheduled_for: "2026-07-01T01:00:00+00:00",
              trigger: "scheduled",
              status: "success",
              error: null,
              started_at: "2026-07-01T01:00:00+00:00",
              finished_at: "2026-07-01T01:01:00+00:00",
              created_at: "2026-07-01T01:00:00+00:00",
            },
            {
              id: "history-2",
              task_id: taskId,
              thread_id: "thread-total",
              run_id: "run-2",
              scheduled_for: "2026-06-30T01:00:00+00:00",
              trigger: "manual",
              status: "failed",
              error: "Previous failure",
              started_at: "2026-06-30T01:00:00+00:00",
              finished_at: "2026-06-30T01:01:00+00:00",
              created_at: "2026-06-30T01:00:00+00:00",
            },
          ]
        : [];
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(runs),
    });
  });

  await page.goto("/workspace/scheduled-tasks");

  const list = page.getByTestId("scheduled-task-list");
  await expect(list).toHaveAttribute(
    "aria-labelledby",
    "scheduled-task-list-heading",
  );
  await expect(
    list.getByRole("heading", { level: 2, name: "Scheduled tasks" }),
  ).toBeAttached();

  const selectedTask = page.getByTestId("scheduled-task-item-task-total");
  await expect(selectedTask).toHaveAttribute("aria-current", "true");
  await expect(selectedTask).toHaveAttribute(
    "aria-controls",
    "scheduled-task-detail-panel",
  );
  await expect(
    selectedTask.locator('[data-slot="badge"]').filter({
      hasText: "Recurring",
    }),
  ).toBeVisible();
  await expect(
    selectedTask.locator('[data-slot="badge"]').filter({ hasText: "Enabled" }),
  ).toBeVisible();

  const detail = page.getByTestId("scheduled-task-detail");
  await expect(detail).toHaveAttribute("id", "scheduled-task-detail-panel");
  await expect(detail).toHaveAttribute(
    "aria-labelledby",
    "scheduled-task-detail-heading",
  );
  await expect(
    detail.getByRole("heading", { level: 2, name: "Daily total" }),
  ).toBeVisible();
  await expect(
    detail.locator('[data-slot="badge"]').filter({ hasText: "Enabled" }),
  ).toBeVisible();
  await expect(page.getByTestId("scheduled-task-run-count")).toHaveText("57");

  const runs = page.getByTestId("scheduled-task-runs");
  await expect(runs).toHaveAttribute(
    "aria-labelledby",
    "scheduled-task-runs-heading",
  );
  await expect(
    runs.getByRole("heading", { level: 3, name: "57 runs" }),
  ).toBeVisible();
  await expect(
    page.getByTestId("scheduled-task-run-list").locator(":scope > div"),
  ).toHaveCount(2);

  const statusGroup = page.getByRole("group", { name: "All statuses" });
  await expect(
    statusGroup.getByRole("button", { name: "All statuses" }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    statusGroup.getByRole("button", { name: "Enabled", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");
  await statusGroup
    .getByRole("button", { name: "Enabled", exact: true })
    .click();
  await expect(
    statusGroup.getByRole("button", { name: "Enabled", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await statusGroup.getByRole("button", { name: "All statuses" }).click();

  const typeGroup = page.getByRole("group", { name: "All types" });
  await expect(
    typeGroup.getByRole("button", { name: "All types" }),
  ).toHaveAttribute("aria-pressed", "true");
  await typeGroup.getByRole("button", { name: "Once" }).click();
  await expect(typeGroup.getByRole("button", { name: "Once" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(
    page.getByTestId("scheduled-task-item-task-once"),
  ).toHaveAttribute("aria-current", "true");
  await expect(selectedTask).toHaveCount(0);

  await page.getByTestId("scheduled-task-create-trigger").click();
  const contextGroup = page.getByRole("group", { name: "Context mode" });
  await expect(
    contextGroup.getByRole("button", { name: "Fresh thread" }),
  ).toHaveAttribute("aria-pressed", "true");
  await contextGroup.getByRole("button", { name: "Reuse thread" }).click();
  await expect(
    contextGroup.getByRole("button", { name: "Reuse thread" }),
  ).toHaveAttribute("aria-pressed", "true");
});

test("desktop workbench keeps the list and detail near a 36/64 split", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  mockLangGraphAPI(page, {
    threads: [],
    scheduledTasks: [
      {
        id: "task-layout",
        thread_id: "thread-layout",
        title: "Layout task",
        prompt: "Check desktop layout",
        schedule_type: "cron",
        schedule_spec: { cron: "0 9 * * *" },
        timezone: "UTC",
        status: "enabled",
        next_run_at: "2026-07-02T01:00:00+00:00",
        last_run_at: null,
        last_run_id: null,
        last_error: null,
        run_count: 0,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
    ],
  });

  await page.goto("/workspace/scheduled-tasks");
  const listBox = await page.getByTestId("scheduled-task-list").boundingBox();
  const detailBox = await page
    .getByTestId("scheduled-task-detail")
    .boundingBox();
  expect(listBox).not.toBeNull();
  expect(detailBox).not.toBeNull();
  if (!listBox || !detailBox) {
    throw new Error("Scheduled task panels were not measurable");
  }

  const combinedWidth = listBox.width + detailBox.width;
  expect(Math.abs(listBox.width / combinedWidth - 0.36)).toBeLessThan(0.03);
  expect(Math.abs(detailBox.width / combinedWidth - 0.64)).toBeLessThan(0.03);
  expect(Math.abs(listBox.y - detailBox.y)).toBeLessThan(2);
});

test("mobile workbench contains long tokens in light and dark themes", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const longThreadId = `thread-${LONG_TOKEN}`;
  mockLangGraphAPI(page, {
    threads: [],
    scheduledTasks: [
      {
        id: "task-overflow",
        thread_id: longThreadId,
        context_mode: "reuse_thread",
        title: `title-${LONG_TOKEN}`,
        prompt: `prompt-${LONG_TOKEN}`,
        schedule_type: "cron",
        schedule_spec: { cron: "0 9 * * *" },
        timezone: "UTC",
        status: "failed",
        next_run_at: "2026-07-02T01:00:00+00:00",
        last_run_at: "2026-07-01T01:00:00+00:00",
        last_run_id: `last-run-${LONG_TOKEN}`,
        last_error: `last-error-${LONG_TOKEN}`,
        run_count: 1,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
    ],
  });
  await page.route("**/api/scheduled-tasks/*/runs", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: "history-overflow",
          task_id: "task-overflow",
          thread_id: `thread-${LONG_TOKEN}`,
          run_id: `run-${LONG_TOKEN}`,
          scheduled_for: "2026-07-01T01:00:00+00:00",
          trigger: "scheduled",
          status: "failed",
          error: `run-error-${LONG_TOKEN}`,
          started_at: "2026-07-01T01:00:00+00:00",
          finished_at: "2026-07-01T01:01:00+00:00",
          created_at: "2026-07-01T01:00:00+00:00",
        },
      ]),
    }),
  );

  await page.goto(
    `/workspace/scheduled-tasks?thread_id=${encodeURIComponent(longThreadId)}`,
  );
  const list = page.getByTestId("scheduled-task-list");
  const detail = page.getByTestId("scheduled-task-detail");
  await expect(list).toBeVisible();
  await expect(detail).toBeVisible();

  const listBox = await list.boundingBox();
  const detailBox = await detail.boundingBox();
  expect(listBox).not.toBeNull();
  expect(detailBox).not.toBeNull();
  if (!listBox || !detailBox) {
    throw new Error("Scheduled task panels were not measurable");
  }
  expect(detailBox.y).toBeGreaterThan(listBox.y + listBox.height);
  await expectNoHorizontalOverflow(page);

  await page.evaluate(() => document.documentElement.classList.add("dark"));
  await expect(page.locator("html")).toHaveClass(/dark/);
  await expect(list).toBeVisible();
  await expect(detail).toBeVisible();
  const failedBadge = detail
    .locator('[data-slot="badge"]')
    .filter({ hasText: "Failed" });
  await expect(failedBadge).toBeVisible();
  const badgeColors = await failedBadge.evaluate((badge) => {
    const styles = getComputedStyle(badge);
    return {
      background: styles.backgroundColor,
      foreground: styles.color,
    };
  });
  expect(badgeColors.background).not.toBe("rgba(0, 0, 0, 0)");
  expect(badgeColors.foreground).not.toBe("rgba(0, 0, 0, 0)");
  expect(badgeColors.foreground).not.toBe("transparent");
  expect(badgeColors.foreground).not.toBe(badgeColors.background);
  await expectNoHorizontalOverflow(page);
});

test("thread page links to filtered scheduled tasks", async ({ page }) => {
  mockLangGraphAPI(page, {
    threads: [
      {
        thread_id: MOCK_THREAD_ID,
        title: "Thread with schedules",
        updated_at: "2025-06-01T12:00:00Z",
      },
    ],
    scheduledTasks: [
      {
        id: "task-1",
        thread_id: MOCK_THREAD_ID,
        title: "Thread task",
        prompt: "Summarize thread",
        schedule_type: "cron",
        schedule_spec: { cron: "0 9 * * *" },
        timezone: "UTC",
        status: "enabled",
        next_run_at: "2026-07-02T01:00:00+00:00",
        last_run_at: null,
        last_run_id: null,
        last_error: null,
        run_count: 0,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
    ],
  });

  await page.goto(`/workspace/chats/${MOCK_THREAD_ID}`);
  await page
    .locator("header")
    .getByRole("link", { name: /scheduled tasks/i })
    .click();
  await page.waitForURL(new RegExp(`thread_id=${MOCK_THREAD_ID}`));
});

test("create sheet supports keyboard controls and restores focus", async ({
  page,
}) => {
  mockLangGraphAPI(page, { threads: [], scheduledTasks: [] });

  await page.goto("/workspace/scheduled-tasks");
  const createTrigger = page.getByTestId("scheduled-task-create-trigger");
  await createTrigger.focus();
  await expect(createTrigger).toBeFocused();

  await page.keyboard.press("Enter");
  const createSheet = page.getByRole("dialog", {
    name: "Create scheduled task",
  });
  await expect(createSheet).toBeVisible();
  await expect(createTrigger).toHaveAttribute("aria-haspopup", "dialog");
  await expect(createTrigger).toHaveAttribute("aria-expanded", "true");

  const focusIsInsideSheet = () =>
    createSheet.evaluate((dialog) => dialog.contains(document.activeElement));
  await expect.poll(focusIsInsideSheet).toBe(true);

  const focusableControls = createSheet.locator(
    'button:not([disabled]):visible, input:not([disabled]):visible, textarea:not([disabled]):visible, [tabindex]:not([tabindex="-1"]):visible',
  );
  const firstFocusableControl = focusableControls.first();
  const lastFocusableControl = focusableControls.last();
  await lastFocusableControl.focus();
  await page.keyboard.press("Tab");
  await expect(firstFocusableControl).toBeFocused();
  await firstFocusableControl.focus();
  await page.keyboard.press("Shift+Tab");
  await expect(lastFocusableControl).toBeFocused();

  const createForm = createSheet.getByTestId("scheduled-task-create-form");
  await createForm.getByPlaceholder("Task title").fill("Preserved draft");
  await createForm.getByPlaceholder("Prompt").fill("Preserved prompt");
  await createSheet.getByRole("button", { name: "Close" }).click();
  await expect(createSheet).toHaveCount(0);
  await expect(createTrigger).toBeFocused();

  await createTrigger.click();
  await expect(createSheet).toBeVisible();
  await expect(createForm.getByPlaceholder("Task title")).toHaveValue(
    "Preserved draft",
  );
  await expect(createForm.getByPlaceholder("Prompt")).toHaveValue(
    "Preserved prompt",
  );

  await page.keyboard.press("Escape");
  await expect(createSheet).toHaveCount(0);
  await expect(createTrigger).toBeFocused();
  await expect(createTrigger).toHaveAttribute("aria-expanded", "false");
});

test("user can create a scheduled task from the page", async ({ page }) => {
  mockLangGraphAPI(page, { threads: [], scheduledTasks: [] });

  await page.goto("/workspace/scheduled-tasks");
  await expect(page.getByTestId("scheduled-task-create-form")).toHaveCount(0);
  const createTrigger = page.getByTestId("scheduled-task-create-trigger");
  await createTrigger.click();

  const createSheet = page.getByRole("dialog", {
    name: "Create scheduled task",
  });
  await expect(createSheet).toBeVisible();
  const createForm = createSheet.getByTestId("scheduled-task-create-form");
  const initialTimezone = (
    await createForm.getByTestId("schedule-timezone").innerText()
  ).trim();
  await createForm.getByRole("button", { name: "Reuse thread" }).click();
  await createForm.getByPlaceholder("Thread ID").fill("thread-create-target");
  await createForm.getByRole("button", { name: "One-time" }).click();
  await createForm.getByTestId("schedule-timezone").click();
  await page
    .getByRole("option", { name: "America/New_York", exact: true })
    .click();
  await createForm.getByLabel("Run at").fill("2026-07-02T09:00");
  await createForm.getByPlaceholder("Task title").fill("Created from UI");
  await createForm.getByPlaceholder("Prompt").fill("Summarize thread");
  const createRequestPromise = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/scheduled-tasks",
  );
  await createForm.getByRole("button", { name: "Create" }).click();
  const createRequest = await createRequestPromise;
  expect(createRequest.postDataJSON()).toEqual({
    context_mode: "reuse_thread",
    thread_id: "thread-create-target",
    title: "Created from UI",
    prompt: "Summarize thread",
    schedule_type: "once",
    schedule_spec: { run_at: "2026-07-02T13:00:00+00:00" },
    timezone: "America/New_York",
  });
  await expect(
    page.getByRole("button", { name: /Created from UI/i }),
  ).toBeVisible();
  await expect(
    page.getByTestId("scheduled-task-detail").getByText("Summarize thread"),
  ).toBeVisible();
  await expect(createSheet).toHaveCount(0);
  await expect(createTrigger).toBeFocused();

  await page.keyboard.press("Enter");
  await expect(createSheet).toBeVisible();
  await expect(createForm.getByPlaceholder("Task title")).toHaveValue("");
  await expect(createForm.getByPlaceholder("Prompt")).toHaveValue("");
  await expect(createForm.getByPlaceholder("Thread ID")).toHaveCount(0);
  await expect(createForm.getByLabel("Run at")).toHaveCount(0);
  await expect(createForm.getByTestId("schedule-preset")).toContainText(
    "Daily",
  );
  await expect(createForm.getByLabel("Time")).toHaveValue("09:00");
  await expect(createForm.getByTestId("schedule-timezone")).toContainText(
    initialTimezone,
  );
  await createForm.getByRole("button", { name: "Reuse thread" }).click();
  await expect(createForm.getByPlaceholder("Thread ID")).toHaveValue("");
});

test("failed creation keeps the sheet open and preserves input", async ({
  page,
}) => {
  mockLangGraphAPI(page, { threads: [], scheduledTasks: [] });
  await page.route("**/api/scheduled-tasks", (route) => {
    if (route.request().method() === "POST") {
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Create failed" }),
      });
    }
    return route.fallback();
  });

  await page.goto("/workspace/scheduled-tasks");
  await page.getByTestId("scheduled-task-create-trigger").click();
  const createSheet = page.getByRole("dialog", {
    name: "Create scheduled task",
  });
  const createForm = createSheet.getByTestId("scheduled-task-create-form");
  await createForm.getByRole("button", { name: "Reuse thread" }).click();
  await createForm.getByPlaceholder("Thread ID").fill("thread-kept");
  await createForm.getByRole("button", { name: "One-time" }).click();
  await createForm.getByLabel("Run at").fill("2026-07-02T09:00");
  await createForm.getByPlaceholder("Task title").fill("Keep this title");
  await createForm.getByPlaceholder("Prompt").fill("Keep this prompt");
  const failedResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/scheduled-tasks") &&
      response.request().method() === "POST",
  );

  await createForm.getByRole("button", { name: "Create" }).click();
  expect((await failedResponse).status()).toBe(500);
  await expect(createSheet).toBeVisible();
  await expect(createForm.getByPlaceholder("Task title")).toHaveValue(
    "Keep this title",
  );
  await expect(createForm.getByPlaceholder("Prompt")).toHaveValue(
    "Keep this prompt",
  );
  await expect(createForm.getByPlaceholder("Thread ID")).toHaveValue(
    "thread-kept",
  );
  await expect(createForm.getByLabel("Run at")).toHaveValue("2026-07-02T09:00");
  await expect(
    page
      .locator("[data-sonner-toast]")
      .filter({ hasText: "Failed to create scheduled task: Create failed" }),
  ).toBeVisible();
});

test("user can pause a scheduled task from the detail pane", async ({
  page,
}) => {
  mockLangGraphAPI(page, {
    threads: [],
    scheduledTasks: [
      {
        id: "task-1",
        thread_id: "thread-1",
        title: "Pausable task",
        prompt: "Summarize thread",
        schedule_type: "cron",
        schedule_spec: { cron: "0 9 * * *" },
        timezone: "UTC",
        status: "enabled",
        next_run_at: "2026-07-02T01:00:00+00:00",
        last_run_at: null,
        last_run_id: null,
        last_error: null,
        run_count: 0,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
    ],
  });

  await page.goto("/workspace/scheduled-tasks");
  const detail = page.getByTestId("scheduled-task-detail");
  await detail.getByRole("button", { name: "Pause" }).click();
  await expect(page.getByTestId("scheduled-task-item-task-1")).toBeVisible();
  await expect(
    page.getByTestId("scheduled-task-item-task-1").getByText(/paused/i),
  ).toBeVisible();
});

test("trigger shows a run entry in the detail pane", async ({ page }) => {
  mockLangGraphAPI(page, {
    threads: [],
    scheduledTasks: [
      {
        id: "task-1",
        thread_id: "thread-1",
        title: "Triggerable task",
        prompt: "Summarize thread",
        schedule_type: "cron",
        schedule_spec: { cron: "0 9 * * *" },
        timezone: "UTC",
        status: "enabled",
        next_run_at: "2026-07-02T01:00:00+00:00",
        last_run_at: null,
        last_run_id: null,
        last_error: null,
        run_count: 0,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
    ],
  });

  await page.goto("/workspace/scheduled-tasks");
  await page.getByRole("button", { name: "Trigger now" }).click();
  await expect(
    page.getByTestId("scheduled-task-run-list").getByText(/Manual · Success/i),
  ).toBeVisible();
});

test("detail pane falls back to a visible task after filters hide the selected task", async ({
  page,
}) => {
  mockLangGraphAPI(page, {
    threads: [],
    scheduledTasks: [
      {
        id: "task-enabled",
        thread_id: "thread-1",
        title: "Enabled task",
        prompt: "Enabled prompt",
        schedule_type: "cron",
        schedule_spec: { cron: "0 9 * * *" },
        timezone: "UTC",
        status: "enabled",
        next_run_at: "2026-07-02T01:00:00+00:00",
        last_run_at: null,
        last_run_id: null,
        last_error: null,
        run_count: 0,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
      {
        id: "task-paused",
        thread_id: "thread-2",
        title: "Paused task",
        prompt: "Paused prompt",
        schedule_type: "cron",
        schedule_spec: { cron: "0 10 * * *" },
        timezone: "UTC",
        status: "paused",
        next_run_at: "2026-07-02T02:00:00+00:00",
        last_run_at: null,
        last_run_id: null,
        last_error: null,
        run_count: 0,
        created_at: "2026-07-01T00:00:00+00:00",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
    ],
  });

  await page.goto("/workspace/scheduled-tasks");
  await page.getByTestId("scheduled-task-item-task-paused").click();
  await expect(
    page.getByTestId("scheduled-task-detail").getByText("Paused task"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Enabled", exact: true }).click();

  await expect(
    page.getByTestId("scheduled-task-detail").getByText("Enabled task"),
  ).toBeVisible();
  await expect(
    page.getByTestId("scheduled-task-item-task-enabled"),
  ).toBeVisible();
  await expect(page.getByTestId("scheduled-task-item-task-paused")).toHaveCount(
    0,
  );
});
