import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ADMIN_MODEL_PROVIDER_ADAPTERS,
  AdminModelCatalogStateView,
  parseAdminModelConnectionTestForm,
  selectAdminModelCatalogItems,
  type AdminModelCredentialOption,
} from "@/components/admin/settings/admin-model-settings-page";
import {
  testAdminModelConnectionInputSchema,
  type AdminModelCatalog,
} from "@/core/admin-settings/models";
import { I18nProvider } from "@/core/i18n/context";
import { enUS, zhCN } from "@/core/i18n/locales";

const credentials: AdminModelCredentialOption[] = [
  {
    id: "11111111-1111-4111-8111-111111111111",
    display_name: "OpenAI primary",
    credential_type: "model_api_key",
    current_version_id: "22222222-2222-4222-8222-222222222222",
    version: 1,
    status: "active",
  },
];

function renderCatalog(): string {
  const catalog: AdminModelCatalog = {
    catalog_revision: 1,
    request_id: "test-request",
    items: [
      {
        id: "33333333-3333-4333-8333-333333333333",
        display_name: "测试模型",
        provider_adapter: "openai",
        provider_model: "gpt-test",
        settings: { base_url: "https://api.example.com/v1" },
        supports_thinking: true,
        supports_reasoning_effort: true,
        supports_vision: true,
        credential_id: credentials[0]!.id,
        credential_version_id: "22222222-2222-4222-8222-222222222222",
        credential_env_key: "OPENAI_API_KEY",
        status: "active",
        is_default: true,
        revision: 1,
        version_number: 1,
        updated_at: "2026-08-06T00:00:00+00:00",
      },
    ],
  };
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <AdminModelCatalogStateView
        state={{ status: "ready", data: catalog }}
        pendingAction={null}
        onCreate={() => undefined}
        onEdit={() => undefined}
        onToggleStatus={() => undefined}
        onSetDefault={() => undefined}
        onRetry={() => undefined}
      />
    </I18nProvider>,
  );
}

describe("admin model catalog", () => {
  test("does not offer retired provider adapters", () => {
    const adapters = ADMIN_MODEL_PROVIDER_ADAPTERS.map(
      (adapter) => adapter.value,
    );

    expect(adapters).not.toContain("patched_mimo");
    expect(adapters).not.toContain("patched_minimax");
    expect(adapters).not.toContain("patched_stepfun");
    expect(adapters).not.toContain("mindie");
    expect(adapters).not.toContain("claude_code");
    expect(adapters).not.toContain("codex_cli");
  });

  test("does not expose credential bindings in the catalog list", () => {
    const markup = renderCatalog();

    expect(markup).not.toContain(
      '<th class="px-3 py-2.5 text-xs font-medium">凭证</th>',
    );
    expect(markup).not.toContain("OpenAI primary");
  });

  test("shows only the display name and the real model ID", () => {
    const markup = renderCatalog();

    expect(markup).toContain("测试模型");
    expect(markup).toContain("模型 ID");
    expect(markup).toContain("gpt-test");
    expect(markup).not.toContain("test-model");
    expect(markup).not.toContain("提供方模型");
  });

  test("does not search the internal logical name", () => {
    const catalog = {
      catalog_revision: 1,
      request_id: "test-request",
      items: [
        {
          id: "33333333-3333-4333-8333-333333333333",
          display_name: "Visible model",
          provider_adapter: "openai" as const,
          provider_model: "gpt-visible",
          settings: {},
          supports_thinking: false,
          supports_reasoning_effort: false,
          supports_vision: false,
          credential_id: credentials[0]!.id,
          credential_version_id: credentials[0]!.current_version_id,
          credential_env_key: "OPENAI_API_KEY",
          status: "active" as const,
          is_default: true,
          revision: 1,
          version_number: 1,
          updated_at: "2026-08-06T00:00:00+00:00",
        },
      ],
    } satisfies AdminModelCatalog;

    expect(
      selectAdminModelCatalogItems(catalog.items, "internal-only-name", "all"),
    ).toEqual([]);
    expect(
      selectAdminModelCatalogItems(catalog.items, "Visible model", "all"),
    ).toHaveLength(1);
  });

  test("removes the three explanatory copy blocks from both locales", () => {
    for (const translations of [zhCN, enUS]) {
      const editor = translations.adminModelSettings.editor;
      expect("credentialBindingDescription" in editor).toBe(false);
      expect("capabilitiesDescription" in editor).toBe(false);
      expect("commonProviderSettingsDescription" in editor).toBe(false);
    }
  });

  test("keeps the vision probe intent in connection-test requests", () => {
    const formData = new FormData();
    formData.set("display_name", "Test model");
    formData.set("provider_adapter", "vision_bridge_fake");
    formData.set("provider_model", "vision-test");
    formData.set("advanced_settings", "{}");
    formData.set("status", "active");
    formData.set("supports_vision", "on");

    const input = parseAdminModelConnectionTestForm(formData);

    expect(testAdminModelConnectionInputSchema.parse(input)).toMatchObject({
      supports_vision: true,
    });
  });
});
