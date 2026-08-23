import { expect, test, type Page, type Route } from "@playwright/test";

const TIMESTAMP = "2026-08-22T00:00:00Z";
const CONFIGURED_MODEL = {
  id: "90000000-0000-4000-8000-000000000002",
  display_name: "Existing DeepSeek",
  provider_adapter: "deepseek",
  provider_model: "deepseek-existing",
  max_input_tokens: 128_000,
  settings: {
    max_tokens: 64_000,
    temperature: 0,
    reasoning_effort: "high",
    extra_body: { reasoning_format: "deepseek-style" },
  },
  supports_thinking: true,
  supports_reasoning_effort: true,
  supports_vision: false,
  status: "active",
  is_default: false,
  revision: 1,
  api_key_configured: true,
  secret_readiness: "ready",
  secret_revision: 1,
  updated_at: TIMESTAMP,
};

const DEEPSEEK_SETTING_FIELDS = [
  {
    name: "base_url",
    label: "Base URL",
    input_type: "url",
    advanced: false,
    form_control: "input",
    default_mode: "platform",
    default_value: "https://api.deepseek.com/v1",
    minimum: null,
    maximum: null,
    step: null,
    options: [],
  },
  {
    name: "max_tokens",
    label: "Max tokens",
    input_type: "integer",
    advanced: false,
    form_control: "input",
    default_mode: "platform",
    default_value: 51_200,
    minimum: 1,
    maximum: 2_000_000,
    step: 1,
    options: [],
  },
  {
    name: "temperature",
    label: "Temperature",
    input_type: "number",
    advanced: true,
    form_control: "input",
    default_mode: "provider",
    default_value: null,
    minimum: -2,
    maximum: 2,
    step: 0.01,
    options: [],
  },
  {
    name: "reasoning_effort",
    label: "Reasoning effort",
    input_type: "enum",
    advanced: true,
    form_control: "input",
    default_mode: "provider",
    default_value: null,
    minimum: null,
    maximum: null,
    step: null,
    options: ["low", "high", "max"],
  },
  {
    name: "extra_body",
    label: "Extra request body",
    input_type: "json",
    advanced: true,
    form_control: "preserve",
    default_mode: "provider",
    default_value: null,
    minimum: null,
    maximum: null,
    step: null,
    options: [],
  },
] as const;

const OPENAI_SETTING_FIELDS = [
  {
    name: "base_url",
    label: "Base URL",
    input_type: "url",
    advanced: false,
    form_control: "input",
    default_mode: "platform",
    default_value: "https://api.openai.com/v1",
    minimum: null,
    maximum: null,
    step: null,
    options: [],
  },
  {
    name: "max_tokens",
    label: "Max tokens",
    input_type: "integer",
    advanced: false,
    form_control: "input",
    default_mode: "platform",
    default_value: 16_384,
    minimum: 1,
    maximum: 2_000_000,
    step: 1,
    options: [],
  },
  {
    name: "use_responses_api",
    label: "Use Responses API",
    input_type: "boolean",
    advanced: true,
    form_control: "input",
    default_mode: "provider",
    default_value: null,
    minimum: null,
    maximum: null,
    step: null,
    options: [],
  },
  {
    name: "output_version",
    label: "Output version",
    input_type: "enum",
    advanced: true,
    form_control: "input",
    default_mode: "platform",
    default_value: "responses/v1",
    minimum: null,
    maximum: null,
    step: null,
    options: ["responses/v1"],
  },
] as const;

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
        schema_version: 5,
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
            identical_calls: {
              warn_threshold: 3,
              hard_limit: 5,
              window_size: 20,
            },
          },
          internal_tool_call_limit: 200,
          read_before_write: { enabled: true },
          safety_finish_reason: { enabled: true },
          subagents: {
            max_concurrent: 3,
            max_total_per_run_by_workload: {
              interactive: 6,
              research: 9,
            },
          },
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
  includeUnknownHistoricalSetting = false,
) {
  const mutationBodies: Array<{
    method: string;
    path: string;
    body: Record<string, unknown>;
  }> = [];
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
        items: [
          includeUnknownHistoricalSetting
            ? {
                ...CONFIGURED_MODEL,
                settings: {
                  ...CONFIGURED_MODEL.settings,
                  retired_vendor_flag: true,
                },
              }
            : CONFIGURED_MODEL,
        ],
        provider_adapters: [
          {
            id: "deepseek",
            api_key_required: true,
            setting_fields: DEEPSEEK_SETTING_FIELDS,
          },
          {
            id: "openai",
            api_key_required: true,
            setting_fields: OPENAI_SETTING_FIELDS,
          },
        ],
        catalog_revision: 1,
        request_id: "browser-model-test",
      });
    }
    if (
      path === "/api/admin/settings/models/test-connection" &&
      request.method() === "POST"
    ) {
      mutationBodies.push({
        method: request.method(),
        path,
        body: JSON.parse(request.postData() ?? "{}") as Record<string, unknown>,
      });
      if (connectionResult === "request-error") {
        return json(route, { detail: "provider unavailable" }, 503);
      }
      return json(route, {
        status: "succeeded",
        request_id: "browser-connection-test",
      });
    }
    if (
      path === `/api/admin/settings/models/${CONFIGURED_MODEL.id}` &&
      request.method() === "PUT"
    ) {
      const body = JSON.parse(request.postData() ?? "{}") as Record<
        string,
        unknown
      >;
      mutationBodies.push({ method: request.method(), path, body });
      const publicBody = { ...body };
      delete publicBody.api_key;
      return json(route, {
        item: {
          ...CONFIGURED_MODEL,
          ...publicBody,
          revision: 2,
          updated_at: TIMESTAMP,
        },
        catalog_revision: 2,
        request_id: "browser-model-update",
      });
    }
    return json(
      route,
      { detail: `unexpected ${request.method()} ${path}` },
      599,
    );
  });
  return mutationBodies;
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

test("edits and retains the shared per-Run internal tool-call limit draft", async ({
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

  const destination = page.locator('[data-settings-destination="run-limits"]');
  await destination.click();
  const executionLimits = page.locator(
    '[data-settings-subsection="run-execution"]',
  );
  const tokenBudget = page.locator('[data-settings-subsection="token-budget"]');
  const internalToolCallLimit = page.locator(
    'input[name="agent_runtime.internal_tool_call_limit"]',
  );
  await expect(executionLimits).toContainText(
    "Always enforced independently of the Token budget toggle.",
  );
  await expect(tokenBudget).toContainText(
    "The toggle controls only the limits and thresholds in this section.",
  );
  await expect(
    executionLimits.locator(
      'input[name="agent_runtime.internal_tool_call_limit"]',
    ),
  ).toHaveCount(1);
  await expect(
    tokenBudget.locator('input[name="agent_runtime.internal_tool_call_limit"]'),
  ).toHaveCount(0);
  const executionBox = await executionLimits.boundingBox();
  const tokenBudgetBox = await tokenBudget.boundingBox();
  expect(executionBox?.y).toBeLessThan(tokenBudgetBox?.y ?? 0);

  await tokenBudget
    .getByRole("switch", { name: "Enable per-Run Token budget" })
    .click();
  await expect(
    tokenBudget.locator('input[name="agent_runtime.token_budget.max_tokens"]'),
  ).toBeDisabled();
  await expect(internalToolCallLimit).toBeEnabled();
  await expect(internalToolCallLimit).toHaveValue("200");
  await internalToolCallLimit.fill("240");
  await expect(
    page.locator('[data-settings-dirty-marker="run-limits"]'),
  ).toHaveText("Unsaved");

  await page
    .locator('[data-settings-destination="assistant-experience"]')
    .click();
  await destination.click();
  await expect(internalToolCallLimit).toHaveValue("240");
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
  await dialog.getByLabel("Maximum input tokens").fill("128000");
  await expect(
    dialog.getByText(/denominator for context-percentage summarization/u),
  ).toBeVisible();
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
  await dialog.getByLabel("Maximum input tokens").fill("128000");
  const keyInput = dialog.getByLabel("API Key");
  await keyInput.fill("temporary-test-key");
  await dialog.getByRole("button", { name: "Test connection" }).click();

  await expect(keyInput).toHaveValue("");
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(dialog.getByRole("status")).toHaveText(
    "Connection test failed. The test Key was cleared from the form; re-enter the API Key to retry or save.",
  );
});

test("uses typed Provider fields, preserves structured settings, and resets defaults on Provider change", async ({
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
  const mutationBodies = await mockModelSettings(page);
  await page.goto("/admin/settings/models");
  await page.getByRole("button", { name: "Edit" }).click();

  let dialog = page.getByRole("dialog", { name: "Edit model" });
  await expect(dialog).toHaveCSS("max-width", "768px");
  await expect(dialog.getByText("Provider settings JSON")).toHaveCount(0);
  await expect(dialog.locator('textarea[name="settings"]')).toHaveCount(0);
  await expect(dialog.getByLabel("Base URL")).toHaveValue(
    "https://api.deepseek.com/v1",
  );
  await expect(dialog.getByLabel("Maximum output tokens")).toHaveValue("64000");
  await expect(dialog.getByLabel("Temperature")).not.toBeVisible();

  const advanced = dialog.locator("details");
  await expect(advanced).not.toHaveAttribute("open", "");
  await advanced.locator("summary").click();
  await expect(dialog.getByLabel("Temperature")).toHaveValue("0");
  const reasoningEffort = advanced.getByRole("combobox");
  await expect(reasoningEffort).toHaveValue("high");
  await expect(reasoningEffort.locator("option")).toHaveText([
    "Provider default",
    "low",
    "high",
    "max",
  ]);
  for (const unsupported of ["none", "minimal", "medium"]) {
    await expect(
      reasoningEffort.locator(`option[value="${unsupported}"]`),
    ).toHaveCount(0);
  }
  await expect(
    dialog.getByText(/structured advanced setting.*preserved unchanged/u),
  ).toBeVisible();
  await expect(dialog).not.toContainText("deepseek-style");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toBeHidden();

  const update = mutationBodies.find((item) => item.method === "PUT");
  expect(update?.body.settings).toEqual({
    max_tokens: 64_000,
    temperature: 0,
    reasoning_effort: "high",
    extra_body: { reasoning_format: "deepseek-style" },
  });

  await page.getByRole("button", { name: "Add model" }).click();
  dialog = page.getByRole("dialog", { name: "Add model" });
  await expect(dialog.getByLabel("Maximum output tokens")).toHaveValue("51200");
  await expect(dialog.getByLabel("Temperature")).not.toBeVisible();
  await dialog.locator("details summary").click();
  await expect(dialog.getByLabel("Temperature")).toHaveValue("");
  await expect(
    dialog.getByText(
      "No override is stored; the Provider decides the effective value.",
    ),
  ).toHaveCount(0);
  await dialog.getByLabel("API Key").fill("temporary-provider-key");
  const providerSelect = dialog.getByRole("combobox", {
    name: "Provider",
    exact: true,
  });
  await providerSelect.selectOption("openai");
  await expect(dialog.getByLabel("API Key")).toHaveValue("");
  await expect(dialog.getByLabel("Base URL")).toHaveValue(
    "https://api.openai.com/v1",
  );
  await expect(dialog.getByLabel("Maximum output tokens")).toHaveValue("16384");
  await expect(dialog.locator("details")).not.toHaveAttribute("open", "");
  await providerSelect.selectOption("deepseek");
  await expect(dialog.getByLabel("Maximum output tokens")).toHaveValue("51200");
  await expect(dialog.getByLabel("Temperature")).not.toBeVisible();
});
