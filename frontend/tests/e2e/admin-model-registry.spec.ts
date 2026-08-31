import { expect, test, type Page, type Route } from "@playwright/test";

const PROVIDER_ID = "20000000-0000-4000-8000-000000000001";
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

async function mockRegistryRoutes(
  page: Page,
  initial: {
    providers?: MockProvider[];
    models?: MockProviderModel[];
    registryDisabled?: boolean;
  } = {},
) {
  const state = {
    providers: [...(initial.providers ?? [])],
    models: [...(initial.models ?? [])],
    providerCreates: [] as Array<Record<string, unknown>>,
    providerUpdates: [] as Array<Record<string, unknown>>,
    modelCreates: [] as Array<Record<string, unknown>>,
    statusPatches: [] as Array<Record<string, unknown>>,
    testedIds: [] as string[],
    nextId: 1,
  };

  // The list payload derives counts and the endpoint freeze exactly like the
  // backend: a provider is frozen while any of its embeddings is referenced.
  const providerItem = (provider: MockProvider) => {
    const models = state.models.filter(
      (model) => model.provider_id === provider.id,
    );
    return {
      ...provider,
      api_key_configured: true,
      model_count: models.length,
      active_model_count: models.filter((model) => model.status === "active")
        .length,
      endpoint_frozen: models.some(
        (model) => model.model_type === "embedding" && model.in_use,
      ),
      created_at: TIMESTAMP,
      updated_at: TIMESTAMP,
    };
  };

  const modelItem = (model: MockProviderModel) => ({
    ...model,
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
  });

  await page.route("**/api/**", (route) => {
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

    // The language-model catalog lives on the same page and must keep
    // working regardless of the registry's module state.
    if (path === "/api/admin/settings/models" && method === "GET") {
      return json(route, {
        items: [],
        provider_adapters: [],
        catalog_revision: 1,
        request_id: "req-language-catalog",
      });
    }

    const providersBase = "/api/admin/settings/model-providers";
    if (path === providersBase && method === "GET") {
      if (initial.registryDisabled) {
        return json(
          route,
          {
            detail: {
              code: "KNOWLEDGE_DISABLED",
              message: "Knowledge 模块未启用",
            },
          },
          404,
        );
      }
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
      if (typeof body.name === "string") target.name = body.name;
      if (typeof body.base_url === "string") target.base_url = body.base_url;
      if (typeof body.request_timeout_seconds === "number")
        target.request_timeout_seconds = body.request_timeout_seconds;
      return json(route, {
        item: providerItem(target),
        request_id: "req-provider-update",
      });
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
      return json(route, {
        ok: true,
        message:
          target?.model_type === "rerank"
            ? "Rerank 连接测试通过"
            : "Embedding 连接测试通过",
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
      state.models = state.models.filter(
        (model) => model.id !== modelMatch[1],
      );
      return json(route, { request_id: "req-model-delete" });
    }

    return json(route, { detail: "not found" }, 404);
  });

  return state;
}

test("creates a provider, adds typed models, and reports per-model probe verdicts", async ({
  page,
}) => {
  const state = await mockRegistryRoutes(page);
  await page.goto("/admin/settings/models");

  const registry = page
    .locator("section")
    .filter({ hasText: "Retrieval model providers" });
  await expect(registry.getByText("No model providers yet.")).toBeVisible();

  await registry.getByRole("button", { name: "Add provider" }).click();
  const providerDialog = page.getByRole("dialog");
  await providerDialog.getByLabel("Provider name").fill("SiliconFlow");
  await providerDialog.getByLabel("API Key").fill("sk-admin-secret");
  await providerDialog
    .getByRole("button", { name: "Save", exact: true })
    .click();

  const providerList = page.getByTestId("admin-model-provider-list");
  const providerCard = providerList
    .getByRole("listitem")
    .filter({ hasText: "SiliconFlow" })
    .first();
  await expect(providerCard).toBeVisible();
  expect(state.providerCreates[0]).toMatchObject({
    name: "SiliconFlow",
    base_url: "https://api.siliconflow.cn/v1",
    request_timeout_seconds: 30,
    api_key: "sk-admin-secret",
  });
  // No probe has ever succeeded: the key must not read as verified.
  await expect(
    providerCard.getByText("Key configured, unverified", { exact: true }),
  ).toBeVisible();

  // Embedding model: the dimension field is required and submitted.
  await providerCard.getByRole("button", { name: "Add model" }).click();
  let modelDialog = page.getByRole("dialog");
  await modelDialog.getByLabel("Model name").fill("Qwen/Qwen3-Embedding-8B");
  await modelDialog.getByRole("button", { name: "Save", exact: true }).click();
  const modelList = page.getByTestId("admin-provider-model-list");
  const embeddingRow = modelList
    .getByRole("listitem")
    .filter({ hasText: "Qwen/Qwen3-Embedding-8B" });
  await expect(embeddingRow.getByText("Embedding", { exact: true })).toBeVisible();
  await expect(embeddingRow.getByText("Dimension 1024", { exact: false })).toBeVisible();
  expect(state.modelCreates[0]).toMatchObject({
    model_type: "embedding",
    model_name: "Qwen/Qwen3-Embedding-8B",
    embedding_dimension: 1024,
  });

  // Rerank model: the dimension input disappears and is never submitted.
  await providerCard.getByRole("button", { name: "Add model" }).click();
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
  expect(state.modelCreates[1]).toMatchObject({
    model_type: "rerank",
    model_name: "BAAI/bge-reranker-v2-m3",
  });
  expect(state.modelCreates[1]).not.toHaveProperty("embedding_dimension");

  // With active models the key badge turns into the neutral configured state.
  await expect(
    providerCard.getByText("Key configured", { exact: true }),
  ).toBeVisible();

  // Per-model probes report typed verdicts independently.
  await embeddingRow.getByRole("button", { name: "Test", exact: true }).click();
  await expect(embeddingRow.getByText("Embedding 连接测试通过")).toBeVisible();
  await rerankRow.getByRole("button", { name: "Test", exact: true }).click();
  await expect(rerankRow.getByText("Rerank 连接测试通过")).toBeVisible();
  expect(state.testedIds).toHaveLength(2);
});

test("in-use protections freeze the endpoint and lock the referenced model", async ({
  page,
}) => {
  const state = await mockRegistryRoutes(page, {
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
  });
  await page.goto("/admin/settings/models");

  const providerCard = page
    .getByTestId("admin-model-provider-list")
    .getByRole("listitem")
    .filter({ hasText: "SiliconFlow" })
    .first();
  await expect(
    providerCard.getByText(
      "The endpoint is referenced by knowledge bases. To move to a new endpoint, create a new provider and model, then rebuild each base explicitly.",
    ),
  ).toBeVisible();
  // A provider that still has models cannot be deleted.
  await expect(
    providerCard.getByRole("button", { name: "Delete", exact: true }).first(),
  ).toBeDisabled();

  const modelList = page.getByTestId("admin-provider-model-list");
  const embeddingRow = modelList
    .getByRole("listitem")
    .filter({ hasText: "Qwen/Qwen3-Embedding-8B" });
  await expect(embeddingRow.getByText("In use")).toBeVisible();
  await expect(
    embeddingRow.getByRole("button", { name: "Disable" }),
  ).toBeDisabled();
  await expect(
    embeddingRow.getByRole("button", { name: "Delete", exact: true }),
  ).toBeDisabled();

  // The unreferenced rerank model can still be disabled.
  const rerankRow = modelList
    .getByRole("listitem")
    .filter({ hasText: "BAAI/bge-reranker-v2-m3" });
  await rerankRow.getByRole("button", { name: "Disable" }).click();
  await expect(rerankRow.getByRole("button", { name: "Enable" })).toBeVisible();
  expect(state.statusPatches).toEqual([
    { id: RERANK_MODEL_ID, status: "disabled" },
  ]);

  // The frozen endpoint is not editable in place.
  await providerCard.getByRole("button", { name: "Edit" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Base URL")).toBeDisabled();
  await expect(
    dialog.getByText(
      "Referenced by knowledge bases; the endpoint cannot be changed in place. Create a new provider for a new endpoint.",
    ),
  ).toBeVisible();
});

test("editing keeps the saved key when blank and demands a new key with a new endpoint", async ({
  page,
}) => {
  const state = await mockRegistryRoutes(page, {
    providers: [providerFixture()],
  });
  await page.goto("/admin/settings/models");

  const providerCard = page
    .getByTestId("admin-model-provider-list")
    .getByRole("listitem")
    .filter({ hasText: "SiliconFlow" })
    .first();

  // A blank key on edit means "preserve the saved value": no api_key field.
  await providerCard.getByRole("button", { name: "Edit" }).click();
  let dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("API Key")).toHaveValue("");
  await dialog.getByLabel("Provider name").fill("SiliconFlow primary");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(
    page
      .getByTestId("admin-model-provider-list")
      .getByText("SiliconFlow primary"),
  ).toBeVisible();
  expect(state.providerUpdates).toHaveLength(1);
  expect(state.providerUpdates[0]).not.toHaveProperty("api_key");

  // Changing the endpoint without a fresh key is rejected before any request.
  await providerCard.getByRole("button", { name: "Edit" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Base URL").fill("https://other.example.test/v1");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(
    dialog.getByText("Changing the endpoint requires entering a new API Key."),
  ).toBeVisible();
  expect(state.providerUpdates).toHaveLength(1);

  // Providing the new key lets the endpoint change go through.
  await dialog.getByLabel("API Key").fill("sk-rotated-secret");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(state.providerUpdates).toHaveLength(2);
  expect(state.providerUpdates[1]).toMatchObject({
    base_url: "https://other.example.test/v1",
    api_key: "sk-rotated-secret",
  });
});

test("a disabled Knowledge module empties the registry section but leaves language models working", async ({
  page,
}) => {
  await mockRegistryRoutes(page, { registryDisabled: true });
  await page.goto("/admin/settings/models");

  await expect(
    page.getByTestId("admin-model-registry-disabled"),
  ).toBeVisible();
  await expect(
    page.getByText(
      "The Knowledge module is not enabled for this deployment, so retrieval model providers are unavailable.",
    ),
  ).toBeVisible();

  // The language-model section on the same page keeps its full toolbar.
  const languageSection = page
    .locator("section")
    .filter({ hasText: "System models" });
  await expect(
    languageSection.getByRole("button", { name: "Add model" }),
  ).toBeVisible();
  await expect(languageSection.getByLabel("Search models")).toBeVisible();
});
