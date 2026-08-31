import { expect, test, type Page, type Route } from "@playwright/test";

const MODEL_ID = "22222222-2222-4222-8222-222222222222";
const SECRET = "fictional-browser-storage-key";
const SAFE_ERROR = "知识库配置无效，请检查存储连接、密钥和模型状态";

function initialSettings() {
  return {
    enabled: false,
    worker_concurrency: 2,
    task_timeout_seconds: 900,
    upload_max_bytes: 10_485_760,
    max_knowledge_bases_per_project: 20,
    max_documents_per_knowledge_base: 100,
    max_segments_per_document: 1000,
    minio_endpoint: null as string | null,
    minio_bucket: null as string | null,
    minio_access_key: null as string | null,
    minio_secure: false,
    summary_model_name: null as string | null,
    query_cache_enabled: true,
    query_cache_max_entries: 256,
    query_cache_ttl_seconds: 300,
    revision: 1,
    updated_at: "2026-08-31T12:00:00Z",
    secret_key_configured: false,
    summary_model: null as { model_name: string; display_name: string } | null,
    request_id: "admin-knowledge-browser",
  };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockKnowledgeSettings(
  page: Page,
  mode: "success" | "conflict" | "conflict-refresh" | "invalid" = "success",
) {
  let settings = initialSettings();
  const writes: Array<Record<string, unknown>> = [];
  const state = {
    role: "system_admin",
    authReads: 0,
    reads: 0,
    modelReads: 0,
    writes,
    responses: [] as unknown[],
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/setup-status")
      return json(route, { needs_setup: false, registration_enabled: true });
    if (path === "/api/v1/auth/me") {
      state.authReads += 1;
      return json(route, {
        id: "11111111-1111-4111-8111-111111111111",
        email: "admin@example.test",
        username: "admin",
        system_role: state.role,
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/models") {
      state.modelReads += 1;
      return json(route, {
        models: [
          {
            name: MODEL_ID,
            model: MODEL_ID,
            display_name: "Document summary model",
            supports_thinking: false,
            supports_reasoning_effort: false,
            supports_vision: false,
            is_default: true,
          },
        ],
        token_usage: { enabled: true },
      });
    }
    if (path === "/api/admin/settings/knowledge") {
      if (request.method() === "GET") {
        state.reads += 1;
        if (mode === "conflict-refresh" && state.reads === 2)
          return json(
            route,
            {
              detail: {
                code: "KNOWLEDGE_SETTINGS_UNAVAILABLE",
                message: "Unavailable",
                request_id: "retry-read",
              },
            },
            503,
          );
        state.responses.push(settings);
        return json(route, settings);
      }
      if (request.method() === "PUT") {
        const input = request.postDataJSON() as Record<string, unknown>;
        writes.push(input);
        if (mode === "invalid")
          return json(
            route,
            {
              detail: {
                code: "KNOWLEDGE_SETTINGS_INVALID",
                message: SAFE_ERROR,
                request_id: "storage-invalid",
              },
            },
            422,
          );
        if (
          (mode === "conflict" || mode === "conflict-refresh") &&
          writes.length === 1
        ) {
          settings = { ...settings, revision: 2, worker_concurrency: 3 };
          return json(
            route,
            {
              detail: {
                code: "KNOWLEDGE_SETTINGS_CONFLICT",
                message: "Configuration changed.",
                request_id: "revision-conflict",
              },
            },
            409,
          );
        }
        const {
          minio_secret_key: secret,
          expected_revision: revision,
          ...fields
        } = input;
        expect(revision).toBe(settings.revision);
        settings = {
          ...settings,
          ...fields,
          revision: settings.revision + 1,
          secret_key_configured:
            settings.secret_key_configured || typeof secret === "string",
          summary_model: fields.summary_model_name
            ? { model_name: MODEL_ID, display_name: "Document summary model" }
            : null,
        };
        state.responses.push(settings);
        return json(route, settings);
      }
    }
    return json(
      route,
      { detail: `unexpected ${request.method()} ${path}` },
      599,
    );
  });
  return state;
}

test("knowledge settings render, track dirty drafts, reset, and save the next revision", async ({
  page,
}) => {
  const state = await mockKnowledgeSettings(page);
  await page.goto("/admin/settings/knowledge");
  await expect(
    page.getByRole("heading", { name: "Knowledge settings", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByTestId("knowledge-settings-restart-banner"),
  ).toContainText("Summary model changes take effect immediately");
  const save = page.getByRole("button", { name: "Save settings", exact: true });
  await expect(save).toBeDisabled();
  await page.getByLabel("Processing concurrency", { exact: true }).fill("5");
  await expect(
    page.getByText("Unsaved changes", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Discard changes" }).click();
  await expect(
    page.getByLabel("Processing concurrency", { exact: true }),
  ).toHaveValue("2");
  await page.getByLabel("Processing concurrency", { exact: true }).fill("4");
  await save.click();
  await expect(page.getByRole("status")).toContainText("Settings saved.");
  await expect(page.getByTestId("knowledge-settings-revision")).toHaveText(
    "Revision 2",
  );
  await expect(save).toBeDisabled();
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0]).toMatchObject({
    expected_revision: 1,
    worker_concurrency: 4,
  });
  expect(state.writes[0]).not.toHaveProperty("minio_secret_key");
});

test("an administrator enables knowledge with storage and a System Model without echoing the key", async ({
  page,
}) => {
  const state = await mockKnowledgeSettings(page);
  await page.goto("/admin/settings/knowledge");
  await page
    .getByRole("switch", { name: "Enable knowledge", exact: true })
    .click();
  await page
    .getByLabel("Storage endpoint", { exact: true })
    .fill("storage.example.test:9000");
  await page.getByLabel("Storage bucket", { exact: true }).fill("documents");
  await page
    .getByLabel("Storage access key", { exact: true })
    .fill("knowledge-access");
  await page.getByLabel("Storage secret key", { exact: true }).fill(SECRET);
  await page.getByRole("switch", { name: "Use TLS", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Summary model", exact: true })
    .click();
  await page
    .getByRole("option", { name: "Document summary model", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Settings saved.");
  const key = page.getByLabel("Storage secret key", { exact: true });
  await expect(key).toHaveValue("");
  await expect(key).toHaveAttribute(
    "placeholder",
    "Configured — leave blank to keep",
  );
  expect(state.writes[0]).toMatchObject({
    enabled: true,
    minio_secret_key: SECRET,
    minio_secure: true,
    summary_model_name: MODEL_ID,
    expected_revision: 1,
  });
  expect(JSON.stringify(state.responses)).not.toContain(SECRET);
  await page.getByLabel("Task timeout (seconds)", { exact: true }).fill("1200");
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(page.getByTestId("knowledge-settings-revision")).toHaveText(
    "Revision 3",
  );
  expect(state.writes[1]).not.toHaveProperty("minio_secret_key");
  await page
    .getByLabel("Storage endpoint", { exact: true })
    .fill("new-storage.example.test:9000");
  await expect(
    page.getByRole("button", { name: "Save settings", exact: true }),
  ).toBeDisabled();
  await key.fill(SECRET);
  await expect(
    page.getByRole("button", { name: "Save settings", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Discard changes" }).click();
  await page.reload();
  await expect(key).toHaveValue("");
  await expect(key).toHaveAttribute(
    "placeholder",
    "Configured — leave blank to keep",
  );
});

test("a revision conflict preserves the safe draft and refreshes revision before an explicit retry", async ({
  page,
}) => {
  const state = await mockKnowledgeSettings(page, "conflict");
  await page.goto("/admin/settings/knowledge");
  await page.getByLabel("Processing concurrency", { exact: true }).fill("6");
  await page.getByLabel("Storage secret key", { exact: true }).fill(SECRET);
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(
    page.getByTestId("admin-knowledge-settings-form").getByRole("alert"),
  ).toContainText("Your draft is preserved");
  await expect(
    page.getByLabel("Processing concurrency", { exact: true }),
  ).toHaveValue("6");
  await expect(
    page.getByLabel("Storage secret key", { exact: true }),
  ).toHaveValue("");
  await expect(page.getByTestId("knowledge-settings-revision")).toHaveText(
    "Revision 2",
  );
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Settings saved.");
  expect(state.writes[1]).toMatchObject({
    expected_revision: 2,
    worker_concurrency: 6,
  });
  expect(state.writes[1]).not.toHaveProperty("minio_secret_key");
});

test("safe storage validation errors remain visible and submitted keys are cleared", async ({
  page,
}) => {
  const state = await mockKnowledgeSettings(page, "invalid");
  await page.goto("/admin/settings/knowledge");
  await page.getByLabel("Processing concurrency", { exact: true }).fill("5");
  await page.getByLabel("Storage secret key", { exact: true }).fill(SECRET);
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(
    page.getByTestId("admin-knowledge-settings-form").getByRole("alert"),
  ).toHaveText(SAFE_ERROR);
  await expect(
    page.getByLabel("Storage secret key", { exact: true }),
  ).toHaveValue("");
  await expect(
    page.getByLabel("Processing concurrency", { exact: true }),
  ).toHaveValue("5");
  await page.getByLabel("Processing concurrency", { exact: true }).blur();
  await expect(
    page.getByTestId("admin-knowledge-settings-form").getByRole("alert"),
  ).toHaveText(SAFE_ERROR);
  expect(state.writes).toHaveLength(1);
});

test("client administrator loss hides the knowledge settings form and navigation", async ({
  page,
}) => {
  const authWarnings: string[] = [];
  page.on("console", (message) => {
    if (message.text().startsWith("[auth]")) authWarnings.push(message.text());
  });
  const state = await mockKnowledgeSettings(page);
  await page.goto("/admin/settings/knowledge");
  await expect(page.getByTestId("admin-knowledge-settings-form")).toBeVisible();
  state.role = "user";
  const authReads = state.authReads;
  await page.bringToFront();
  // Exercise the normal visibility refresh after its 60-second throttle.
  await page.clock.setFixedTime(Date.now() + 61_000);
  await expect
    .poll(() => page.evaluate(() => document.visibilityState))
    .toBe("visible");
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await expect.poll(() => state.authReads).toBeGreaterThan(authReads);
  await expect
    .poll(async () => ({
      count: await page.getByTestId("admin-knowledge-settings-form").count(),
      authWarnings,
    }))
    .toEqual({ count: 0, authWarnings: [] });
  await expect(page.locator('a[href="/admin/settings/knowledge"]')).toHaveCount(
    0,
  );
  const reads = state.reads;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  expect(state.reads).toBe(reads);
  expect(state.writes).toHaveLength(0);
});

test("a failed conflict refresh stays recoverable after the administrator edits the draft", async ({
  page,
}) => {
  const state = await mockKnowledgeSettings(page, "conflict-refresh");
  await page.goto("/admin/settings/knowledge");
  await page.getByLabel("Processing concurrency", { exact: true }).fill("6");
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(
    page.getByTestId("admin-knowledge-settings-form").getByRole("alert"),
  ).toContainText("Retry loading the latest revision");
  await page.getByLabel("Processing concurrency", { exact: true }).fill("7");
  await expect(
    page.getByRole("button", { name: "Save settings", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByTestId("knowledge-settings-revision")).toHaveText(
    "Revision 2",
  );
  await expect(
    page.getByLabel("Processing concurrency", { exact: true }),
  ).toHaveValue("7");
  await page
    .getByRole("button", { name: "Save settings", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Settings saved.");
  expect(state.writes[1]).toMatchObject({
    expected_revision: 2,
    worker_concurrency: 7,
  });
});
