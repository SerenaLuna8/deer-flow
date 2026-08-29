import { expect, test, type Page, type Route } from "@playwright/test";

const IN_USE_MODEL_ID = "30000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-29T00:00:00Z";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type MockModel = {
  id: string;
  display_name: string;
  status: "active" | "disabled";
  base_url: string;
  embedding_model: string;
  embedding_dimension: number;
  embedding_max_batch: number;
  reranker_model: string;
  reranker_max_batch: number;
  request_timeout_seconds: number;
  in_use: boolean;
  created_at: string;
  updated_at: string;
};

function modelFixture(overrides: Partial<MockModel> = {}): MockModel {
  return {
    id: IN_USE_MODEL_ID,
    display_name: "SiliconFlow bge-m3",
    status: "active",
    base_url: "https://api.siliconflow.cn/v1",
    embedding_model: "BAAI/bge-m3",
    embedding_dimension: 1024,
    embedding_max_batch: 64,
    reranker_model: "BAAI/bge-reranker-v2-m3",
    reranker_max_batch: 32,
    request_timeout_seconds: 30,
    in_use: true,
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
    ...overrides,
  };
}

async function mockAdminKnowledgeRoutes(
  page: Page,
  initialModels: MockModel[],
) {
  const state = {
    models: [...initialModels],
    createdPayloads: [] as Array<Record<string, unknown>>,
    updatePayloads: [] as Array<Record<string, unknown>>,
    testedIds: [] as string[],
  };

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

    const adminBase = "/api/admin/knowledge/models";
    if (path === adminBase && method === "GET") {
      return json(route, {
        items: state.models,
        total: state.models.length,
        page: 1,
        page_size: 100,
        request_id: "req-admin-list",
      });
    }
    if (path === adminBase && method === "POST") {
      const body = request.postDataJSON() as {
        display_name?: string;
        embedding_model?: string;
        reranker_model?: string;
      } & Record<string, unknown>;
      state.createdPayloads.push(body);
      const created = modelFixture({
        id: "30000000-0000-4000-8000-000000000002",
        display_name: body.display_name ?? "",
        embedding_model: body.embedding_model ?? "",
        reranker_model: body.reranker_model ?? "",
        in_use: false,
      });
      state.models.push(created);
      return json(route, { item: created, request_id: "req-admin-create" });
    }
    const testMatch =
      /\/api\/admin\/knowledge\/models\/([0-9a-f-]{36})\/test$/u.exec(path);
    if (testMatch && method === "POST") {
      state.testedIds.push(testMatch[1]!);
      return json(route, {
        ok: true,
        message: "Embedding 与 Reranker 连接测试均通过",
        request_id: "req-admin-test",
      });
    }
    const itemMatch = /\/api\/admin\/knowledge\/models\/([0-9a-f-]{36})$/u.exec(
      path,
    );
    if (itemMatch && method === "PATCH") {
      const target = state.models.find((model) => model.id === itemMatch[1]);
      if (!target) return json(route, { detail: "not found" }, 404);
      const body = request.postDataJSON() as Partial<
        Omit<MockModel, "id" | "in_use" | "created_at" | "updated_at">
      > & { api_key?: string };
      state.updatePayloads.push(body);
      if (body.status !== undefined) target.status = body.status;
      if (body.display_name !== undefined)
        target.display_name = body.display_name;
      if (body.base_url !== undefined) target.base_url = body.base_url;
      if (body.embedding_model !== undefined)
        target.embedding_model = body.embedding_model;
      if (body.embedding_dimension !== undefined)
        target.embedding_dimension = body.embedding_dimension;
      if (body.embedding_max_batch !== undefined)
        target.embedding_max_batch = body.embedding_max_batch;
      if (body.reranker_model !== undefined)
        target.reranker_model = body.reranker_model;
      if (body.reranker_max_batch !== undefined)
        target.reranker_max_batch = body.reranker_max_batch;
      if (body.request_timeout_seconds !== undefined)
        target.request_timeout_seconds = body.request_timeout_seconds;
      return json(route, { item: target, request_id: "req-admin-update" });
    }
    if (itemMatch && method === "DELETE") {
      state.models = state.models.filter((model) => model.id !== itemMatch[1]);
      return json(route, { request_id: "req-admin-delete" });
    }

    return json(route, { detail: "not found" }, 404);
  });

  return state;
}

test("creates a configuration and reports a joint embedding+reranker test verdict", async ({
  page,
}) => {
  const state = await mockAdminKnowledgeRoutes(page, []);
  await page.goto("/admin/settings/knowledge");

  await expect(
    page.getByText("No knowledge model configurations yet."),
  ).toBeVisible();

  await page.getByRole("button", { name: "Add configuration" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Display name").fill("SiliconFlow primary");
  await dialog
    .getByLabel("Embedding model", { exact: true })
    .fill("BAAI/bge-m3");
  await dialog
    .getByLabel("Reranker model", { exact: true })
    .fill("BAAI/bge-reranker-v2-m3");
  await dialog.getByLabel("API Key").fill("sk-test-key");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();

  const list = page.getByTestId("admin-knowledge-model-list");
  await expect(list.getByText("SiliconFlow primary")).toBeVisible();
  expect(state.createdPayloads[0]?.api_key).toBe("sk-test-key");
  expect(state.createdPayloads[0]?.embedding_dimension).toBe(1024);

  const row = list
    .getByRole("listitem")
    .filter({ hasText: "SiliconFlow primary" });
  await row.getByRole("button", { name: "Test connection" }).click();
  await expect(
    row.getByText("Embedding 与 Reranker 连接测试均通过"),
  ).toBeVisible();
  expect(state.testedIds).toEqual(["30000000-0000-4000-8000-000000000002"]);
});

test("a configuration referenced by bases cannot be disabled or deleted", async ({
  page,
}) => {
  await mockAdminKnowledgeRoutes(page, [modelFixture({ in_use: true })]);
  await page.goto("/admin/settings/knowledge");

  const row = page
    .getByTestId("admin-knowledge-model-list")
    .getByRole("listitem")
    .filter({ hasText: "SiliconFlow bge-m3" });
  await expect(row.getByText("In use")).toBeVisible();
  await expect(row.getByRole("button", { name: "Disable" })).toBeDisabled();
  await expect(row.getByRole("button", { name: "Delete" })).toBeDisabled();
});

test("edits a configuration and preserves the saved key when left blank", async ({
  page,
}) => {
  const state = await mockAdminKnowledgeRoutes(page, [
    modelFixture({
      id: "30000000-0000-4000-8000-000000000004",
      display_name: "Old name",
      in_use: false,
    }),
  ]);
  await page.goto("/admin/settings/knowledge");

  const list = page.getByTestId("admin-knowledge-model-list");
  const row = list.getByRole("listitem").filter({ hasText: "Old name" });
  await row.getByRole("button", { name: "Edit" }).click();

  const dialog = page.getByRole("dialog");
  // The form prefills from the saved configuration; the key stays blank.
  await expect(dialog.getByLabel("Display name")).toHaveValue("Old name");
  await expect(dialog.getByLabel("Embedding dimension")).toHaveValue("1024");
  await expect(dialog.getByLabel("API Key")).toHaveValue("");

  await dialog.getByLabel("Display name").fill("Primary embedder");
  await dialog
    .getByLabel("Embedding model", { exact: true })
    .fill("Qwen/Qwen3-Embedding-8B");
  await dialog.getByLabel("Embedding dimension").fill("4096");
  await dialog.getByLabel("Request timeout (seconds)").fill("60");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();

  await expect(list.getByText("Primary embedder")).toBeVisible();
  await expect(
    list.getByText("Qwen/Qwen3-Embedding-8B (4096)", { exact: false }),
  ).toBeVisible();

  const payload = state.updatePayloads[0];
  expect(payload?.display_name).toBe("Primary embedder");
  expect(payload?.embedding_dimension).toBe(4096);
  expect(payload?.request_timeout_seconds).toBe(60);
  // A blank key means "preserve the saved value": no api_key field at all.
  expect(payload !== undefined && "api_key" in payload).toBe(false);
});

test("an unused configuration can be disabled and deleted", async ({
  page,
}) => {
  await mockAdminKnowledgeRoutes(page, [
    modelFixture({
      id: "30000000-0000-4000-8000-000000000003",
      display_name: "Backup embedder",
      in_use: false,
    }),
  ]);
  await page.goto("/admin/settings/knowledge");

  const list = page.getByTestId("admin-knowledge-model-list");
  const row = list.getByRole("listitem").filter({ hasText: "Backup embedder" });
  await row.getByRole("button", { name: "Disable" }).click();
  await expect(row.getByRole("button", { name: "Enable" })).toBeVisible();

  await row.getByRole("button", { name: "Delete" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(
    page.getByText("No knowledge model configurations yet."),
  ).toBeVisible();
});
