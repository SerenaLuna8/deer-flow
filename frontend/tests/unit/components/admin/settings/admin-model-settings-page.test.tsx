import { describe, expect, test } from "@rstest/core";

import {
  adminModelConnectionTestErrorState,
  adminModelConnectionTestResultMessage,
  adminModelProviderSettingLabel,
  adminModelSettingsCopy,
  consumeAdminModelEditorSubmission,
  isAdminModelEditorSaveDisabled,
  selectAdminModelCatalogItems,
} from "@/components/admin/settings/admin-model-settings-page";
import {
  adminModelCatalogSchema,
  adminModelSettingsSchemaForProvider,
  createAdminModelProviderSettingsDraft,
  createAdminModelInputSchema,
  testAdminModelConnectionInputSchema,
  updateAdminModelProviderSettingDraftValue,
  type AdminModelCatalog,
} from "@/core/admin-settings/models";

const catalog: AdminModelCatalog = {
  items: [
    {
      id: "11111111-1111-4111-8111-111111111111",
      display_name: "DeepSeek Flash",
      provider_adapter: "deepseek",
      provider_model: "deepseek-v4-flash",
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

describe("admin model settings domain-owned API Key", () => {
  test("provides a complete English governance surface without Chinese fallbacks", () => {
    const copy = adminModelSettingsCopy("en-US");

    expect(copy.pageTitle).toBe("Model management");
    expect(copy.addModel).toBe("Add model");
    expect(copy.clearDialogTitle).toBe("Clear API Key?");
    expect(copy.testConnection).toBe("Test connection");
    expect(JSON.stringify(copy)).toContain("Maximum input tokens");
    expect(JSON.stringify(copy)).toContain("not the maximum output token");
    expect(JSON.stringify(copy)).not.toMatch(/[\u3400-\u9fff]/u);
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

  test("catalog responses expose status only and reject plaintext", () => {
    expect(adminModelCatalogSchema.safeParse(catalog).success).toBe(true);
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

  test("save accepts write-only key while connection test requires a fresh key", () => {
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
        api_key: "temporary-key",
      }).success,
    ).toBe(true);
    expect(
      createAdminModelInputSchema.safeParse({
        ...common,
        status: "active",
        api_key: null,
      }).success,
    ).toBe(true);
    expect(
      testAdminModelConnectionInputSchema.safeParse({
        provider_adapter: common.provider_adapter,
        provider_model: common.provider_model,
        max_input_tokens: common.max_input_tokens,
        settings: common.settings,
        supports_vision: false,
        api_key: "",
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
        api_key: "temporary-key",
      }).success,
    ).toBe(false);
    for (const maxInputTokens of [1, 128_000, 2_000_000]) {
      expect(
        createAdminModelInputSchema.safeParse({
          ...commonWithoutCapacity,
          max_input_tokens: maxInputTokens,
          status: "active",
          api_key: "temporary-key",
        }).success,
      ).toBe(true);
      expect(
        testAdminModelConnectionInputSchema.safeParse({
          provider_adapter: commonWithoutCapacity.provider_adapter,
          provider_model: commonWithoutCapacity.provider_model,
          settings: commonWithoutCapacity.settings,
          max_input_tokens: maxInputTokens,
          supports_vision: false,
          api_key: "temporary-key",
        }).success,
      ).toBe(true);
    }
    for (const invalidCapacity of [0, -1, 1.5, 2_000_001]) {
      expect(
        createAdminModelInputSchema.safeParse({
          ...commonWithoutCapacity,
          max_input_tokens: invalidCapacity,
          status: "active",
          api_key: "temporary-key",
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
      "temporary-key",
      () => undefined,
      "en-US",
    );
    expect(
      "max_input_tokens" in submission.common
        ? submission.common.max_input_tokens
        : null,
    ).toBe(128_000);

    for (const invalidCapacity of ["", "0", "1.5", "2000001", "128k"]) {
      form.set("max_input_tokens", invalidCapacity);
      expect(() =>
        consumeAdminModelEditorSubmission(
          form,
          descriptor,
          settingsDraft,
          "temporary-key",
          () => undefined,
          "en-US",
        ),
      ).toThrow(
        "Maximum input tokens must be a whole number from 1 to 2,000,000.",
      );
    }
  });

  test("typed settings validation clears the write-only API Key first", () => {
    const form = new FormData();
    form.set("max_input_tokens", "128000");
    const descriptor = catalog.provider_adapters[0];
    const invalidDraft = updateAdminModelProviderSettingDraftValue(
      createAdminModelProviderSettingsDraft(descriptor, {}),
      "max_tokens",
      "0",
    );
    let cleared = false;

    expect(() =>
      consumeAdminModelEditorSubmission(
        form,
        descriptor,
        invalidDraft,
        "temporary-key",
        () => {
          cleared = true;
        },
      ),
    ).toThrow("Provider 设置无效。");
    expect(cleared).toBe(true);

    expect(() =>
      consumeAdminModelEditorSubmission(
        form,
        descriptor,
        invalidDraft,
        "temporary-key",
        () => undefined,
        "en-US",
      ),
    ).toThrow("Provider settings are invalid.");
  });

  test("requires a fresh Key to save a newly tested model", () => {
    expect(
      isAdminModelEditorSaveDisabled({
        apiKey: "",
        creating: true,
        pending: false,
        providerRequiresApiKey: true,
        providerSettingsIncompatible: false,
        testPending: false,
      }),
    ).toBe(true);
    expect(
      isAdminModelEditorSaveDisabled({
        apiKey: "replacement-key",
        creating: true,
        pending: false,
        providerRequiresApiKey: true,
        providerSettingsIncompatible: false,
        testPending: false,
      }),
    ).toBe(false);
    expect(
      isAdminModelEditorSaveDisabled({
        apiKey: "",
        creating: false,
        pending: false,
        providerRequiresApiKey: true,
        providerSettingsIncompatible: false,
        testPending: false,
      }),
    ).toBe(false);
    expect(
      isAdminModelEditorSaveDisabled({
        apiKey: "temporary-key",
        creating: true,
        pending: false,
        providerRequiresApiKey: true,
        providerSettingsIncompatible: true,
        testPending: false,
      }),
    ).toBe(true);
    expect(
      adminModelConnectionTestResultMessage("succeeded", "zh-CN"),
    ).toContain("保存前必须重新输入 API Key");
    expect(adminModelConnectionTestResultMessage("failed", "zh-CN")).toContain(
      "测试用 Key 已从表单清除",
    );
    expect(adminModelConnectionTestResultMessage("succeeded", "en-US")).toBe(
      "Connection test succeeded. The test Key was cleared from the form; re-enter the API Key before saving.",
    );
    expect(
      adminModelConnectionTestResultMessage("succeeded", "en-US", "edit"),
    ).toBe(
      "Connection test succeeded. The test Key was cleared and not saved. Leave the field blank to preserve the saved Key, or re-enter a Key to replace it.",
    );
  });

  test("keeps the re-entry guidance when the connection request throws", () => {
    expect(
      adminModelConnectionTestErrorState(
        new Error("provider unavailable"),
        "en-US",
        "create",
      ),
    ).toEqual({
      error: "provider unavailable",
      result:
        "Connection test failed. The test Key was cleared from the form; re-enter the API Key to retry or save.",
    });
    expect(adminModelConnectionTestErrorState(null, "zh-CN", "edit")).toEqual({
      error: "连接测试失败。",
      result:
        "连接测试失败。测试用 Key 已清空且不会保存；留空可保留原 Key，重新输入可再次测试或替换。",
    });
  });
});
