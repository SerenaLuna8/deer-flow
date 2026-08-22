import { describe, expect, test } from "@rstest/core";

import {
  adminModelConnectionTestErrorState,
  adminModelConnectionTestResultMessage,
  adminModelSettingsCopy,
  consumeAdminModelEditorSubmission,
  isAdminModelEditorSaveDisabled,
  selectAdminModelCatalogItems,
} from "@/components/admin/settings/admin-model-settings-page";
import {
  adminModelCatalogSchema,
  createAdminModelInputSchema,
  testAdminModelConnectionInputSchema,
  type AdminModelCatalog,
} from "@/core/admin-settings/models";

const catalog: AdminModelCatalog = {
  items: [
    {
      id: "11111111-1111-4111-8111-111111111111",
      display_name: "DeepSeek Flash",
      provider_adapter: "deepseek",
      provider_model: "deepseek-v4-flash",
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
      setting_fields: [],
    },
  ],
  catalog_revision: 3,
  request_id: "request-1",
};

describe("admin model settings domain-owned API Key", () => {
  test("provides a complete English governance surface without Chinese fallbacks", () => {
    const copy = adminModelSettingsCopy("en-US");

    expect(copy.pageTitle).toBe("Model settings");
    expect(copy.addModel).toBe("Add model");
    expect(copy.clearDialogTitle).toBe("Clear API Key?");
    expect(copy.testConnection).toBe("Test connection");
    expect(JSON.stringify(copy)).not.toMatch(/[\u3400-\u9fff]/u);
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

  test("save accepts write-only key while connection test requires a fresh key", () => {
    const common = {
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
        settings: common.settings,
        supports_vision: false,
        api_key: "",
      }).success,
    ).toBe(false);
  });

  test("local settings validation clears the write-only API Key first", () => {
    const form = new FormData();
    form.set("settings", "{invalid-json");
    let cleared = false;

    expect(() =>
      consumeAdminModelEditorSubmission(
        form,
        "deepseek",
        "temporary-key",
        () => {
          cleared = true;
        },
      ),
    ).toThrow("Provider 设置必须是 JSON 对象。");
    expect(cleared).toBe(true);

    expect(() =>
      consumeAdminModelEditorSubmission(
        form,
        "deepseek",
        "temporary-key",
        () => undefined,
        "en-US",
      ),
    ).toThrow("Provider settings must be a JSON object.");
  });

  test("requires a fresh Key to save a newly tested model", () => {
    expect(
      isAdminModelEditorSaveDisabled({
        apiKey: "",
        creating: true,
        pending: false,
        providerRequiresApiKey: true,
        testPending: false,
      }),
    ).toBe(true);
    expect(
      isAdminModelEditorSaveDisabled({
        apiKey: "replacement-key",
        creating: true,
        pending: false,
        providerRequiresApiKey: true,
        testPending: false,
      }),
    ).toBe(false);
    expect(
      isAdminModelEditorSaveDisabled({
        apiKey: "",
        creating: false,
        pending: false,
        providerRequiresApiKey: true,
        testPending: false,
      }),
    ).toBe(false);
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
