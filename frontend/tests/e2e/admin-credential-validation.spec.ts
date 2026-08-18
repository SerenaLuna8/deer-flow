import { expect, test, type Page, type Route } from "@playwright/test";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockCredentialAdmin(page: Page) {
  const unexpectedRequests: string[] = [];
  let credentialPostCount = 0;

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
    if (path === "/api/admin/assets/credentials" && method === "GET") {
      return json(route, { items: [], request_id: "request-credentials" });
    }
    if (path === "/api/admin/assets/credentials" && method === "POST") {
      credentialPostCount += 1;
      return json(
        route,
        {
          detail: {
            code: "asset_validation_failed",
            message: "Asset validation failed",
            request_id: "request-invalid-credential",
          },
        },
        422,
      );
    }

    unexpectedRequests.push(`${method} ${path}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return {
    unexpectedRequests,
    credentialPostCount: () => credentialPostCount,
  };
}

test("credential validation identifies the slug and preserves non-secret input after failure", async ({
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
  const mocked = await mockCredentialAdmin(page);

  await page.goto("/admin/assets/credentials");
  await page.getByRole("button", { name: "创建凭据" }).click();

  const dialog = page.getByRole("dialog", { name: "创建凭据" });
  const displayName = dialog.locator('input[name="display_name"]');
  const credentialName = dialog.locator('input[name="name"]');
  const credentialType = dialog.locator('select[name="credential_type"]');
  const fieldName = dialog.locator('input[name^="credential_field:"]');
  const credentialValue = dialog.locator('input[name^="credential_value:"]');

  await displayName.fill("Example Credential");
  await credentialName.fill("Bad Slug!");
  await credentialType.selectOption("model_api_key");
  await fieldName.fill("API_TOKEN");
  await credentialValue.fill("test-value");
  await dialog.getByRole("button", { name: "加密写入" }).click();

  await expect(dialog.getByRole("alert")).toHaveText(
    "凭据标识需为 1–63 位小写字母、数字、点、下划线或连字符，并以字母或数字开头和结尾。",
  );
  await expect(displayName).toHaveValue("Example Credential");
  await expect(credentialName).toHaveValue("Bad Slug!");
  await expect(credentialType).toHaveValue("model_api_key");
  await expect(fieldName).toHaveValue("API_TOKEN");
  expect(mocked.credentialPostCount()).toBe(0);

  await credentialName.fill("example-credential");
  await dialog.getByRole("button", { name: "加密写入" }).click();

  await expect(dialog.getByRole("alert")).toHaveText(
    "提交内容不符合资产要求。",
  );
  await expect(displayName).toHaveValue("Example Credential");
  await expect(credentialName).toHaveValue("example-credential");
  await expect(credentialType).toHaveValue("model_api_key");
  await expect(fieldName).toHaveValue("API_TOKEN");
  await expect(credentialValue).toHaveValue("");
  expect(mocked.credentialPostCount()).toBe(1);
  expect(mocked.unexpectedRequests).toEqual([]);
});
