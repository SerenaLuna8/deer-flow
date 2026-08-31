import { expect, test, type APIResponse, type Page } from "@playwright/test";

import {
  registerReplayProject,
  type ReplayProjectScope,
} from "./project-fixture";
import { browserPersistenceSecretLocations } from "./secret-browser-fixture";

const APP =
  process.env.E2E_APP_URL ??
  `http://localhost:${process.env.E2E_FRONTEND_PORT ?? "3000"}`;

const MODEL_FIRST = "playwright-model-first-never-expose";
const MODEL_SECOND = "playwright-model-second-never-expose";
const MCP_DELETED = "playwright-mcp-deleted-never-expose";
const MCP_FIRST = "playwright-mcp-first-never-expose";
const MCP_QUERY_FIRST = "playwright-mcp-query-first-never-expose";
const MCP_SECOND = "playwright-mcp-second-never-expose";
const MCP_QUERY_SECOND = "playwright-mcp-query-second-never-expose";
const CHANNEL_FIRST = "playwright-channel-first-never-expose";
const CHANNEL_SECOND = "playwright-channel-second-never-expose";

type SafeResponse = Pick<APIResponse, "headers" | "status" | "text">;

async function expectSafeJson(
  response: SafeResponse,
  forbiddenValues: readonly string[],
): Promise<Record<string, unknown>> {
  const text = await response.text();
  expect(response.status(), text).toBe(200);
  for (const value of forbiddenValues) expect(text).not.toContain(value);
  return JSON.parse(text) as Record<string, unknown>;
}

async function expectSafeModelResponse(
  response: SafeResponse,
  configured: boolean,
  forbiddenValues: readonly string[],
): Promise<{ secretRevision: number; modelId: string }> {
  const body = await expectSafeJson(response, forbiddenValues);
  const item = body.item as Record<string, unknown>;
  expect(item.api_key_configured).toBe(configured);
  expect(item.secret_readiness).toBe(configured ? "ready" : "unready");
  expect(typeof item.secret_revision).toBe("number");
  expect(typeof item.id).toBe("string");
  expect(Object.keys(item)).not.toContain("api_key");
  return {
    secretRevision: item.secret_revision as number,
    modelId: item.id as string,
  };
}

async function expectSafeProviderResponse(
  response: SafeResponse,
  forbiddenValues: readonly string[],
): Promise<{ providerId: string }> {
  const body = await expectSafeJson(response, forbiddenValues);
  const item = body.item as Record<string, unknown>;
  expect(item.api_key_configured).toBe(true);
  expect(typeof item.id).toBe("string");
  expect(Object.keys(item)).not.toContain("api_key");
  return { providerId: item.id as string };
}

async function expectSafeMcpResponse(
  response: SafeResponse,
  configured: boolean,
  forbiddenValues: readonly string[],
): Promise<{ revision: number }> {
  const body = await expectSafeJson(response, forbiddenValues);
  expect(response.headers()["cache-control"]).toContain("no-store");
  expect(Object.keys(body).sort()).toEqual([
    "mcp_server_id",
    "mcp_server_version_id",
    "readiness",
    "request_id",
    "revision",
    "slots",
  ]);
  expect(body.readiness).toBe(configured ? "ready" : "unready");
  const slots = body.slots as Array<Record<string, unknown>>;
  expect(slots).toHaveLength(1);
  expect(Object.keys(slots[0]!).sort()).toEqual([
    "configured",
    "id",
    "name",
    "payload_schema",
    "purpose",
    "required",
    "revision",
  ]);
  expect(slots[0]).toMatchObject({
    name: "auth",
    required: true,
    configured,
  });
  expect(Object.keys(slots[0]!)).not.toContain("payload");
  expect(typeof body.revision).toBe("number");
  return { revision: body.revision as number };
}

async function expectSafeChannelResponse(
  response: SafeResponse,
  configured: boolean,
  forbiddenValues: readonly string[],
): Promise<{ revision: number }> {
  const body = await expectSafeJson(response, forbiddenValues);
  expect(body.secret_configured).toBe(configured);
  expect(body.secret_readiness).toBe(configured ? "ready" : "unready");
  expect(typeof body.secret_revision).toBe("number");
  expect(Object.keys(body)).not.toContain("secrets");
  return { revision: body.secret_revision as number };
}

function modelCard(page: Page, displayName: string) {
  return page.locator('[data-slot="card"]').filter({
    has: page.getByText(displayName, { exact: true }),
  });
}

test.describe("Domain-owned configuration secrets (real Gateway)", () => {
  let project: ReplayProjectScope;

  test.beforeEach(async ({ context }) => {
    project = await registerReplayProject(context, APP);
  });

  test("keeps Model Provider API keys write-only across create, blank preserve, and rotation", async ({
    page,
    context,
  }) => {
    await context.clearCookies();
    await context.addCookies([
      {
        name: "locale",
        value: "zh-CN",
        url: APP,
      },
    ]);
    const suffix = project.id.slice(0, 8);
    const providerName = `Browser secret provider ${suffix}`;
    const displayName = `Browser secret model ${suffix}`;
    const providerModel = `browser-secret-${suffix}`;

    // The provider owns the only API Key; text models carry no key of their own.
    await page.goto("/admin/settings/models");
    await page.getByRole("button", { name: "添加供应商" }).click();
    const providerDialog = page.getByRole("dialog", {
      name: "添加模型供应商",
    });
    await providerDialog.getByLabel("供应商名称").fill(providerName);
    await providerDialog
      .getByLabel("Base URL")
      .fill(`https://provider-${suffix}.invalid/v1`);
    const providerKeyInput = providerDialog.getByLabel("API Key");
    await providerKeyInput.fill(MODEL_FIRST);
    const providerCreatedPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/api/admin/settings/model-providers"),
    );
    await providerDialog.getByRole("button", { name: "保存" }).click();
    const provider = await expectSafeProviderResponse(
      await providerCreatedPromise,
      [MODEL_FIRST],
    );
    await expect(providerDialog).toBeHidden();

    const providerCard = page
      .getByTestId("admin-model-provider-card")
      .filter({ hasText: providerName })
      .first();
    await expect(
      providerCard.getByText("Key 已配置", { exact: true }),
    ).toBeVisible();

    // A text model binds the provider and derives credential and endpoint.
    await providerCard.getByRole("button", { name: "添加文本模型" }).click();
    const modelDialog = page.getByRole("dialog", { name: "添加文本模型" });
    await expect(modelDialog.getByLabel("API Key")).toHaveCount(0);
    await expect(
      modelDialog.getByLabel("所属供应商", { exact: true }),
    ).toHaveValue(provider.providerId);
    await modelDialog.getByLabel("显示名称").fill(displayName);
    await modelDialog
      .getByRole("combobox", { name: "适配器", exact: true })
      .selectOption("deepseek");
    await modelDialog.getByLabel("供应商侧模型 ID").fill(providerModel);
    await modelDialog
      .getByRole("spinbutton", { name: /最大输入 Token/u })
      .fill("64000");
    const modelCreatedPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith("/api/admin/settings/models"),
    );
    await modelDialog.getByRole("button", { name: "保存" }).click();
    const created = await expectSafeModelResponse(
      await modelCreatedPromise,
      true,
      [MODEL_FIRST],
    );
    await expect(modelDialog).toBeHidden();

    const card = modelCard(page, displayName);
    await expect(card.getByText("已配置", { exact: true })).toBeVisible();
    await expect(card.getByText("就绪", { exact: true })).toBeVisible();

    // A blank key on provider edit preserves the stored secret unchanged.
    await providerCard
      .getByRole("button", { name: "编辑", exact: true })
      .first()
      .click();
    const preserveDialog = page.getByRole("dialog", {
      name: "编辑模型供应商",
    });
    const preserveInput = preserveDialog.getByLabel("API Key");
    await expect(preserveInput).toHaveValue("");
    const preserveRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "PATCH" &&
        request
          .url()
          .endsWith(
            `/api/admin/settings/model-providers/${provider.providerId}`,
          ),
    );
    const preserveResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PATCH" &&
        response
          .url()
          .endsWith(
            `/api/admin/settings/model-providers/${provider.providerId}`,
          ),
    );
    await preserveDialog.getByRole("button", { name: "保存" }).click();
    const preserveBody = (
      await preserveRequestPromise
    ).postDataJSON() as Record<string, unknown>;
    expect(preserveBody).not.toHaveProperty("api_key");
    await expectSafeProviderResponse(await preserveResponsePromise, [
      MODEL_FIRST,
    ]);
    await expect(preserveDialog).toBeHidden();

    // Rotating the provider key fans out to every bound text model.
    await providerCard
      .getByRole("button", { name: "编辑", exact: true })
      .first()
      .click();
    const rotateDialog = page.getByRole("dialog", { name: "编辑模型供应商" });
    const rotateInput = rotateDialog.getByLabel("API Key");
    await rotateInput.fill(MODEL_SECOND);
    await expect(
      page.getByTestId("admin-provider-fanout-warning"),
    ).toBeVisible();
    const rotateRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "PATCH" &&
        request
          .url()
          .endsWith(
            `/api/admin/settings/model-providers/${provider.providerId}`,
          ),
    );
    const rotateResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PATCH" &&
        response
          .url()
          .endsWith(
            `/api/admin/settings/model-providers/${provider.providerId}`,
          ),
    );
    const catalogRefreshPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        response.url().endsWith("/api/admin/settings/models"),
    );
    await rotateDialog.getByRole("button", { name: "保存" }).click();
    expect((await rotateRequestPromise).postDataJSON()).toMatchObject({
      api_key: MODEL_SECOND,
    });
    await expectSafeProviderResponse(await rotateResponsePromise, [
      MODEL_FIRST,
      MODEL_SECOND,
    ]);
    await expect(rotateDialog).toBeHidden();

    const catalogText = await (await catalogRefreshPromise).text();
    expect(catalogText).not.toContain(MODEL_FIRST);
    expect(catalogText).not.toContain(MODEL_SECOND);
    const catalogBody = JSON.parse(catalogText) as {
      items: Array<{ id: string; secret_revision: number }>;
    };
    const rotatedModel = catalogBody.items.find(
      (item) => item.id === created.modelId,
    );
    expect(rotatedModel?.secret_revision).toBe(created.secretRevision + 1);

    expect(
      await browserPersistenceSecretLocations(page, [
        MODEL_FIRST,
        MODEL_SECOND,
      ]),
    ).toEqual([]);
  });

  test("keeps Project MCP slots write-only across replace and confirmed clear", async ({
    page,
  }) => {
    const suffix = project.id.slice(0, 8);
    const displayName = `Browser MCP ${suffix}`;
    const slug = `browser-mcp-${suffix}`;

    await page.goto(`/projects/${encodeURIComponent(project.slug)}/mcp`);
    await page.getByRole("tab", { name: /项目自建/u }).click();
    await page.getByRole("button", { name: "添加 MCP" }).click();
    const createDialog = page.getByRole("dialog", {
      name: "新增 Project MCP",
    });
    await createDialog.getByLabel("名称").fill(displayName);
    await createDialog.getByLabel("标识").fill(slug);
    await createDialog.getByLabel("URL").fill("http://127.0.0.1:65535/mcp");
    await expect(createDialog.getByLabel("凭证发送方式")).toHaveCount(0);
    await expect(createDialog.getByLabel("槽位名")).toHaveCount(0);
    await createDialog.getByRole("button", { name: "添加凭证参数" }).click();
    const headerNames = createDialog.getByLabel("请求头名称", { exact: true });
    await headerNames.fill("X-Temporary-Test");
    await createDialog
      .getByLabel("X-Temporary-Test 的凭证值", { exact: true })
      .fill(MCP_DELETED);
    await createDialog.getByRole("button", { name: "添加凭证参数" }).click();
    await expect(headerNames).toHaveCount(2);
    await headerNames.nth(1).fill("Authorization");
    const createSecretInputByLabel = createDialog.getByLabel(
      "Authorization 的凭证值",
      {
        exact: true,
      },
    );
    await expect(createSecretInputByLabel).toHaveAttribute(
      "name",
      "project_mcp_secret_1",
    );
    const createSecretInput = createDialog.locator(
      'input[name="project_mcp_secret_1"]',
    );
    await createSecretInput.pressSequentially(MCP_FIRST);
    await headerNames.nth(1).fill("Authorization-Edited");
    await expect(createSecretInput).toHaveValue(MCP_FIRST);
    await headerNames.nth(1).fill("Authorization");
    await expect(createSecretInput).toHaveValue(MCP_FIRST);
    await createDialog.getByRole("button", { name: "添加凭证参数" }).click();
    await expect(headerNames).toHaveCount(3);
    const querySecretInput = createDialog.locator(
      'input[name="project_mcp_secret_2"]',
    );
    await querySecretInput.fill(MCP_DELETED);
    await createDialog
      .getByLabel("第 3 个凭证参数的发送位置")
      .selectOption("query");
    await expect(querySecretInput).toHaveValue("");
    const queryNames = createDialog.getByLabel("查询参数名称", {
      exact: true,
    });
    await queryNames.fill("api_key");
    await querySecretInput.fill(MCP_QUERY_FIRST);
    await createDialog
      .getByRole("button", { name: "删除第 1 个凭证参数" })
      .click();
    await expect(headerNames).toHaveCount(1);
    await expect(headerNames).toHaveValue("Authorization");
    await expect(queryNames).toHaveValue("api_key");
    await expect(createSecretInput).toHaveAttribute("type", "password");
    await expect(createSecretInput).toHaveValue(MCP_FIRST);
    await expect(querySecretInput).toHaveValue(MCP_QUERY_FIRST);
    await createSecretInput.press("Tab");
    await page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => resolve());
        }),
    );
    await expect(createSecretInput).toHaveValue(MCP_FIRST);
    expect(await createSecretInput.getAttribute("name")).toBe(
      "project_mcp_secret_1",
    );
    expect(
      await createSecretInput.evaluate((input: HTMLInputElement) => {
        if (!input.form) throw new Error("MCP secret input has no form");
        return new FormData(input.form).get(input.name);
      }),
    ).toBe(MCP_FIRST);
    expect(
      await querySecretInput.evaluate((input: HTMLInputElement) => {
        if (!input.form) throw new Error("MCP query secret input has no form");
        return new FormData(input.form).get(input.name);
      }),
    ).toBe(MCP_QUERY_FIRST);
    const createRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        request
          .url()
          .endsWith(`/api/projects/${project.id}/mcp-servers/configured`),
    );
    const createResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response
          .url()
          .endsWith(`/api/projects/${project.id}/mcp-servers/configured`),
    );
    const firstSecretResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response.url().includes(`/api/projects/${project.id}/mcp-servers/`) &&
        response.url().includes("/secrets/auth"),
    );
    await createDialog.getByRole("button", { name: "保存" }).click();
    await expect(createSecretInput).toHaveValue("");
    await expect(querySecretInput).toHaveValue("");
    expect((await createRequestPromise).postDataJSON()).toMatchObject({
      secret_slots: [
        {
          name: "auth",
          payload_schema: {
            headers: ["Authorization"],
            query: ["api_key"],
          },
        },
      ],
    });
    const createResponse = await createResponsePromise;
    const createText = await createResponse.text();
    expect(createResponse.status(), createText).toBe(201);
    expect(createText).not.toContain(MCP_DELETED);
    expect(createText).not.toContain(MCP_FIRST);
    expect(createText).not.toContain(MCP_QUERY_FIRST);
    const createBody = JSON.parse(createText) as {
      item?: { id?: unknown };
      version?: { id?: unknown };
    };
    const assetId = createBody.item?.id;
    const versionOneId = createBody.version?.id;
    if (typeof assetId !== "string" || typeof versionOneId !== "string") {
      throw new Error("Configured MCP response is missing stable IDs");
    }
    const firstSecretResponse = await firstSecretResponsePromise;
    expect(firstSecretResponse.request().postDataJSON()).toEqual({
      payload: {
        headers: { Authorization: MCP_FIRST },
        query: { api_key: MCP_QUERY_FIRST },
      },
    });
    const first = await expectSafeMcpResponse(firstSecretResponse, true, [
      MCP_DELETED,
      MCP_FIRST,
      MCP_QUERY_FIRST,
    ]);
    await expect(createDialog).toBeHidden();

    const detail = page.getByRole("dialog", { name: displayName });
    await expect(detail).toBeVisible();
    const secrets = detail.getByRole("region", { name: "MCP 秘密配置" });
    await expect(
      secrets.getByText("必需 · 已配置", { exact: true }),
    ).toBeVisible();
    const secretInput = secrets.getByLabel("auth headers Authorization 秘密值");
    const querySecret = secrets.getByLabel("auth query api_key 秘密值");
    await expect(secretInput).toHaveValue("");
    await expect(querySecret).toHaveValue("");
    await expect(
      secrets.getByRole("button", { name: "替换此槽位" }),
    ).toBeDisabled();

    await detail.getByRole("button", { name: "编辑配置" }).click();
    const editDialog = page.getByRole("dialog", {
      name: "编辑 Project MCP",
    });
    await editDialog.getByLabel("URL").fill("http://127.0.0.1:65535/mcp-v2");
    await expect(
      editDialog.getByLabel("Authorization 的凭证值", { exact: true }),
    ).toHaveValue("");
    await expect(
      editDialog.getByLabel("api_key 的凭证值", { exact: true }),
    ).toHaveValue("");
    const updateResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response
          .url()
          .endsWith(
            `/api/projects/${project.id}/mcp-servers/${assetId}/configured`,
          ),
    );
    await editDialog.getByRole("button", { name: "保存" }).click();
    const updateResponse = await updateResponsePromise;
    const updateText = await updateResponse.text();
    expect(updateResponse.status(), updateText).toBe(200);
    expect(updateText).not.toContain(MCP_FIRST);
    expect(updateText).not.toContain(MCP_QUERY_FIRST);
    const updateBody = JSON.parse(updateText) as {
      version?: { id?: unknown; supersedes_version_id?: unknown };
    };
    const versionTwoId = updateBody.version?.id;
    if (typeof versionTwoId !== "string") {
      throw new Error("Updated MCP response is missing its Version ID");
    }
    expect(updateBody.version?.supersedes_version_id).toBe(versionOneId);
    await expect(editDialog).toBeHidden();

    const versionPicker = detail.getByLabel("查看配置");
    await expect(versionPicker).toBeVisible();
    await expect(versionPicker.locator("option")).toHaveCount(2);
    await expect(
      versionPicker.locator(`option[value="${versionOneId}"]`),
    ).toHaveText("配置 1 · 已发布");
    await expect(
      versionPicker.locator(`option[value="${versionTwoId}"]`),
    ).toHaveText("配置 2 · 当前配置");
    await expect(
      detail.locator("p").filter({ hasText: "配置 2 · 已发布" }),
    ).toBeVisible();

    const copiedVersionStatus = await expectSafeMcpResponse(
      await page.request.get(
        `${APP}/api/projects/${project.id}/mcp-servers/${assetId}/versions/${versionTwoId}/secrets`,
      ),
      true,
      [MCP_FIRST, MCP_QUERY_FIRST],
    );
    const retainedStatusPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        response
          .url()
          .endsWith(
            `/api/projects/${project.id}/mcp-servers/${assetId}/versions/${versionOneId}/secrets`,
          ),
    );
    await versionPicker.selectOption(versionOneId);
    await expect(versionPicker).toHaveValue(versionOneId);
    await expect(
      detail.locator("p").filter({ hasText: "配置 2 · 已发布" }),
    ).toBeVisible();
    await expectSafeMcpResponse(await retainedStatusPromise, true, [
      MCP_FIRST,
      MCP_QUERY_FIRST,
    ]);

    await secretInput.fill(MCP_SECOND);
    await querySecret.fill(MCP_QUERY_SECOND);
    const replacementResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response
          .url()
          .endsWith(
            `/api/projects/${project.id}/mcp-servers/${assetId}/versions/${versionOneId}/secrets/auth`,
          ),
    );
    const exactInventoryRefreshPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        response
          .url()
          .endsWith(
            `/api/projects/${project.id}/mcp-servers/${assetId}/versions/${versionOneId}/tools`,
          ),
    );
    await secrets.getByRole("button", { name: "替换此槽位" }).click();
    await expect(secretInput).toHaveValue("");
    await expect(querySecret).toHaveValue("");
    const replacementResponse = await replacementResponsePromise;
    expect(replacementResponse.request().postDataJSON()).toEqual({
      payload: {
        headers: { Authorization: MCP_SECOND },
        query: { api_key: MCP_QUERY_SECOND },
      },
    });
    const replaced = await expectSafeMcpResponse(replacementResponse, true, [
      MCP_FIRST,
      MCP_QUERY_FIRST,
      MCP_SECOND,
      MCP_QUERY_SECOND,
    ]);
    expect(replaced.revision).toBe(first.revision + 1);
    expect((await exactInventoryRefreshPromise).status()).toBe(200);
    await expect(versionPicker).toHaveValue(versionOneId);
    const copiedAfterReplacement = await expectSafeMcpResponse(
      await page.request.get(
        `${APP}/api/projects/${project.id}/mcp-servers/${assetId}/versions/${versionTwoId}/secrets`,
      ),
      true,
      [MCP_FIRST, MCP_QUERY_FIRST, MCP_SECOND, MCP_QUERY_SECOND],
    );
    expect(copiedAfterReplacement.revision).toBe(copiedVersionStatus.revision);

    await secrets.getByRole("button", { name: "清除" }).click();
    const clearDialog = page.getByRole("dialog", {
      name: "清除此 MCP 秘密槽位？",
    });
    const clearRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        request
          .url()
          .endsWith(
            `/api/projects/${project.id}/mcp-servers/${assetId}/versions/${versionOneId}/secrets/auth/clear`,
          ),
    );
    const clearResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response
          .url()
          .endsWith(
            `/api/projects/${project.id}/mcp-servers/${assetId}/versions/${versionOneId}/secrets/auth/clear`,
          ),
    );
    await clearDialog.getByRole("button", { name: "确认清除" }).click();
    expect((await clearRequestPromise).postDataJSON()).toEqual({
      confirmed: true,
    });
    const cleared = await expectSafeMcpResponse(
      await clearResponsePromise,
      false,
      [MCP_FIRST, MCP_QUERY_FIRST, MCP_SECOND, MCP_QUERY_SECOND],
    );
    expect(cleared.revision).toBe(first.revision + 2);
    await expect(
      secrets.getByText("必需 · 未配置", { exact: true }),
    ).toBeVisible();
    await expect(secretInput).toHaveValue("");
    await expect(querySecret).toHaveValue("");
    const copiedAfterClear = await expectSafeMcpResponse(
      await page.request.get(
        `${APP}/api/projects/${project.id}/mcp-servers/${assetId}/versions/${versionTwoId}/secrets`,
      ),
      true,
      [MCP_FIRST, MCP_QUERY_FIRST, MCP_SECOND, MCP_QUERY_SECOND],
    );
    expect(copiedAfterClear.revision).toBe(copiedVersionStatus.revision);

    expect(
      await browserPersistenceSecretLocations(page, [
        MCP_DELETED,
        MCP_FIRST,
        MCP_QUERY_FIRST,
        MCP_SECOND,
        MCP_QUERY_SECOND,
      ]),
    ).toEqual([]);
  });

  test("keeps Project Channel secrets write-only across blank preserve, replace, and confirmed clear", async ({
    page,
    context,
  }) => {
    const seedResponse = await context.request.put(
      `${APP}/api/projects/${project.id}/channel-instances/feishu`,
      {
        headers: { "X-CSRF-Token": project.csrf },
        data: {
          public_config: {
            app_id: `cli_${project.id.replaceAll("-", "").slice(0, 16)}`,
            domain: "https://open.feishu.cn",
          },
          secrets: { app_secret: CHANNEL_FIRST },
          enabled: false,
        },
      },
    );
    const seeded = await expectSafeChannelResponse(seedResponse, true, [
      CHANNEL_FIRST,
    ]);

    await page.goto(
      `/projects/${encodeURIComponent(project.slug)}/connections`,
    );
    const card = page.getByRole("listitem").filter({ hasText: "飞书" });
    await expect(
      card.getByText("App Secret 已配置", { exact: true }),
    ).toBeVisible();
    await expect(card.getByText("已停用", { exact: true })).toBeVisible();

    await card.getByRole("button", { name: "修改" }).click();
    const preserveDialog = page.getByRole("dialog", { name: "修改飞书" });
    const preserveInput = preserveDialog.getByLabel("App Secret");
    await expect(preserveInput).toHaveValue("");
    const preserveRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "PUT" &&
        request
          .url()
          .endsWith(`/api/projects/${project.id}/channel-instances/feishu`),
    );
    const preserveResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response
          .url()
          .endsWith(`/api/projects/${project.id}/channel-instances/feishu`),
    );
    await preserveDialog.getByRole("button", { name: "保存" }).click();
    const preserveBody = (
      await preserveRequestPromise
    ).postDataJSON() as Record<string, unknown>;
    expect(preserveBody).not.toHaveProperty("secrets");
    expect(preserveBody.enabled).toBe(false);
    const preserved = await expectSafeChannelResponse(
      await preserveResponsePromise,
      true,
      [CHANNEL_FIRST],
    );
    expect(preserved.revision).toBe(seeded.revision);

    await card.getByRole("button", { name: "修改" }).click();
    const replaceDialog = page.getByRole("dialog", { name: "修改飞书" });
    const replaceInput = replaceDialog.getByLabel("App Secret");
    await replaceInput.fill(CHANNEL_SECOND);
    const replaceRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "PUT" &&
        request
          .url()
          .endsWith(`/api/projects/${project.id}/channel-instances/feishu`),
    );
    const replaceResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response
          .url()
          .endsWith(`/api/projects/${project.id}/channel-instances/feishu`),
    );
    await replaceDialog.getByRole("button", { name: "保存" }).click();
    await expect(replaceInput).toHaveValue("");
    expect((await replaceRequestPromise).postDataJSON()).toMatchObject({
      secrets: { app_secret: CHANNEL_SECOND },
      enabled: false,
    });
    const replaced = await expectSafeChannelResponse(
      await replaceResponsePromise,
      true,
      [CHANNEL_FIRST, CHANNEL_SECOND],
    );
    expect(replaced.revision).toBe(seeded.revision + 1);

    await card.getByRole("button", { name: "清除秘密" }).click();
    const clearDialog = page.getByRole("dialog", { name: "清除渠道秘密" });
    const clearRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        request
          .url()
          .endsWith(
            `/api/projects/${project.id}/channel-instances/feishu/secret/clear`,
          ),
    );
    const clearResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response
          .url()
          .endsWith(
            `/api/projects/${project.id}/channel-instances/feishu/secret/clear`,
          ),
    );
    await clearDialog.getByRole("button", { name: "确认清除" }).click();
    expect((await clearRequestPromise).postDataJSON()).toEqual({
      confirmed: true,
    });
    const cleared = await expectSafeChannelResponse(
      await clearResponsePromise,
      false,
      [CHANNEL_FIRST, CHANNEL_SECOND],
    );
    expect(cleared.revision).toBe(seeded.revision + 2);
    await expect(card.getByText("秘密未配置", { exact: true })).toBeVisible();

    expect(
      await browserPersistenceSecretLocations(page, [
        CHANNEL_FIRST,
        CHANNEL_SECOND,
      ]),
    ).toEqual([]);
  });
});
