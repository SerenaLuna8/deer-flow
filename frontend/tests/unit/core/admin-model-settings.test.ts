import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import {
  adminModelCatalogSchema,
  adminModelSettingsQueryKey,
  abortAdminModelSettingsAccount,
  createAdminModel,
  createAdminModelInputSchema,
  fetchAdminModelCatalog,
  replaceAdminModel,
  replaceAdminModelInputSchema,
  runAbortableAdminModelMutation,
  setAdminModelDefault,
  setAdminModelStatus,
} from "@/core/admin-settings/models";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";

const mockedFetch = rs.mocked(fetchWithAuth);
const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const MODEL_ID = "22222222-2222-4222-8222-222222222222";
const CREDENTIAL_ID = "33333333-3333-4333-8333-333333333333";
const CREDENTIAL_VERSION_ID = "44444444-4444-4444-8444-444444444444";

const model = {
  id: MODEL_ID,
  logical_name: "analysis-pro",
  display_name: "分析模型 Pro",
  description: "适合深度分析任务",
  provider_adapter: "openai",
  provider_model: "gpt-5.2",
  settings: {
    base_url: "https://models.example.invalid/v1",
    temperature: 0.2,
    max_tokens: 16384,
  },
  supports_thinking: true,
  supports_reasoning_effort: true,
  supports_vision: false,
  status: "active",
  is_default: true,
  revision: 3,
  version_number: 2,
  credential_id: CREDENTIAL_ID,
  credential_version_id: CREDENTIAL_VERSION_ID,
  credential_env_key: "OPENAI_API_KEY",
  sort_order: 0,
  updated_at: "2026-07-31T08:00:00Z",
} as const;

const catalog = {
  items: [model],
  catalog_revision: 7,
  request_id: "req-model-catalog",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("admin model settings contract", () => {
  test("accepts the safe catalog shape and rejects extra or nested secret fields", () => {
    expect(adminModelCatalogSchema.parse(catalog)).toEqual(catalog);

    expect(
      adminModelCatalogSchema.safeParse({
        ...catalog,
        items: [{ ...model, ciphertext: "must-never-leave-gateway" }],
      }).success,
    ).toBe(false);
    expect(
      adminModelCatalogSchema.safeParse({
        ...catalog,
        items: [
          {
            ...model,
            settings: {
              ...model.settings,
              api_key: "must-never-enter-query-cache",
            },
          },
        ],
      }).success,
    ).toBe(false);
    expect(
      adminModelCatalogSchema.safeParse({
        ...catalog,
        items: [
          { ...model, is_default: true },
          { ...model, is_default: true },
        ],
      }).success,
    ).toBe(false);
  });

  test("validates safe write inputs without allowing secret-bearing settings", () => {
    const input = {
      logical_name: model.logical_name,
      display_name: model.display_name,
      description: model.description,
      provider_adapter: model.provider_adapter,
      provider_model: model.provider_model,
      settings: model.settings,
      supports_thinking: model.supports_thinking,
      supports_reasoning_effort: model.supports_reasoning_effort,
      supports_vision: model.supports_vision,
      status: model.status,
      credential_id: model.credential_id,
      credential_version_id: model.credential_version_id,
      credential_env_key: model.credential_env_key,
      sort_order: model.sort_order,
    };

    expect(createAdminModelInputSchema.parse(input)).toEqual(input);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        settings: { default_headers: { Authorization: "Bearer secret" } },
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        settings: { arbitrary_provider_json: { mode: "custom" } },
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        settings: {
          base_url: "https://user@example.invalid/v1?token=redacted#models",
        },
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        provider_adapter: "codex_cli",
        settings: { base_url: "https://example.invalid/v1" },
        credential_id: null,
        credential_version_id: null,
        credential_env_key: null,
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        logical_name: "sk-proj-example-secret-value",
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        display_name: "Bearer abcdefghijklmnop",
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        description: "api_key=must-not-enter-public-cache",
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        provider_adapter: "langchain_openai:ChatOpenAI",
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        credential_id: null,
        credential_version_id: null,
        credential_env_key: null,
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        provider_adapter: "codex_cli",
        settings: { reasoning_effort: "high", retry_max_attempts: 2 },
        credential_id: null,
        credential_version_id: null,
        credential_env_key: null,
      }).success,
    ).toBe(true);

    expect(
      createAdminModelInputSchema.safeParse({
        ...input,
        provider_adapter: "patched_deepseek",
        settings: {
          base_url: "https://api.deepseek.com/v1",
          request_timeout: 120,
          max_retries: 3,
          max_tokens: 8192,
          temperature: 0.2,
          reasoning_effort: "medium",
          when_thinking_enabled: {
            extra_body: { thinking: { type: "enabled" } },
          },
          when_thinking_disabled: {
            extra_body: { thinking: { type: "disabled" } },
          },
          extra_body: { reasoning: { effort: "medium" } },
        },
      }).success,
    ).toBe(true);

    const replaceInput = {
      display_name: input.display_name,
      description: input.description,
      provider_adapter: input.provider_adapter,
      provider_model: input.provider_model,
      settings: input.settings,
      supports_thinking: input.supports_thinking,
      supports_reasoning_effort: input.supports_reasoning_effort,
      supports_vision: input.supports_vision,
      credential_id: input.credential_id,
      credential_version_id: input.credential_version_id,
      credential_env_key: input.credential_env_key,
      sort_order: input.sort_order,
      expected_revision: model.revision,
    };
    expect(replaceAdminModelInputSchema.parse(replaceInput)).toEqual(
      replaceInput,
    );
    expect(
      replaceAdminModelInputSchema.safeParse({
        ...replaceInput,
        logical_name: model.logical_name,
      }).success,
    ).toBe(false);
    expect(
      replaceAdminModelInputSchema.safeParse({
        ...replaceInput,
        status: "active",
      }).success,
    ).toBe(false);
  });

  test("uses an account-scoped catalog key", () => {
    expect(adminModelSettingsQueryKey(ACCOUNT_ID)).toEqual([
      "account",
      ACCOUNT_ID,
      "admin",
      "settings",
      "models",
    ]);
    expect(() => adminModelSettingsQueryKey("")).toThrow();
  });

  test("loads the catalog through authenticated fetch and forwards AbortSignal", async () => {
    const controller = new AbortController();
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, catalog));

    await expect(
      fetchAdminModelCatalog(ACCOUNT_ID, controller.signal),
    ).resolves.toEqual(catalog);
    expect(mockedFetch).toHaveBeenCalledWith(
      "/backend/api/admin/settings/models",
      { signal: controller.signal },
    );
  });

  test("aborts account-scoped model mutations during an identity transition", async () => {
    let capturedSignal: AbortSignal | undefined;
    const mutation = runAbortableAdminModelMutation(
      ACCOUNT_ID,
      (signal) =>
        new Promise<never>((_resolve, reject) => {
          capturedSignal = signal;
          signal.addEventListener(
            "abort",
            () => {
              reject(
                Object.assign(new Error("Aborted"), { name: "AbortError" }),
              );
            },
            { once: true },
          );
        }),
    );

    await Promise.resolve();
    abortAdminModelSettingsAccount(ACCOUNT_ID);

    await expect(mutation).rejects.toMatchObject({ name: "AbortError" });
    expect(capturedSignal?.aborted).toBe(true);
  });

  test("keeps all model mutations on the explicit admin settings routes", async () => {
    const createInput = {
      logical_name: model.logical_name,
      display_name: model.display_name,
      description: model.description,
      provider_adapter: model.provider_adapter,
      provider_model: model.provider_model,
      settings: model.settings,
      supports_thinking: model.supports_thinking,
      supports_reasoning_effort: model.supports_reasoning_effort,
      supports_vision: model.supports_vision,
      status: model.status,
      credential_id: model.credential_id,
      credential_version_id: model.credential_version_id,
      credential_env_key: model.credential_env_key,
      sort_order: model.sort_order,
    };
    const mutationResponse = {
      item: model,
      catalog_revision: 8,
      request_id: "req-model-write",
    };
    mockedFetch
      .mockResolvedValueOnce(jsonResponse(201, mutationResponse))
      .mockResolvedValueOnce(jsonResponse(200, mutationResponse))
      .mockResolvedValueOnce(jsonResponse(200, mutationResponse))
      .mockResolvedValueOnce(jsonResponse(200, mutationResponse));

    await createAdminModel(ACCOUNT_ID, createInput);
    await replaceAdminModel(ACCOUNT_ID, MODEL_ID, {
      display_name: createInput.display_name,
      description: createInput.description,
      provider_adapter: createInput.provider_adapter,
      provider_model: createInput.provider_model,
      settings: createInput.settings,
      supports_thinking: createInput.supports_thinking,
      supports_reasoning_effort: createInput.supports_reasoning_effort,
      supports_vision: createInput.supports_vision,
      credential_id: createInput.credential_id,
      credential_version_id: createInput.credential_version_id,
      credential_env_key: createInput.credential_env_key,
      sort_order: createInput.sort_order,
      expected_revision: model.revision,
    });
    await setAdminModelStatus(ACCOUNT_ID, MODEL_ID, {
      status: "suspended",
      expected_revision: model.revision,
    });
    await setAdminModelDefault(ACCOUNT_ID, MODEL_ID, {
      expected_catalog_revision: catalog.catalog_revision,
    });

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      "/backend/api/admin/settings/models",
      `/backend/api/admin/settings/models/${MODEL_ID}`,
      `/backend/api/admin/settings/models/${MODEL_ID}/status`,
      `/backend/api/admin/settings/models/${MODEL_ID}/default`,
    ]);
    expect(
      mockedFetch.mock.calls.map(([, init]) => ({
        method: init?.method,
        body:
          typeof init?.body === "string"
            ? (JSON.parse(init.body) as unknown)
            : init?.body,
      })),
    ).toEqual([
      { method: "POST", body: createInput },
      {
        method: "PUT",
        body: {
          display_name: createInput.display_name,
          description: createInput.description,
          provider_adapter: createInput.provider_adapter,
          provider_model: createInput.provider_model,
          settings: createInput.settings,
          supports_thinking: createInput.supports_thinking,
          supports_reasoning_effort: createInput.supports_reasoning_effort,
          supports_vision: createInput.supports_vision,
          credential_id: createInput.credential_id,
          credential_version_id: createInput.credential_version_id,
          credential_env_key: createInput.credential_env_key,
          sort_order: createInput.sort_order,
          expected_revision: model.revision,
        },
      },
      {
        method: "POST",
        body: {
          status: "suspended",
          expected_revision: model.revision,
        },
      },
      {
        method: "POST",
        body: {
          expected_catalog_revision: catalog.catalog_revision,
        },
      },
    ]);
    for (const [, init] of mockedFetch.mock.calls) {
      expect(init?.headers).toEqual({ "Content-Type": "application/json" });
    }
  });

  test("fails closed when a successful response does not match the strict schema", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        ...catalog,
        items: [{ ...model, storage_locator: "vault://not-for-ui" }],
      }),
    );

    await expect(fetchAdminModelCatalog(ACCOUNT_ID)).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });
});
