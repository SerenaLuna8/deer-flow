import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AdminModelCatalogStateView,
  type AdminModelCredentialOption,
} from "@/components/admin/settings/admin-model-settings-page";
import type { AdminModelCatalog } from "@/core/admin-settings/models";
import { I18nProvider } from "@/core/i18n/context";

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
        logical_name: "test-model",
        display_name: "测试模型",
        description: "",
        provider_adapter: "openai",
        provider_model: "gpt-test",
        settings: { base_url: "https://api.example.com/v1" },
        supports_thinking: true,
        supports_reasoning_effort: true,
        supports_vision: true,
        credential_id: credentials[0]!.id,
        credential_version_id: "22222222-2222-4222-8222-222222222222",
        credential_env_key: "OPENAI_API_KEY",
        sort_order: 0,
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
  test("does not expose credential bindings in the catalog list", () => {
    const markup = renderCatalog();

    expect(markup).not.toContain(
      '<th class="px-3 py-2.5 text-xs font-medium">凭证</th>',
    );
    expect(markup).not.toContain("OpenAI primary");
  });
});
