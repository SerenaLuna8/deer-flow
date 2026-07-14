import { expect, test, type Page, type Route } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

const SYSTEM_SKILL = {
  id: "20000000-0000-4000-8000-000000000001",
  scope: "system",
  project_id: null,
  slug: "paper-review",
  display_name: "Paper Review",
  status: "active",
  current_published_version_id: "20000000-0000-4000-8000-000000000002",
  version: 1,
  created_by_user_id: "system-admin",
  created_at: "2026-07-13T00:00:00Z",
  updated_at: "2026-07-13T00:00:00Z",
};

async function mockSystemSkillCatalog(
  page: Page,
  handler: (route: Route) => Promise<void> | void = (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [SYSTEM_SKILL],
        request_id: "catalog-skills",
      }),
    }),
) {
  await page.route("**/api/assets/catalog/skills", handler);
}

async function mockOrdinaryUser(page: Page) {
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "member",
        email: "member@test.local",
        system_role: "user",
        needs_setup: false,
      }),
    }),
  );
}

test("legacy Skills page uses the read-only PostgreSQL system catalog", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  let catalogRequests = 0;
  await mockSystemSkillCatalog(page, async (route) => {
    catalogRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [SYSTEM_SKILL],
        request_id: "catalog-skills",
      }),
    });
  });

  await page.goto("/workspace/skills");

  await expect(page.getByText("系统 Skill", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText(SYSTEM_SKILL.display_name)).toBeVisible();
  await expect(page.getByText(SYSTEM_SKILL.slug)).toBeVisible();
  await expect.poll(() => catalogRequests).toBe(1);

  await expect(page.getByRole("switch")).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /view .*skill\.md|create|edit|enable/i }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "前往平台资产管理" }),
  ).toHaveAttribute("href", "/admin/assets");
});

test("ordinary users can view system Skills without a management entry", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockOrdinaryUser(page);
  await mockSystemSkillCatalog(page);

  await page.goto("/workspace/skills");

  await expect(page.getByText(SYSTEM_SKILL.display_name)).toBeVisible({
    timeout: 15_000,
  });
  const refreshResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/auth/me" &&
      response.request().method() === "GET",
  );
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await refreshResponse;
  await expect(
    page.getByRole("link", { name: "前往平台资产管理" }),
  ).toHaveCount(0);
  await expect(page.getByRole("switch")).toHaveCount(0);
});

test("the compatibility view distinguishes catalog loading from empty", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  let pendingRoute: Route | null = null;
  await mockSystemSkillCatalog(page, (route) => {
    pendingRoute = route;
  });

  await page.goto("/workspace/skills");

  await expect(page.locator('[data-slot="skeleton"]')).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText("暂无系统 Skill。")).toHaveCount(0);
  expect(pendingRoute).not.toBeNull();

  await (pendingRoute as unknown as Route).fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      items: [SYSTEM_SKILL],
      request_id: "catalog-skills-loaded",
    }),
  });
  await expect(page.getByText(SYSTEM_SKILL.display_name)).toBeVisible();
  await expect(page.locator('[data-slot="skeleton"]')).toHaveCount(0);
});

test("catalog failures render a safe compatibility error", async ({ page }) => {
  mockLangGraphAPI(page);
  await mockSystemSkillCatalog(page, (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        detail: {
          code: "asset_storage_unavailable",
          message: "internal storage detail must not be shown",
          request_id: "catalog-skills-error",
        },
      }),
    }),
  );

  await page.goto("/workspace/skills");

  await expect(page.locator('p[role="alert"]')).toHaveText(
    "系统资产暂时无法加载，请稍后重试。",
    { timeout: 15_000 },
  );
  await expect(
    page.getByText("internal storage detail must not be shown"),
  ).toHaveCount(0);
});
