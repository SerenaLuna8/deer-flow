import { expect, test, type Page, type Route } from "@playwright/test";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "90000000-0000-4000-8000-000000000002";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockProjectQuotaAdmin(page: Page) {
  const unexpectedRequests: string[] = [];
  let quotaPatchCount = 0;

  await page.route("**/api/**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/auth/me" && method === "GET") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "admin@example.test",
        username: "admin",
        system_role: "system_admin",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status" && method === "GET") {
      return json(route, {
        needs_setup: false,
        registration_enabled: true,
      });
    }
    if (
      path === `/api/admin/projects/${PROJECT_ID}/usage` &&
      method === "GET"
    ) {
      return json(route, {
        policy: {
          version: 0,
          configured: {
            member_limit: null,
            storage_bytes_limit: null,
            concurrent_run_limit: null,
            mcp_calls_daily_limit: null,
          },
          effective: {
            member_limit: 20,
            storage_bytes_limit: 10_737_418_240,
            concurrent_run_limit: 4,
            mcp_calls_daily_limit: 10_000,
          },
        },
        dimensions: [
          {
            dimension: "members",
            bucket: "lifetime",
            used: 1,
            reserved: 0,
            limit: 20,
            warning_threshold_reached: false,
          },
          {
            dimension: "storage_bytes",
            bucket: "lifetime",
            used: 0,
            reserved: 0,
            limit: 10_737_418_240,
            warning_threshold_reached: false,
          },
          {
            dimension: "concurrent_runs",
            bucket: "lifetime",
            used: 0,
            reserved: 0,
            limit: 4,
            warning_threshold_reached: false,
          },
          {
            dimension: "mcp_calls_daily",
            bucket: "2026-08-17",
            used: 0,
            reserved: 0,
            limit: 10_000,
            warning_threshold_reached: false,
          },
        ],
      });
    }
    if (
      path === `/api/admin/projects/${PROJECT_ID}/usage/limits` &&
      method === "PATCH"
    ) {
      quotaPatchCount += 1;
      return json(
        route,
        {
          code: "RELIABILITY_INVALID",
          message: "Reliability request is invalid.",
          request_id: "request-invalid-quota",
        },
        422,
      );
    }

    unexpectedRequests.push(`${method} ${path}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return {
    unexpectedRequests,
    quotaPatchCount: () => quotaPatchCount,
  };
}

test("project quota widening names the inherited platform ceiling", async ({
  page,
  baseURL,
}) => {
  await page.context().addCookies([
    {
      name: "locale",
      value: "zh-CN",
      url: baseURL ?? "http://localhost:3000",
    },
  ]);
  const mocked = await mockProjectQuotaAdmin(page);

  await page.goto(`/admin/projects/${PROJECT_ID}/assets/quotas`);
  await page.getByLabel("成员").fill("21");
  await page.getByRole("button", { name: "保存配额" }).click();

  await expect(page.locator("form").getByRole("alert")).toHaveText(
    "成员不能超过平台上限 20。项目配额只能比平台上限更严格或相等。",
  );
  expect(mocked.quotaPatchCount()).toBe(0);
  expect(mocked.unexpectedRequests).toEqual([]);
});
