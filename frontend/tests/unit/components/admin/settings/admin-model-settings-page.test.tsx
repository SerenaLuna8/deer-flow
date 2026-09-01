import { describe, expect, test } from "@rstest/core";

import {
  adminModelConnectionTestResultMessage,
  adminModelProviderSettingLabel,
  adminModelSettingsCopy,
  consumeAdminModelEditorSubmission,
  isAdminModelEditorSaveDisabled,
  selectAdminModelCatalogItems,
  selectAdminProviderModelItems,
} from "@/components/admin/settings/admin-model-settings-page";
import type { AdminProviderModelItem } from "@/core/admin-settings/model-registry/types";
import {
  adminModelCatalogSchema,
  adminModelSettingsSchemaForProvider,
  createAdminModelProviderSettingsDraft,
  createAdminModelInputSchema,
  testAdminModelConnectionInputSchema,
  updateAdminModelProviderSettingDraftValue,
  type AdminModelCatalog,
} from "@/core/admin-settings/models";

const PROVIDER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

const catalog: AdminModelCatalog = {
  items: [
    {
      id: "11111111-1111-4111-8111-111111111111",
      display_name: "DeepSeek Flash",
      provider_adapter: "deepseek",
      provider_model: "deepseek-v4-flash",
      provider_id: PROVIDER_ID,
      provider_name: "DeepSeek",
      max_input_tokens: 128_000,
      settings: {},
      supports_thinking: false,
      supports_reasoning_effort: false,
      supports_vision: false,
      status: "active",
      is_default: true,
      revision: 2,
      api_key_configured: true,
      secret_readiness: "ready",
      secret_revision: 1,
      updated_at: "2026-08-22T00:00:00Z",
    },
  ],
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
          default_mode: "platform",
          default_value: 51_200,
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
  catalog_revision: 3,
  request_id: "request-1",
};

describe("admin model settings with provider-owned credentials", () => {
  test("provides a complete English governance surface without Chinese fallbacks", () => {
    const copy = adminModelSettingsCopy("en-US");

    expect(copy.pageTitle).toBe("Model management");
    expect(copy.addModel).toBe("Add text model");
    expect(copy.testConnection).toBe("Test connection");
    expect(copy.providerBinding).toBe("Provider");
    expect(JSON.stringify(copy)).toContain("Maximum input tokens");
    expect(JSON.stringify(copy)).toContain("not the maximum output token");
    expect(JSON.stringify(copy)).not.toMatch(/[\u3400-\u9fff]/u);
    // The model editor carries no credential vocabulary of its own anymore.
    expect(JSON.stringify(copy)).not.toContain("Clear API Key");
    expect(
      adminModelProviderSettingLabel("max_tokens", "Max tokens", "en-US"),
    ).toBe("Maximum output tokens");
    expect(
      adminModelProviderSettingLabel("max_tokens", "Max tokens", "zh-CN"),
    ).toBe("最大输出 Token");
    expect(
      adminModelProviderSettingLabel(
        "vendor_quality",
        "Vendor quality",
        "zh-CN",
      ),
    ).toBe("Vendor quality");
  });

  test("catalog filtering uses stable model configs", () => {
    expect(
      selectAdminModelCatalogItems(catalog.items, "flash", "active"),
    ).toEqual(catalog.items);
  });

  test("unified model filtering maps suspended to disabled retrieval models", () => {
    const retrievalModels: AdminProviderModelItem[] = [
      {
        id: "30000000-0000-4000-8000-000000000001",
        provider_id: PROVIDER_ID,
        model_type: "embedding",
        model_name: "Qwen/Qwen3-Embedding-8B",
        embedding_dimension: 4096,
        max_batch: 64,
        status: "active",
        in_use: false,
        created_at: "2026-08-30T00:00:00Z",
        updated_at: "2026-08-30T00:00:00Z",
      },
      {
        id: "30000000-0000-4000-8000-000000000002",
        provider_id: PROVIDER_ID,
        model_type: "rerank",
        model_name: "Qwen/Qwen3-VL-Reranker-8B",
        embedding_dimension: null,
        max_batch: 32,
        status: "disabled",
        in_use: false,
        created_at: "2026-08-30T00:00:00Z",
        updated_at: "2026-08-30T00:00:00Z",
      },
    ];

    expect(
      selectAdminProviderModelItems(retrievalModels, "reranker", "suspended"),
    ).toEqual([retrievalModels[1]]);
    expect(
      selectAdminProviderModelItems(retrievalModels, "embedding", "active"),
    ).toEqual([retrievalModels[0]]);
  });

  test("catalog items carry the provider binding and reject plaintext keys", () => {
    expect(adminModelCatalogSchema.safeParse(catalog).success).toBe(true);
    expect(catalog.items[0]?.provider_id).toBe(PROVIDER_ID);
    expect(catalog.items[0]?.provider_name).toBe("DeepSeek");
    expect(
      adminModelCatalogSchema.safeParse({
        ...catalog,
        items: [{ ...catalog.items[0], api_key: "must-not-leak" }],
      }).success,
    ).toBe(false);
  });

  test("DeepSeek exposes only its supported reasoning-effort choices", () => {
    const descriptor = adminModelCatalogSchema
      .parse(catalog)
      .provider_adapters.find((item) => item.id === "deepseek");
    if (!descriptor) throw new Error("DeepSeek descriptor is missing");
    const reasoningEffort = descriptor.setting_fields.find(
      (field) => field.name === "reasoning_effort",
    );

    expect(reasoningEffort).toMatchObject({
      input_type: "enum",
      default_mode: "provider",
      default_value: null,
      options: ["low", "high", "max"],
    });
    const settingsSchema = adminModelSettingsSchemaForProvider(descriptor);
    expect(settingsSchema.safeParse({}).success).toBe(true);
    for (const value of ["low", "high", "max"]) {
      expect(
        settingsSchema.safeParse({ reasoning_effort: value }).success,
      ).toBe(true);
    }
    for (const value of ["none", "minimal", "medium"]) {
      expect(
        settingsSchema.safeParse({ reasoning_effort: value }).success,
      ).toBe(false);
    }
  });

  test("model writes bind a provider and never carry a model-level key", () => {
    const common = {
      display_name: "DeepSeek Pro",
      provider_adapter: "deepseek",
      provider_model: "deepseek-v4-pro",
      max_input_tokens: 128_000,
      settings: {},
      supports_thinking: true,
      supports_reasoning_effort: false,
      supports_vision: false,
    };
    expect(
      createAdminModelInputSchema.safeParse({
        ...common,
        status: "active",
        provider_id: PROVIDER_ID,
      }).success,
    ).toBe(true);
    expect(
      createAdminModelInputSchema.safeParse({
        ...common,
        status: "active",
        provider_id: PROVIDER_ID,
        api_key: "must-not-exist",
      }).success,
    ).toBe(false);
    expect(
      createAdminModelInputSchema.safeParse({
        ...common,
        status: "active",
      }).success,
    ).toBe(false);
    // The stored-key connection test addresses the provider, never a raw key.
    expect(
      testAdminModelConnectionInputSchema.safeParse({
        provider_id: PROVIDER_ID,
        provider_adapter: common.provider_adapter,
        provider_model: common.provider_model,
        max_input_tokens: common.max_input_tokens,
        settings: common.settings,
        supports_vision: false,
      }).success,
    ).toBe(true);
    expect(
      testAdminModelConnectionInputSchema.safeParse({
        provider_id: PROVIDER_ID,
        provider_adapter: common.provider_adapter,
        provider_model: common.provider_model,
        max_input_tokens: common.max_input_tokens,
        settings: common.settings,
        supports_vision: false,
        api_key: "must-not-exist",
      }).success,
    ).toBe(false);
  });

  test("requires and parses a bounded maximum input context for every model operation", () => {
    const commonWithoutCapacity = {
      display_name: "DeepSeek Pro",
      provider_adapter: "deepseek",
      provider_model: "deepseek-v4-pro",
      settings: {},
      supports_thinking: true,
      supports_reasoning_effort: false,
      supports_vision: false,
    };

    expect(
      createAdminModelInputSchema.safeParse({
        ...commonWithoutCapacity,
        status: "active",
        provider_id: PROVIDER_ID,
      }).success,
    ).toBe(false);
    for (const maxInputTokens of [1, 128_000, 2_000_000]) {
      expect(
        createAdminModelInputSchema.safeParse({
          ...commonWithoutCapacity,
          max_input_tokens: maxInputTokens,
          status: "active",
          provider_id: PROVIDER_ID,
        }).success,
      ).toBe(true);
      expect(
        testAdminModelConnectionInputSchema.safeParse({
          provider_id: PROVIDER_ID,
          provider_adapter: commonWithoutCapacity.provider_adapter,
          provider_model: commonWithoutCapacity.provider_model,
          settings: commonWithoutCapacity.settings,
          max_input_tokens: maxInputTokens,
          supports_vision: false,
        }).success,
      ).toBe(true);
    }
    for (const invalidCapacity of [0, -1, 1.5, 2_000_001]) {
      expect(
        createAdminModelInputSchema.safeParse({
          ...commonWithoutCapacity,
          max_input_tokens: invalidCapacity,
          status: "active",
          provider_id: PROVIDER_ID,
        }).success,
      ).toBe(false);
    }

    const form = new FormData();
    form.set("display_name", "DeepSeek Pro");
    form.set("provider_model", "deepseek-v4-pro");
    form.set("max_input_tokens", "128000");
    const descriptor = catalog.provider_adapters[0];
    const settingsDraft = createAdminModelProviderSettingsDraft(descriptor, {});
    const submission = consumeAdminModelEditorSubmission(
      form,
      descriptor,
      settingsDraft,
      "en-US",
    );
    expect(submission.max_input_tokens).toBe(128_000);
    expect("api_key" in submission).toBe(false);
    expect("provider_id" in submission).toBe(false);

    for (const invalidCapacity of ["", "0", "1.5", "2000001", "128k"]) {
      form.set("max_input_tokens", invalidCapacity);
      expect(() =>
        consumeAdminModelEditorSubmission(
          form,
          descriptor,
          settingsDraft,
          "en-US",
        ),
      ).toThrow(
        "Maximum input tokens must be a whole number from 1 to 2,000,000.",
      );
    }
  });

  test("typed settings validation reports locale-specific errors", () => {
    const form = new FormData();
    form.set("max_input_tokens", "128000");
    const descriptor = catalog.provider_adapters[0];
    const invalidDraft = updateAdminModelProviderSettingDraftValue(
      createAdminModelProviderSettingsDraft(descriptor, {}),
      "max_tokens",
      "0",
    );

    expect(() =>
      consumeAdminModelEditorSubmission(form, descriptor, invalidDraft),
    ).toThrow("Provider 设置无效。");
    expect(() =>
      consumeAdminModelEditorSubmission(
        form,
        descriptor,
        invalidDraft,
        "en-US",
      ),
    ).toThrow("Provider settings are invalid.");
  });

  test("saving requires a resolvable provider binding, not a fresh key", () => {
    expect(
      isAdminModelEditorSaveDisabled({
        pending: false,
        providerMissing: true,
        providerSettingsIncompatible: false,
        testPending: false,
      }),
    ).toBe(true);
    expect(
      isAdminModelEditorSaveDisabled({
        pending: false,
        providerMissing: false,
        providerSettingsIncompatible: false,
        testPending: false,
      }),
    ).toBe(false);
    expect(
      isAdminModelEditorSaveDisabled({
        pending: false,
        providerMissing: false,
        providerSettingsIncompatible: true,
        testPending: false,
      }),
    ).toBe(true);
    expect(
      isAdminModelEditorSaveDisabled({
        pending: false,
        providerMissing: false,
        providerSettingsIncompatible: false,
        testPending: true,
      }),
    ).toBe(true);
  });

  test("connection test messages describe the provider's stored key", () => {
    expect(adminModelConnectionTestResultMessage("succeeded", "zh-CN")).toBe(
      "连接测试成功（使用供应商已保存的 Key）。",
    );
    expect(adminModelConnectionTestResultMessage("failed", "zh-CN")).toContain(
      "请检查供应商的服务地址与 Key",
    );
    expect(adminModelConnectionTestResultMessage("succeeded", "en-US")).toBe(
      "Connection test succeeded using the provider's saved Key.",
    );
    expect(adminModelConnectionTestResultMessage("failed", "en-US")).toContain(
      "Check the provider's endpoint and Key",
    );
  });
});
