import { expect, test, type Page, type Route } from "@playwright/test";

const TIMESTAMP = "2026-08-22T00:00:00Z";
const CONFIGURED_MODEL = {
  id: "90000000-0000-4000-8000-000000000002",
  display_name: "Existing DeepSeek",
  provider_adapter: "deepseek",
  provider_model: "deepseek-existing",
  settings: {},
  supports_thinking: false,
  supports_reasoning_effort: false,
  supports_vision: false,
  status: "active",
  is_default: false,
  revision: 1,
  api_key_configured: true,
  secret_readiness: "ready",
  secret_revision: 1,
  updated_at: TIMESTAMP,
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function systemSettingsCatalog() {
  return {
    catalog_revision: 1,
    sections: {
      auth: {
        section: "auth",
        revision: 1,
        schema_version: 2,
        effective_revision: 1,
        effect_scope: "new_requests",
        updated_at: TIMESTAMP,
        value: { allow_registration: true },
      },
      automations: {
        section: "automations",
        revision: 1,
        schema_version: 2,
        effective_revision: 1,
        effect_scope: "new_requests",
        updated_at: TIMESTAMP,
        value: {
          enabled: true,
          poll_interval_seconds: 5,
          max_concurrent_runs: 3,
          min_once_delay_seconds: 60,
        },
      },
      quotas: {
        section: "quotas",
        revision: 1,
        schema_version: 2,
        effective_revision: 1,
        effect_scope: "next_authoritative_check",
        updated_at: TIMESTAMP,
        value: {
          default_member_limit: 10,
          default_storage_bytes_limit: 1_073_741_824,
          default_concurrent_run_limit: 4,
          default_mcp_calls_daily_limit: 1_000,
          warning_threshold: 0.8,
        },
      },
      agent_runtime: {
        section: "agent_runtime",
        revision: 1,
        schema_version: 2,
        effective_revision: 1,
        effect_scope: "new_requests_and_runs",
        updated_at: TIMESTAMP,
        value: {
          token_usage: { enabled: true },
          token_budget: {
            enabled: true,
            max_tokens: 100_000,
            max_input_tokens: null,
            max_output_tokens: null,
            warn_threshold: 0.8,
            hard_stop_threshold: 0.95,
          },
          max_recursion_limit: 100,
          vision_bridge: {
            model_name: null,
            timeout_seconds: 20,
            contract_version: "vision.bridge.v1",
          },
          title: {
            enabled: true,
            max_words: 10,
            max_chars: 80,
            model_name: null,
          },
          suggestions: { enabled: true },
          input_polish: {
            enabled: true,
            max_chars: 10_000,
            model_name: null,
          },
          summarization: {
            enabled: true,
            model_name: null,
            trigger: null,
            keep: { type: "messages", value: 20 },
            trim_tokens_to_summarize: null,
            skill_file_read_tool_names: ["read_file"],
          },
          memory: {
            enabled: true,
            model_name: null,
            dream_interval_minutes: 120,
            max_injection_tokens: 2_000,
            idle_seal_minutes: 1_440,
            episode_retention_days: 365,
          },
          tool_search: { enabled: true, auto_promote_top_k: 3 },
          tool_output: {
            enabled: true,
            externalize_min_chars: 10_000,
            preview_head_chars: 1_000,
            preview_tail_chars: 1_000,
            fallback_max_chars: 10_000,
            fallback_head_chars: 1_000,
            fallback_tail_chars: 1_000,
            exempt_tools: [],
            tool_overrides: {},
          },
          loop_detection: {
            enabled: true,
            warn_threshold: 10,
            hard_limit: 20,
            window_size: 50,
            max_tracked_threads: 100,
            tool_freq_warn: 5,
            tool_freq_hard_limit: 10,
            tool_freq_overrides: {},
          },
          read_before_write: { enabled: true },
          safety_finish_reason: { enabled: true },
          subagents: { max_total_per_run: 5 },
        },
      },
      memory_document: {
        section: "memory_document",
        revision: 1,
        schema_version: 2,
        effective_revision: 1,
        effect_scope: "new_memory_documents",
        updated_at: TIMESTAMP,
        value: { sections: ["用户偏好", "项目背景"] },
      },
    },
  };
}

async function mockSystemSettings(page: Page) {
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/setup-status" && request.method() === "GET") {
      return json(route, {
        needs_setup: false,
        registration_enabled: true,
      });
    }
    if (path === "/api/v1/auth/me" && request.method() === "GET") {
      return json(route, {
        id: "90000000-0000-4000-8000-000000000001",
        email: "admin@example.test",
        username: "admin",
        system_role: "system_admin",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/admin/settings/system" && request.method() === "GET") {
      return json(route, systemSettingsCatalog());
    }
    if (path === "/api/models" && request.method() === "GET") {
      return json(route, { models: [], token_usage: { enabled: true } });
    }
    if (path === "/api/admin/settings/models" && request.method() === "GET") {
      return json(route, {
        items: [],
        provider_adapters: [],
        catalog_revision: 1,
        request_id: "browser-draft-test",
      });
    }
    return json(
      route,
      { detail: `unexpected ${request.method()} ${path}` },
      599,
    );
  });
}

async function mockModelSettings(
  page: Page,
  connectionResult: "succeeded" | "request-error" = "succeeded",
) {
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/setup-status" && request.method() === "GET") {
      return json(route, {
        needs_setup: false,
        registration_enabled: true,
      });
    }
    if (path === "/api/v1/auth/me" && request.method() === "GET") {
      return json(route, {
        id: "90000000-0000-4000-8000-000000000001",
        email: "admin@example.test",
        username: "admin",
        system_role: "system_admin",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/admin/settings/models" && request.method() === "GET") {
      return json(route, {
        items: [CONFIGURED_MODEL],
        provider_adapters: [
          { id: "deepseek", api_key_required: true, setting_fields: [] },
        ],
        catalog_revision: 1,
        request_id: "browser-model-test",
      });
    }
    if (
      path === "/api/admin/settings/models/test-connection" &&
      request.method() === "POST"
    ) {
      if (connectionResult === "request-error") {
        return json(route, { detail: "provider unavailable" }, 503);
      }
      return json(route, {
        status: "succeeded",
        request_id: "browser-connection-test",
      });
    }
    return json(
      route,
      { detail: `unexpected ${request.method()} ${path}` },
      599,
    );
  });
}

test("retains grouped drafts, marks them, and confirms in-app route leave", async ({
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
  await mockSystemSettings(page);
  await page.goto("/admin/settings/system");

  const runLimits = page.locator('[data-settings-destination="run-limits"]');
  await runLimits.click();
  const tokenUsage = page.getByRole("switch", { name: "记录 Token 用量" });
  await expect(tokenUsage).toBeChecked();
  await tokenUsage.click();

  await expect(
    page.locator('[data-settings-dirty-marker="run-limits"]'),
  ).toHaveText("未保存");
  await page
    .locator('[data-settings-destination="assistant-experience"]')
    .click();
  await runLimits.click();
  await expect(tokenUsage).not.toBeChecked();

  const leaveDialog = page.getByRole("dialog", { name: "放弃未保存修改？" });
  const skipLink = page.locator('a[href="#admin-main"]');
  await skipLink.focus();
  await expect(skipLink).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/admin\/settings\/system#admin-main$/u);
  await expect(page.locator("#admin-main")).toBeFocused();
  await expect(leaveDialog).toBeHidden();

  await page.locator('a[href="/admin/settings/models"]').first().click();
  await expect(leaveDialog).toBeVisible();
  await expect(page).toHaveURL(/\/admin\/settings\/system#admin-main$/u);
  await leaveDialog.getByRole("button", { name: "继续编辑" }).click();
  await expect(leaveDialog).toBeHidden();
  await expect(tokenUsage).not.toBeChecked();

  await page.locator('a[href="/admin/settings/models"]').first().click();
  await leaveDialog.getByRole("button", { name: "放弃修改并离开" }).click();
  await expect(page).toHaveURL(/\/admin\/settings\/models$/u);
});

test("confirms browser Back navigation while a grouped draft is dirty", async ({
  page,
  baseURL,
}) => {
  await page.context().addCookies([
    {
      name: "locale",
      value: "en-US",
      url: baseURL ?? "http://localhost:3000",
    },
  ]);
  await mockSystemSettings(page);
  await page.goto("/admin/settings/system");
  await page.evaluate(() => {
    window.history.pushState(null, "", "/admin/settings/models");
    window.history.pushState(null, "", "/admin/settings/system");
  });

  await page.locator('[data-settings-destination="run-limits"]').click();
  await page.getByRole("switch", { name: "Track Token usage" }).click();
  await page.goBack();

  const leaveDialog = page.getByRole("dialog", {
    name: "Discard unsaved changes?",
  });
  await expect(leaveDialog).toBeVisible();
  await expect(page).toHaveURL(/\/admin\/settings\/system$/u);
  await leaveDialog.getByRole("button", { name: "Continue editing" }).click();
  await expect(leaveDialog).toBeHidden();
  await expect(page).toHaveURL(/\/admin\/settings\/system$/u);
});

test("explains tested-Key clearing and requires re-entry before create", async ({
  page,
  baseURL,
}) => {
  await page.context().addCookies([
    {
      name: "locale",
      value: "en-US",
      url: baseURL ?? "http://localhost:3000",
    },
  ]);
  await mockModelSettings(page);
  await page.goto("/admin/settings/models");
  const modelPage = page.locator('main[data-slot="admin-page"]');
  await expect(modelPage).not.toContainText(/[\u3400-\u9fff]/u);
  await page.getByRole("button", { name: "Clear API Key" }).click();
  const clearDialog = page.getByRole("dialog", { name: "Clear API Key?" });
  await expect(clearDialog).not.toContainText(/[\u3400-\u9fff]/u);
  await clearDialog.getByRole("button", { name: "Cancel" }).click();

  await page.getByRole("button", { name: "Add model" }).click();

  const dialog = page.getByRole("dialog", { name: "Add model" });
  await dialog.getByLabel("Display name").fill("Temporary DeepSeek");
  await dialog.getByLabel("Provider model ID").fill("deepseek-test");
  const keyInput = dialog.getByLabel("API Key");
  const save = dialog.getByRole("button", { name: "Save" });
  await expect(save).toBeDisabled();
  await keyInput.fill("temporary-test-key");
  await expect(save).toBeEnabled();
  await dialog.getByRole("button", { name: "Test connection" }).click();

  await expect(keyInput).toHaveValue("");
  await expect(save).toBeDisabled();
  await expect(dialog.getByRole("status")).toHaveText(
    "Connection test succeeded. The test Key was cleared from the form; re-enter the API Key before saving.",
  );
  await expect(
    dialog.getByText(/A connection test immediately clears/u),
  ).toBeVisible();
  await expect(dialog).not.toContainText(/[\u3400-\u9fff]/u);

  await keyInput.fill("fresh-save-key");
  await expect(save).toBeEnabled();
});

test("explains Key re-entry after a connection-test request error", async ({
  page,
  baseURL,
}) => {
  await page.context().addCookies([
    {
      name: "locale",
      value: "en-US",
      url: baseURL ?? "http://localhost:3000",
    },
  ]);
  await mockModelSettings(page, "request-error");
  await page.goto("/admin/settings/models");
  await page.getByRole("button", { name: "Add model" }).click();

  const dialog = page.getByRole("dialog", { name: "Add model" });
  await dialog.getByLabel("Display name").fill("Unavailable DeepSeek");
  await dialog.getByLabel("Provider model ID").fill("deepseek-unavailable");
  const keyInput = dialog.getByLabel("API Key");
  await keyInput.fill("temporary-test-key");
  await dialog.getByRole("button", { name: "Test connection" }).click();

  await expect(keyInput).toHaveValue("");
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(dialog.getByRole("status")).toHaveText(
    "Connection test failed. The test Key was cleared from the form; re-enter the API Key to retry or save.",
  );
});
