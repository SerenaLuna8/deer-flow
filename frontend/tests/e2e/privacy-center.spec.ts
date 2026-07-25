import { Buffer } from "node:buffer";

import { expect, test, type Route } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const JOB_ID = "70000000-0000-4000-8000-000000000001";

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

test("privacy center exports retained data and queues exact scoped early deletion", async ({
  page,
  context,
}) => {
  mockLangGraphAPI(page);
  await page.route("**/api/v1/auth/me", (route) =>
    json(route, {
      id: ACCOUNT_ID,
      email: "former-member@example.test",
      system_role: "user",
      needs_setup: false,
    }),
  );

  let earlyDeleteRequested = false;
  const privacyRequests: Array<{
    method: string;
    path: string;
    body: string | null;
  }> = [];
  const unrelatedPrivateRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/privacy/")) {
      privacyRequests.push({
        method: request.method(),
        path,
        body: request.postData(),
      });
    }
    if (path.includes("/private-work") || path.startsWith("/api/threads")) {
      unrelatedPrivateRequests.push(`${request.method()} ${path}`);
    }
  });

  await context.route("**/api/privacy/cases", async (route) => {
    const request = route.request();
    expect(request.method()).toBe("GET");
    await json(route, [
      {
        project_id: PROJECT_ID,
        project_slug: "former-project",
        project_display_name: "Former Project",
        project_icon: "folder",
        membership_status: "left",
        retention_kind: "former_owner",
        deletion_deadline: "2026-08-21T08:00:00Z",
        early_delete_requested: earlyDeleteRequested,
      },
    ]);
  });
  await context.route(
    `**/api/privacy/cases/${PROJECT_ID}/early-delete`,
    async (route) => {
      expect(route.request().method()).toBe("POST");
      earlyDeleteRequested = true;
      await json(
        route,
        { project_id: PROJECT_ID, job_id: JOB_ID, status: "queued" },
        202,
      );
    },
  );

  // Chromium promotes attachment navigations to downloads before ordinary
  // Playwright routing can fulfill them. Fulfill this one exact GET at the
  // browser network layer so the real anchor/download path stays deterministic.
  const cdp = await context.newCDPSession(page);
  await cdp.send("Fetch.enable", {
    patterns: [
      {
        urlPattern: `*/api/privacy/cases/${PROJECT_ID}/export`,
        requestStage: "Request",
      },
    ],
  });
  let resolveExportFulfillment!: () => void;
  let rejectExportFulfillment!: (reason?: unknown) => void;
  const exportFulfilled = new Promise<void>((resolve, reject) => {
    resolveExportFulfillment = resolve;
    rejectExportFulfillment = reject;
  });
  cdp.on("Fetch.requestPaused", ({ requestId, request }) => {
    const path = new URL(request.url).pathname;
    expect(request.method).toBe("GET");
    expect(path).toBe(`/api/privacy/cases/${PROJECT_ID}/export`);
    privacyRequests.push({ method: request.method, path, body: null });
    void cdp
      .send("Fetch.fulfillRequest", {
        requestId,
        responseCode: 200,
        responseHeaders: [
          { name: "Content-Type", value: "application/x-ndjson" },
          {
            name: "Content-Disposition",
            value: 'attachment; filename="former-project-privacy.ndjson"',
          },
        ],
        body: Buffer.from(
          `${JSON.stringify({ type: "manifest", project_id: PROJECT_ID })}\n`,
        ).toString("base64"),
      })
      .then(resolveExportFulfillment, rejectExportFulfillment);
  });

  await page.goto("/workspace/privacy");
  await expect(
    page.getByRole("heading", { name: "个人数据中心" }),
  ).toBeVisible();
  const privacyCase = page.getByTestId(`privacy-case-${PROJECT_ID}`);
  await expect(privacyCase).toContainText("Former Project");

  const downloadStarted = page.waitForEvent("download");
  await privacyCase.getByRole("button", { name: "导出我的数据" }).click();
  const download = await downloadStarted;
  expect(download.suggestedFilename()).toBe("former-project-privacy.ndjson");
  await exportFulfilled;
  await cdp.send("Fetch.disable");
  await expect(page.getByRole("status")).toContainText(
    "流式数据导出已开始下载",
  );

  await privacyCase.getByRole("button", { name: "提前删除" }).click();
  const deleteDialog = page.getByRole("dialog", {
    name: "确认提前删除个人数据",
  });
  const confirmation = deleteDialog.getByLabel(/输入项目名称/u);
  await confirmation.fill("Wrong Project");
  await expect(
    deleteDialog.getByRole("button", { name: "永久删除我的数据" }),
  ).toBeDisabled();
  await confirmation.fill("Former Project");
  await deleteDialog.getByRole("button", { name: "永久删除我的数据" }).click();

  await expect(deleteDialog).toHaveCount(0);
  await expect(page.getByRole("status")).toContainText("删除请求已提交");
  await expect(
    privacyCase.getByRole("button", { name: "删除请求已提交" }),
  ).toBeDisabled();

  expect(privacyRequests).toEqual([
    { method: "GET", path: "/api/privacy/cases", body: null },
    {
      method: "GET",
      path: `/api/privacy/cases/${PROJECT_ID}/export`,
      body: null,
    },
    {
      method: "POST",
      path: `/api/privacy/cases/${PROJECT_ID}/early-delete`,
      body: null,
    },
    { method: "GET", path: "/api/privacy/cases", body: null },
  ]);
  expect(unrelatedPrivateRequests).toEqual([]);
});
