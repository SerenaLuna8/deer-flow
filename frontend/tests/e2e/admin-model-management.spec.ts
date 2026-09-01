import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";

const PROVIDER_ID = "20000000-0000-4000-8000-000000000001";
const TEXT_MODEL_ID = "10000000-0000-4000-8000-000000000001";
const EMBEDDING_MODEL_ID = "30000000-0000-4000-8000-000000000001";
const RERANK_MODEL_ID = "30000000-0000-4000-8000-000000000002";
const TIMESTAMP = "2026-08-30T00:00:00Z";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type MockProvider = {
  id: string;
  name: string;
  base_url: string;
  request_timeout_seconds: number;
};

type MockTextModel = {
  id: string;
  display_name: string;
  provider_adapter: string;
  provider_model: string;
  provider_id: string;
  max_input_tokens: number;
  status: "active" | "suspended";
  is_default: boolean;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  supports_vision: boolean;
};

type MockProviderModel = {
  id: string;
  provider_id: string;
  model_type: "embedding" | "rerank";
  model_name: string;
  embedding_dimension: number | null;
  max_batch: number;
  status: "active" | "disabled";
  in_use: boolean;
};

function providerFixture(overrides: Partial<MockProvider> = {}): MockProvider {
  return {
    id: PROVIDER_ID,
    name: "SiliconFlow",
    base_url: "https://api.siliconflow.cn/v1",
    request_timeout_seconds: 30,
    ...overrides,
  };
}

function textModelFixture(
  overrides: Partial<MockTextModel> = {},
): MockTextModel {
  return {
    id: TEXT_MODEL_ID,
    display_name: "DeepSeek Chat",
    provider_adapter: "deepseek",
    provider_model: "deepseek-chat",
    provider_id: PROVIDER_ID,
    max_input_tokens: 128_000,
    status: "active",
    is_default: true,
    supports_thinking: false,
    supports_reasoning_effort: false,
    supports_vision: false,
    ...overrides,
  };
}

function modelFixture(
  overrides: Partial<MockProviderModel> = {},
): MockProviderModel {
  return {
    id: EMBEDDING_MODEL_ID,
    provider_id: PROVIDER_ID,
    model_type: "embedding",
    model_name: "Qwen/Qwen3-Embedding-8B",
    embedding_dimension: 1024,
    max_batch: 64,
    status: "active",
    in_use: false,
    ...overrides,
  };
}

async function mockModelManagementRoutes(
  page: Page,
  initial: {
    providers?: MockProvider[];
    textModels?: MockTextModel[];
    models?: MockProviderModel[];
    providerDeleteConflictMessage?: string;
    providerUpdateConflictsOnce?: boolean;
    retrievalTestOk?: boolean;
    deferTextDelete?: boolean;
  } = {},
) {
  const state = {
    providers: [...(initial.providers ?? [])],
    textModels: [...(initial.textModels ?? [])],
    models: [...(initial.models ?? [])],
    providerUpdateConflictsRemaining: initial.providerUpdateConflictsOnce
      ? 1
      : 0,
    providerCreates: [] as Array<Record<string, unknown>>,
    providerUpdates: [] as Array<Record<string, unknown>>,
    candidateTests: [] as Array<Record<string, unknown>>,
    textModelCreates: [] as Array<Record<string, unknown>>,
    textModelReplacements: [] as Array<Record<string, unknown>>,
    storedKeyTests: [] as Array<Record<string, unknown>>,
    textModelDeletes: [] as string[],
    releaseTextDelete: null as (() => void) | null,
    modelCreates: [] as Array<Record<string, unknown>>,
    statusPatches: [] as Array<Record<string, unknown>>,
    testedIds: [] as string[],
    nextId: 1,
  };

  // The list payload derives counts and the endpoint freeze exactly like the
  // backend: counts aggregate bound text models with retrieval models, and a
  // provider is frozen while any of its embeddings is referenced.
  const providerItem = (provider: MockProvider) => {
    const models = state.models.filter(
      (model) => model.provider_id === provider.id,
    );
    const textModels = state.textModels.filter(
      (model) => model.provider_id === provider.id,
    );
    return {
      ...provider,
      api_key_configured: true,
      model_count: models.length + textModels.length,
      active_model_count:
        models.filter((model) => model.status === "active").length +
        textModels.filter((model) => model.status === "active").length,
      endpoint_frozen: models.some(
        (model) => model.model_type === "embedding" && model.in_use,
      ),
      created_at: TIMESTAMP,
      updated_at: TIMESTAMP,
    };
  };

  const textModelItem = (model: MockTextModel) => ({
    ...model,
    provider_name:
      state.providers.find((provider) => provider.id === model.provider_id)
        ?.name ?? "",
    settings: {},
    supports_thinking: model.supports_thinking,
    supports_reasoning_effort: model.supports_reasoning_effort,
    supports_vision: model.supports_vision,
    revision: 1,
    api_key_configured: true,
    secret_readiness: "ready",
    secret_revision: 1,
    updated_at: TIMESTAMP,
  });

  const modelItem = (model: MockProviderModel) => ({
    ...model,
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/auth/me") {
      return json(route, {
        id: "90000000-0000-4000-8000-000000000001",
        email: "admin@example.test",
        username: "admin",
        system_role: "system_admin",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status") {
      return json(route, { needs_setup: false, registration_enabled: true });
    }
    if (path === "/api/projects" && method === "GET") {
      return json(route, { items: [], next_cursor: null });
    }

    // Text-model catalog: adapters carry no base_url or key field anymore.
    if (path === "/api/admin/settings/models" && method === "GET") {
      return json(route, {
        items: state.textModels.map(textModelItem),
        provider_adapters: [
          {
            id: "deepseek",
            api_key_required: true,
            setting_fields: [
              {
                name: "max_tokens",
                label: "Max tokens",
                input_type: "integer",
                advanced: false,
                form_control: "input",
                default_mode: "provider",
                default_value: null,
                minimum: 1,
                maximum: 2_000_000,
                step: 1,
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
            ],
          },
        ],
        catalog_revision: 1,
        request_id: "req-language-catalog",
      });
    }
    if (path === "/api/admin/settings/models" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.textModelCreates.push(body);
      const created = textModelFixture({
        id: `10000000-0000-4000-8000-00000000000${state.nextId++}`,
        display_name:
          typeof body.display_name === "string" ? body.display_name : "",
        provider_model:
          typeof body.provider_model === "string" ? body.provider_model : "",
        provider_id:
          typeof body.provider_id === "string" ? body.provider_id : "",
        max_input_tokens:
          typeof body.max_input_tokens === "number" ? body.max_input_tokens : 0,
        is_default: false,
      });
      state.textModels.push(created);
      return json(route, {
        item: textModelItem(created),
        catalog_revision: 2,
        request_id: "req-text-model-create",
      });
    }
    if (
      path === "/api/admin/settings/models/test-connection" &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.storedKeyTests.push(body);
      return json(route, {
        status: "succeeded",
        request_id: "req-stored-key-test",
      });
    }
    const textModelMatch =
      /^\/api\/admin\/settings\/models\/([0-9a-f-]{36})$/u.exec(path);
    if (textModelMatch && method === "PUT") {
      const target = state.textModels.find(
        (model) => model.id === textModelMatch[1],
      );
      if (!target) return json(route, { detail: "not found" }, 404);
      const body = request.postDataJSON() as Record<string, unknown>;
      state.textModelReplacements.push(body);
      if (typeof body.display_name === "string")
        target.display_name = body.display_name;
      if (typeof body.provider_id === "string")
        target.provider_id = body.provider_id;
      return json(route, {
        item: textModelItem(target),
        catalog_revision: 3,
        request_id: "req-text-model-replace",
      });
    }
    if (textModelMatch && method === "DELETE") {
      if (initial.deferTextDelete) {
        await new Promise<void>((resolve) => {
          state.releaseTextDelete = resolve;
        });
        state.releaseTextDelete = null;
      }
      state.textModelDeletes.push(textModelMatch[1]!);
      state.textModels = state.textModels.filter(
        (model) => model.id !== textModelMatch[1],
      );
      return json(route, { request_id: "req-text-model-delete" });
    }

    const providersBase = "/api/admin/settings/model-providers";
    if (path === `${providersBase}/test-connection` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.candidateTests.push(body);
      return json(route, {
        status: "succeeded",
        request_id: "req-candidate-test",
      });
    }
    if (path === providersBase && method === "GET") {
      return json(route, {
        items: state.providers.map(providerItem),
        request_id: "req-provider-list",
      });
    }
    if (path === providersBase && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.providerCreates.push(body);
      const created = providerFixture({
        id: `20000000-0000-4000-8000-00000000000${state.nextId++}`,
        name: typeof body.name === "string" ? body.name : "",
        base_url: typeof body.base_url === "string" ? body.base_url : "",
        request_timeout_seconds:
          typeof body.request_timeout_seconds === "number"
            ? body.request_timeout_seconds
            : 30,
      });
      state.providers.push(created);
      return json(route, {
        item: providerItem(created),
        request_id: "req-provider-create",
      });
    }

    const providerMatch =
      /^\/api\/admin\/settings\/model-providers\/([0-9a-f-]{36})$/u.exec(path);
    if (providerMatch && method === "PATCH") {
      const target = state.providers.find(
        (provider) => provider.id === providerMatch[1],
      );
      if (!target) return json(route, { detail: "not found" }, 404);
      const body = request.postDataJSON() as Record<string, unknown>;
      state.providerUpdates.push(body);
      if (state.providerUpdateConflictsRemaining > 0) {
        state.providerUpdateConflictsRemaining -= 1;
        return json(
          route,
          {
            detail: {
              code: "KNOWLEDGE_CONFLICT",
              message: "绑定模型正被使用，请稍后重试",
            },
          },
          409,
        );
      }
      if (typeof body.name === "string") target.name = body.name;
      if (typeof body.base_url === "string") target.base_url = body.base_url;
      if (typeof body.request_timeout_seconds === "number")
        target.request_timeout_seconds = body.request_timeout_seconds;
      return json(route, {
        item: providerItem(target),
        request_id: "req-provider-update",
      });
    }
    if (providerMatch && method === "DELETE") {
      if (initial.providerDeleteConflictMessage) {
        return json(
          route,
          {
            detail: {
              code: "KNOWLEDGE_CONFLICT",
              message: initial.providerDeleteConflictMessage,
            },
          },
          409,
        );
      }
      state.providers = state.providers.filter(
        (provider) => provider.id !== providerMatch[1],
      );
      return json(route, { request_id: "req-provider-delete" });
    }

    const modelsMatch =
      /^\/api\/admin\/settings\/model-providers\/([0-9a-f-]{36})\/models$/u.exec(
        path,
      );
    if (modelsMatch && method === "GET") {
      return json(route, {
        items: state.models
          .filter((model) => model.provider_id === modelsMatch[1])
          .map(modelItem),
        request_id: "req-model-list",
      });
    }
    if (modelsMatch && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.modelCreates.push(body);
      const modelType = body.model_type === "rerank" ? "rerank" : "embedding";
      const created = modelFixture({
        id: `30000000-0000-4000-8000-00000000000${state.nextId++}`,
        provider_id: modelsMatch[1]!,
        model_type: modelType,
        model_name: typeof body.model_name === "string" ? body.model_name : "",
        embedding_dimension:
          modelType === "embedding"
            ? Number(body.embedding_dimension ?? 0)
            : null,
        max_batch:
          typeof body.max_batch === "number"
            ? body.max_batch
            : modelType === "embedding"
              ? 64
              : 32,
      });
      state.models.push(created);
      return json(route, {
        item: modelItem(created),
        request_id: "req-model-create",
      });
    }

    const testMatch =
      /^\/api\/admin\/settings\/provider-models\/([0-9a-f-]{36})\/test$/u.exec(
        path,
      );
    if (testMatch && method === "POST") {
      state.testedIds.push(testMatch[1]!);
      const target = state.models.find((model) => model.id === testMatch[1]);
      const ok = initial.retrievalTestOk !== false;
      return json(route, {
        ok,
        message: `${target?.model_type === "rerank" ? "Rerank" : "Embedding"} 连接测试${ok ? "通过" : "失败"}`,
        request_id: "req-model-test",
      });
    }

    const modelMatch =
      /^\/api\/admin\/settings\/provider-models\/([0-9a-f-]{36})$/u.exec(path);
    if (modelMatch && method === "PATCH") {
      const target = state.models.find((model) => model.id === modelMatch[1]);
      if (!target) return json(route, { detail: "not found" }, 404);
      const body = request.postDataJSON() as { status?: "active" | "disabled" };
      state.statusPatches.push({ id: modelMatch[1], ...body });
      if (body.status) target.status = body.status;
      return json(route, {
        item: modelItem(target),
        request_id: "req-model-status",
      });
    }
    if (modelMatch && method === "DELETE") {
      state.models = state.models.filter((model) => model.id !== modelMatch[1]);
      return json(route, { request_id: "req-model-delete" });
    }

    return json(route, { detail: "not found" }, 404);
  });

  return state;
}

function providerCard(page: Page, name: string) {
  return page
    .getByTestId("admin-model-provider-card")
    .filter({ hasText: name })
    .first();
}

function modelRow(page: Page, name: string) {
  return page
    .getByTestId("admin-provider-model-list")
    .getByRole("listitem")
    .filter({ hasText: name });
}

async function expectTextModelActions(
  row: Locator,
  statusAction: "Disable" | "Enable",
  defaultAction: "Default" | "Set as default",
) {
  const actions = row.getByRole("group", { name: "Actions" });
  await expect(actions.getByRole("button")).toHaveCount(3);
  await expect(
    actions.getByRole("button", { name: statusAction, exact: true }),
  ).toBeVisible();
  await expect(
    actions.getByRole("button", { name: defaultAction, exact: true }),
  ).toBeVisible();
  await expect(
    actions.getByRole("button", { name: "More actions", exact: true }),
  ).toBeVisible();
}

async function expectRetrievalModelActions(
  row: Locator,
  statusAction: "Disable" | "Enable",
) {
  const actions = row.getByRole("group", { name: "Actions" });
  await expect(actions.getByRole("button")).toHaveCount(2);
  await expect(
    actions.getByRole("button", { name: statusAction, exact: true }),
  ).toBeVisible();
  await expect(
    actions.getByRole("button", { name: "More actions", exact: true }),
  ).toBeVisible();
}

async function runModelRowTest(page: Page, row: Locator) {
  await row.getByRole("button", { name: "More actions", exact: true }).click();
  await page.getByRole("menuitem", { name: "Test", exact: true }).click();
}

test("does not overflow a desktop viewport when the model list is short", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
    textModels: [textModelFixture()],
  });
  await page.goto("/admin/settings/models");
  await expect(providerCard(page, "SiliconFlow")).toBeVisible();

  const pageHeight = await page.evaluate(() => ({
    clientHeight: document.documentElement.clientHeight,
    scrollHeight: document.documentElement.scrollHeight,
  }));
  expect(pageHeight.scrollHeight).toBeLessThanOrEqual(pageHeight.clientHeight);
});

test("shows output tokens without an adapter group and keeps advanced settings collapsed", async ({
  page,
}) => {
  await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
  });
  await page.goto("/admin/settings/models");

  await providerCard(page, "SiliconFlow")
    .getByRole("button", { name: "Add text model" })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Maximum output tokens")).toBeVisible();
  await expect(
    dialog.getByRole("heading", { name: "Adapter settings" }),
  ).toHaveCount(0);

  const advancedSettings = dialog
    .locator("details")
    .filter({ hasText: "Advanced settings" });
  await expect(advancedSettings).toBeVisible();
  await expect(advancedSettings).not.toHaveAttribute("open", "");
  await expect(advancedSettings.getByLabel("Reasoning effort")).toBeHidden();
  await advancedSettings
    .getByText("Advanced settings", { exact: true })
    .click();
  await expect(advancedSettings.getByLabel("Reasoning effort")).toBeVisible();
});

test("creates a provider through the candidate connection test without persisting the key", async ({
  page,
}) => {
  const state = await mockModelManagementRoutes(page);
  await page.goto("/admin/settings/models");

  await expect(page.getByText("No model providers yet.")).toBeVisible();

  await page.getByRole("button", { name: "Add provider" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Provider name").fill("SiliconFlow");
  await dialog.getByLabel("Base URL").fill("https://api.siliconflow.cn/v1");
  await dialog.getByLabel("API Key").fill("sk-admin-secret");

  // The candidate test probes the transient URL/Key against one explicit
  // text-model target and saves nothing.
  await dialog.getByLabel("Model name for the test").fill("deepseek-chat");
  await dialog.getByRole("button", { name: "Test connection" }).click();
  await expect(
    dialog.getByText(
      "Connection test succeeded for this URL/Key/model combination.",
    ),
  ).toBeVisible();
  expect(state.candidateTests).toHaveLength(1);
  expect(state.candidateTests[0]).toMatchObject({
    base_url: "https://api.siliconflow.cn/v1",
    api_key: "sk-admin-secret",
    provider_adapter: "deepseek",
    provider_model: "deepseek-chat",
    settings: {},
    supports_vision: false,
  });

  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(providerCard(page, "SiliconFlow")).toBeVisible();
  expect(state.providerCreates[0]).toMatchObject({
    name: "SiliconFlow",
    base_url: "https://api.siliconflow.cn/v1",
    request_timeout_seconds: 30,
    api_key: "sk-admin-secret",
  });
});

test("adds a text model bound to a provider and tests with the stored key", async ({
  page,
}) => {
  const state = await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
  });
  await page.goto("/admin/settings/models");

  const card = providerCard(page, "SiliconFlow");
  await expect(
    card.getByText("No models match the current filter."),
  ).toBeVisible();

  await card.getByRole("button", { name: "Add text model" }).click();
  const dialog = page.getByRole("dialog");
  // The dialog binds the provider whose card launched it, shows its endpoint,
  // and exposes no API Key field of its own.
  await expect(dialog.getByLabel("Provider", { exact: true })).toHaveValue(
    PROVIDER_ID,
  );
  await expect(dialog.getByText("https://api.siliconflow.cn/v1")).toBeVisible();
  await expect(dialog.getByLabel("API Key")).toHaveCount(0);

  await dialog.getByLabel("Display name").fill("DeepSeek Chat");
  await dialog.getByLabel("Model ID at the provider").fill("deepseek-chat");
  await dialog.getByLabel("Maximum input tokens").fill("128000");

  // The connection test addresses the provider's stored key; the request
  // body carries the binding, never a key.
  await dialog.getByRole("button", { name: "Test connection" }).click();
  const connectionResult = dialog.getByText(
    "Connection test succeeded using the provider's saved Key.",
  );
  await expect(connectionResult).toBeVisible();
  await expect(connectionResult).toHaveClass(/text-success/u);
  expect(state.storedKeyTests).toHaveLength(1);
  expect(state.storedKeyTests[0]).toMatchObject({
    provider_id: PROVIDER_ID,
    provider_adapter: "deepseek",
    provider_model: "deepseek-chat",
    max_input_tokens: 128_000,
  });
  expect(state.storedKeyTests[0]).not.toHaveProperty("api_key");

  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(state.textModelCreates).toHaveLength(1);
  expect(state.textModelCreates[0]).toMatchObject({
    display_name: "DeepSeek Chat",
    provider_id: PROVIDER_ID,
    provider_model: "deepseek-chat",
    max_input_tokens: 128_000,
    status: "active",
  });
  expect(state.textModelCreates[0]).not.toHaveProperty("api_key");

  // The saved model appears inside its provider's card.
  await expect(card.getByText("DeepSeek Chat")).toBeVisible();
});

test("rebinding a text model to another provider warns about re-encryption", async ({
  page,
}) => {
  const OTHER_PROVIDER_ID = "20000000-0000-4000-8000-000000000002";
  const state = await mockModelManagementRoutes(page, {
    providers: [
      providerFixture(),
      providerFixture({
        id: OTHER_PROVIDER_ID,
        name: "DeepSeek Cloud",
        base_url: "https://api.deepseek.com/v1",
      }),
    ],
    textModels: [textModelFixture()],
  });
  await page.goto("/admin/settings/models");

  const textRow = modelRow(page, "DeepSeek Chat");
  await textRow
    .getByRole("button", { name: "More actions", exact: true })
    .click();
  await page.getByRole("menuitem", { name: "Edit", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Provider", { exact: true })).toHaveValue(
    PROVIDER_ID,
  );

  await dialog
    .getByLabel("Provider", { exact: true })
    .selectOption(OTHER_PROVIDER_ID);
  await expect(page.getByTestId("admin-model-rebind-warning")).toBeVisible();
  await expect(dialog.getByText("https://api.deepseek.com/v1")).toBeVisible();

  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(state.textModelReplacements).toHaveLength(1);
  expect(state.textModelReplacements[0]).toMatchObject({
    provider_id: OTHER_PROVIDER_ID,
  });
  expect(state.textModelReplacements[0]).not.toHaveProperty("api_key");
});

test("adds typed retrieval models and reports per-model probe verdicts", async ({
  page,
}) => {
  const state = await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
  });
  await page.goto("/admin/settings/models");

  const card = providerCard(page, "SiliconFlow");

  // Embedding model: the dimension field is required and submitted.
  await card.getByRole("button", { name: "Add retrieval model" }).click();
  let modelDialog = page.getByRole("dialog");
  await modelDialog.getByLabel("Model name").fill("Qwen/Qwen3-Embedding-8B");
  await modelDialog.getByRole("button", { name: "Save", exact: true }).click();
  const modelList = page.getByTestId("admin-provider-model-list");
  const embeddingRow = modelList
    .getByRole("listitem")
    .filter({ hasText: "Qwen/Qwen3-Embedding-8B" });
  await expect(
    embeddingRow.getByText("Embedding", { exact: true }),
  ).toBeVisible();
  await expect(embeddingRow).toHaveAttribute("data-model-kind", "embedding");
  expect(state.modelCreates[0]).toMatchObject({
    model_type: "embedding",
    model_name: "Qwen/Qwen3-Embedding-8B",
    embedding_dimension: 1024,
  });

  // Rerank model: the dimension input disappears and is never submitted.
  await card.getByRole("button", { name: "Add retrieval model" }).click();
  modelDialog = page.getByRole("dialog");
  await modelDialog.getByRole("combobox").click();
  await page.getByRole("option", { name: "Rerank" }).click();
  await expect(modelDialog.getByLabel("Embedding dimension")).toHaveCount(0);
  await modelDialog.getByLabel("Model name").fill("BAAI/bge-reranker-v2-m3");
  await modelDialog.getByRole("button", { name: "Save", exact: true }).click();
  const rerankRow = modelList
    .getByRole("listitem")
    .filter({ hasText: "BAAI/bge-reranker-v2-m3" });
  await expect(rerankRow.getByText("Rerank", { exact: true })).toBeVisible();
  await expect(rerankRow).toHaveAttribute("data-model-kind", "rerank");
  expect(state.modelCreates[1]).toMatchObject({
    model_type: "rerank",
    model_name: "BAAI/bge-reranker-v2-m3",
  });
  expect(state.modelCreates[1]).not.toHaveProperty("embedding_dimension");

  // Per-model probes report typed verdicts independently.
  await runModelRowTest(page, embeddingRow);
  await expect(
    page
      .locator("[data-sonner-toast]")
      .filter({ hasText: "Embedding 连接测试通过" }),
  ).toBeVisible();
  await expect(embeddingRow.getByText("Embedding 连接测试通过")).toHaveCount(0);
  await runModelRowTest(page, rerankRow);
  await expect(
    page
      .locator("[data-sonner-toast]")
      .filter({ hasText: "Rerank 连接测试通过" }),
  ).toBeVisible();
  await expect(rerankRow.getByText("Rerank 连接测试通过")).toHaveCount(0);
  expect(state.testedIds).toHaveLength(2);
});

test("uses compact primary actions for every model type across providers", async ({
  page,
}) => {
  const OTHER_PROVIDER_ID = "20000000-0000-4000-8000-000000000002";
  const state = await mockModelManagementRoutes(page, {
    providers: [
      providerFixture(),
      providerFixture({
        id: OTHER_PROVIDER_ID,
        name: "DeepSeek",
        base_url: "https://api.deepseek.com/v1",
      }),
    ],
    textModels: [
      textModelFixture(),
      textModelFixture({
        id: "10000000-0000-4000-8000-000000000002",
        display_name: "DeepSeek V4 Pro",
        provider_model: "deepseek-v4-pro",
        provider_id: OTHER_PROVIDER_ID,
        is_default: false,
      }),
    ],
    models: [
      modelFixture(),
      modelFixture({
        id: RERANK_MODEL_ID,
        provider_id: OTHER_PROVIDER_ID,
        model_type: "rerank",
        model_name: "Qwen/Qwen3-VL-Reranker-8B",
        embedding_dimension: null,
        max_batch: 32,
      }),
    ],
  });
  await page.goto("/admin/settings/models");

  const modelList = page.getByTestId("admin-provider-model-list");
  const siliconTextRow = modelRow(page, "DeepSeek Chat");
  const siliconEmbeddingRow = modelRow(page, "Qwen/Qwen3-Embedding-8B");
  await expect(siliconTextRow).toBeVisible();
  await expect(siliconEmbeddingRow).toBeVisible();
  await expect(modelList.getByText("DeepSeek V4 Pro")).toHaveCount(0);
  await expectTextModelActions(siliconTextRow, "Disable", "Default");
  await expect(
    siliconTextRow.getByRole("button", { name: "Default", exact: true }),
  ).toBeDisabled();
  await expectRetrievalModelActions(siliconEmbeddingRow, "Disable");

  await runModelRowTest(page, siliconTextRow);
  await expect.poll(() => state.storedKeyTests.length).toBe(1);
  expect(state.storedKeyTests[0]).toEqual({
    provider_id: PROVIDER_ID,
    provider_adapter: "deepseek",
    provider_model: "deepseek-chat",
    max_input_tokens: 128_000,
    settings: {},
    supports_vision: false,
  });

  await siliconTextRow
    .getByRole("button", { name: "More actions", exact: true })
    .click();
  let actionMenu = page.getByRole("menu");
  await expect(
    actionMenu.getByRole("menuitem", { name: "Edit", exact: true }),
  ).toBeVisible();
  await expect(
    actionMenu.getByRole("menuitem", { name: "Test", exact: true }),
  ).toBeVisible();
  await expect(
    actionMenu.getByRole("menuitem", { name: "Delete", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");

  await siliconEmbeddingRow
    .getByRole("button", { name: "More actions", exact: true })
    .click();
  actionMenu = page.getByRole("menu");
  await expect(
    actionMenu.getByRole("menuitem", { name: "Delete", exact: true }),
  ).toBeVisible();
  await expect(
    actionMenu.getByRole("menuitem", { name: "Test", exact: true }),
  ).toBeVisible();
  await expect(
    actionMenu.getByRole("menuitem", { name: "Edit", exact: true }),
  ).toHaveCount(0);
  await page.keyboard.press("Escape");

  await page
    .getByTestId("admin-model-provider-selector")
    .filter({ hasText: "DeepSeek" })
    .click();

  const deepSeekTextRow = modelRow(page, "DeepSeek V4 Pro");
  const deepSeekRerankRow = modelRow(page, "Qwen/Qwen3-VL-Reranker-8B");
  await expect(deepSeekTextRow).toBeVisible();
  await expect(deepSeekRerankRow).toBeVisible();
  await expect(modelList.getByText("DeepSeek Chat")).toHaveCount(0);
  await expect(modelList.getByText("Qwen/Qwen3-Embedding-8B")).toHaveCount(0);
  await expectTextModelActions(deepSeekTextRow, "Disable", "Set as default");
  await expectRetrievalModelActions(deepSeekRerankRow, "Disable");

  await deepSeekTextRow
    .getByRole("button", { name: "More actions", exact: true })
    .click();
  actionMenu = page.getByRole("menu");
  await expect(
    actionMenu.getByRole("menuitem", { name: "Edit", exact: true }),
  ).toBeVisible();
  await expect(
    actionMenu.getByRole("menuitem", { name: "Test", exact: true }),
  ).toBeVisible();
  await expect(
    actionMenu.getByRole("menuitem", { name: "Delete", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");

  await deepSeekRerankRow
    .getByRole("button", { name: "More actions", exact: true })
    .click();
  actionMenu = page.getByRole("menu");
  await expect(
    actionMenu.getByRole("menuitem", { name: "Delete", exact: true }),
  ).toBeVisible();
  await expect(
    actionMenu.getByRole("menuitem", { name: "Test", exact: true }),
  ).toBeVisible();
  await expect(
    actionMenu.getByRole("menuitem", { name: "Edit", exact: true }),
  ).toHaveCount(0);
});

test("logically deletes the default text model while retaining historical records", async ({
  page,
}) => {
  const state = await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
    textModels: [textModelFixture({ is_default: true })],
    deferTextDelete: true,
  });
  await page.goto("/admin/settings/models");

  const defaultTextRow = modelRow(page, "DeepSeek Chat");
  await expect(defaultTextRow).toBeVisible();
  await expect(
    defaultTextRow.getByRole("button", { name: "Default", exact: true }),
  ).toBeDisabled();

  await defaultTextRow
    .getByRole("button", { name: "More actions", exact: true })
    .click();
  const deleteAction = page.getByRole("menuitem", {
    name: "Delete",
    exact: true,
  });
  await expect(deleteAction).toBeVisible();
  await deleteAction.click();

  const deleteDialog = page.getByRole("dialog");
  await expect(
    deleteDialog.getByRole("heading", { name: "Delete model" }),
  ).toBeVisible();
  await expect(deleteDialog).toContainText(
    "It will be removed from the current model catalog and unavailable for new use.",
  );
  await expect(deleteDialog).toContainText(
    "Historical records that already reference it will be retained.",
  );

  await deleteDialog
    .getByRole("button", { name: "Delete", exact: true })
    .click();

  await expect.poll(() => state.releaseTextDelete !== null).toBe(true);
  await page.keyboard.press("Escape");
  await expect(deleteDialog).toBeVisible();
  await expect(
    deleteDialog.getByRole("button", { name: "Cancel", exact: true }),
  ).toBeDisabled();
  state.releaseTextDelete?.();

  await expect(defaultTextRow).toHaveCount(0);
  await expect(page.getByTestId("admin-provider-model-list")).toBeFocused();
  expect(state.textModelDeletes).toEqual([TEXT_MODEL_ID]);
});

test("centers model test notifications and colors success and failure semantically", async ({
  page,
}) => {
  await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
    textModels: [textModelFixture()],
    models: [modelFixture()],
    retrievalTestOk: false,
  });
  await page.goto("/admin/settings/models");

  await runModelRowTest(page, modelRow(page, "DeepSeek Chat"));
  const successToast = page.locator("[data-sonner-toast]").filter({
    hasText: "Connection test succeeded using the provider's saved Key.",
  });
  await expect(successToast).toBeVisible();
  await expect(successToast).toHaveAttribute("data-type", "success");
  await expect(successToast).toHaveAttribute("data-x-position", "center");
  await expect(successToast).toHaveAttribute("data-y-position", "top");
  await expect(successToast).toHaveAttribute("data-rich-colors", "true");

  await runModelRowTest(page, modelRow(page, "Qwen/Qwen3-Embedding-8B"));
  const errorToast = page
    .locator("[data-sonner-toast]")
    .filter({ hasText: "Embedding 连接测试失败" });
  await expect(errorToast).toBeVisible();
  await expect(errorToast).toHaveAttribute("data-type", "error");
  await expect(errorToast).toHaveAttribute("data-x-position", "center");
  await expect(errorToast).toHaveAttribute("data-y-position", "top");
  await expect(errorToast).toHaveAttribute("data-rich-colors", "true");
});

test("shows text-model capabilities as distinct tags", async ({ page }) => {
  await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
    textModels: [
      textModelFixture({
        supports_thinking: true,
        supports_reasoning_effort: true,
        supports_vision: true,
      }),
    ],
  });
  await page.goto("/admin/settings/models");

  const row = page
    .getByTestId("admin-provider-model-list")
    .getByRole("listitem")
    .filter({ hasText: "DeepSeek Chat" });
  await expect(row.getByText("Thinking", { exact: true })).toBeVisible();
  await expect(
    row.getByText("Reasoning effort", { exact: true }),
  ).toBeVisible();
  await expect(row.getByText("Vision", { exact: true })).toBeVisible();
});

test("in-use protections freeze the endpoint and lock the referenced model", async ({
  page,
}) => {
  const state = await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
    models: [
      modelFixture({ in_use: true }),
      modelFixture({
        id: RERANK_MODEL_ID,
        model_type: "rerank",
        model_name: "BAAI/bge-reranker-v2-m3",
        embedding_dimension: null,
        max_batch: 32,
      }),
    ],
    providerDeleteConflictMessage:
      "供应商仍有绑定的模型，请先改绑或删除后再试。",
  });
  await page.goto("/admin/settings/models");

  const card = providerCard(page, "SiliconFlow");
  const modelList = page.getByTestId("admin-provider-model-list");
  const embeddingRow = modelList
    .getByRole("listitem")
    .filter({ hasText: "Qwen/Qwen3-Embedding-8B" });
  await expect(embeddingRow.getByText("In use")).toBeVisible();
  await expect(
    embeddingRow.getByRole("button", { name: "Disable" }),
  ).toBeDisabled();
  await embeddingRow
    .getByRole("button", { name: "More actions", exact: true })
    .click();
  await expect(
    page.getByRole("menuitem", { name: "Delete", exact: true }),
  ).toHaveAttribute("data-disabled", "");
  await page.keyboard.press("Escape");

  // The unreferenced rerank model can still be disabled.
  const rerankRow = modelList
    .getByRole("listitem")
    .filter({ hasText: "BAAI/bge-reranker-v2-m3" });
  await rerankRow.getByRole("button", { name: "Disable" }).click();
  await expect(rerankRow.getByRole("button", { name: "Enable" })).toBeVisible();
  expect(state.statusPatches).toEqual([
    { id: RERANK_MODEL_ID, status: "disabled" },
  ]);

  // Deleting a provider that still owns models surfaces the server verdict.
  await card
    .getByRole("button", { name: "Delete", exact: true })
    .first()
    .click();
  const deleteDialog = page.getByRole("dialog");
  await deleteDialog
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(
    deleteDialog.getByText("供应商仍有绑定的模型，请先改绑或删除后再试。"),
  ).toBeVisible();

  // The frozen endpoint is not editable in place.
  await deleteDialog.getByRole("button", { name: "Cancel" }).click();
  await card.getByRole("button", { name: "Edit", exact: true }).last().click();
  const editDialog = page.getByRole("dialog");
  await expect(editDialog.getByLabel("Base URL")).toBeDisabled();
  await expect(
    editDialog.getByText(
      "Referenced by knowledge bases; the endpoint cannot be changed in place. Create a new provider for a new endpoint.",
    ),
  ).toBeVisible();
});

test("editing keeps the saved key when blank and demands a new key with a new endpoint", async ({
  page,
}) => {
  const state = await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
    textModels: [textModelFixture()],
  });
  await page.goto("/admin/settings/models");

  const card = providerCard(page, "SiliconFlow");

  // The provider header's Edit comes before the text-model card's Edit.
  // A blank key on edit means "preserve the saved value": no api_key field.
  await card.getByRole("button", { name: "Edit", exact: true }).first().click();
  let dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("API Key")).toHaveValue("");
  await dialog.getByLabel("Provider name").fill("SiliconFlow primary");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(providerCard(page, "SiliconFlow primary")).toBeVisible();
  expect(state.providerUpdates).toHaveLength(1);
  expect(state.providerUpdates[0]).not.toHaveProperty("api_key");

  // Changing the endpoint without a fresh key is rejected before any request.
  await providerCard(page, "SiliconFlow primary")
    .getByRole("button", { name: "Edit", exact: true })
    .first()
    .click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Base URL").fill("https://other.example.test/v1");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(
    dialog.getByText("Changing the endpoint requires entering a new API Key."),
  ).toBeVisible();
  expect(state.providerUpdates).toHaveLength(1);

  // Entering a key with bound text models surfaces the fan-out warning, and
  // the endpoint change goes through with the fresh key.
  await dialog.getByLabel("API Key").fill("sk-rotated-secret");
  await expect(page.getByTestId("admin-provider-fanout-warning")).toBeVisible();
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(state.providerUpdates).toHaveLength(2);
  expect(state.providerUpdates[1]).toMatchObject({
    base_url: "https://other.example.test/v1",
    api_key: "sk-rotated-secret",
  });
});

test("warns that provider key rotation also affects hidden deleted text models", async ({
  page,
}) => {
  await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
  });
  await page.goto("/admin/settings/models");

  await providerCard(page, "SiliconFlow")
    .getByRole("button", { name: "Edit", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("API Key").fill("sk-rotated-history");

  await expect(
    dialog.getByTestId("admin-provider-fanout-warning"),
  ).toContainText("including hidden deleted models");
});

test("a failed save keeps the entered key so the retry still rotates it", async ({
  page,
}) => {
  const state = await mockModelManagementRoutes(page, {
    providers: [providerFixture()],
    textModels: [textModelFixture()],
    providerUpdateConflictsOnce: true,
  });
  await page.goto("/admin/settings/models");

  // Rotate the key; the first save hits a 409 fan-out conflict.
  await providerCard(page, "SiliconFlow")
    .getByRole("button", { name: "Edit", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("API Key").fill("sk-retry-secret");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog.getByText("绑定模型正被使用，请稍后重试")).toBeVisible();

  // The dialog stays open with the key intact, so the retry is still a
  // rotation instead of silently degrading to a rename-only update.
  await expect(dialog.getByLabel("API Key")).toHaveValue("sk-retry-secret");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(state.providerUpdates).toHaveLength(2);
  expect(state.providerUpdates[0]).toMatchObject({
    api_key: "sk-retry-secret",
  });
  expect(state.providerUpdates[1]).toMatchObject({
    api_key: "sk-retry-secret",
  });
});
