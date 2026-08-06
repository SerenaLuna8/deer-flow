import { describe, expect, rs, test } from "@rstest/core";
import type { ComponentProps, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogContent: ({
    children,
    closeLabel: _closeLabel,
    ...props
  }: ComponentProps<"div"> & { closeLabel?: string }) => (
    <div {...props}>{children}</div>
  ),
  DialogDescription: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  DialogFooter: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  DialogHeader: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  DialogTitle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

import {
  AdminModelCatalogStateView,
  ModelEditorDialog,
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

function renderEditor(): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <ModelEditorDialog
        open
        model={null}
        credentials={credentials}
        credentialStatus="ready"
        credentialsRefreshing={false}
        pending={false}
        mutationError={null}
        onOpenChange={() => undefined}
        onRetryCredentials={() => undefined}
        onSubmit={async () => true}
        onTestConnection={async () => ({
          status: "succeeded",
          request_id: "test-request",
        })}
      />
    </I18nProvider>,
  );
}

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

describe("ModelEditorDialog", () => {
  test("keeps the Base URL in basic configuration before credential and runtime sections", () => {
    const markup = renderEditor();
    const basicStart = markup.indexOf(
      'data-testid="admin-model-editor-basic-information"',
    );
    const credentialStart = markup.indexOf(
      'data-testid="admin-model-editor-credential-binding"',
    );
    const capabilitiesStart = markup.indexOf(
      'data-testid="admin-model-editor-capabilities-and-runtime"',
    );
    const baseUrl = markup.indexOf('id="base_url"');

    expect(basicStart).toBeGreaterThan(-1);
    expect(baseUrl).toBeGreaterThan(basicStart);
    expect(baseUrl).toBeLessThan(credentialStart);
    expect(credentialStart).toBeLessThan(capabilitiesStart);
  });

  test("keeps advanced JSON collapsed until the user chooses to open it", () => {
    const markup = renderEditor();
    const advancedStart = markup.indexOf(
      'data-testid="admin-model-editor-advanced-settings"',
    );
    const advancedTagStart = markup.lastIndexOf("<details", advancedStart);
    const advancedTag = markup.slice(
      advancedTagStart,
      markup.indexOf(">", advancedStart) + 1,
    );

    expect(advancedTag).toContain("<details");
    expect(advancedTag).not.toContain("open");
  });

  test("offers a connection test after the Credential binding", () => {
    const markup = renderEditor();
    const credentialStart = markup.indexOf(
      'data-testid="admin-model-editor-credential-binding"',
    );
    const testAction = markup.indexOf(
      'data-testid="admin-model-test-connection"',
    );
    const capabilityStart = markup.indexOf(
      'data-testid="admin-model-editor-capabilities-and-runtime"',
    );

    expect(testAction).toBeGreaterThan(credentialStart);
    expect(testAction).toBeLessThan(capabilityStart);
    expect(markup).toContain("测试连通性");
  });

  test("separates model status from model capabilities in the catalog table", () => {
    const markup = renderCatalog();
    const statusHeader = markup.indexOf(
      '<th class="px-3 py-2.5 text-xs font-medium">状态</th>',
    );
    const capabilitiesHeader = markup.indexOf(
      '<th class="px-3 py-2.5 text-xs font-medium">模型能力</th>',
    );

    expect(statusHeader).toBeGreaterThan(-1);
    expect(statusHeader).toBeLessThan(capabilitiesHeader);
    expect(markup).toContain("已启用");
    expect(markup).toContain("深度思考");
    expect(markup).toContain("推理强度");
    expect(markup).toContain("视觉输入");
    expect(markup).toContain('class="flex flex-nowrap gap-1.5"');
    expect(markup).toContain('<col class="w-[12%]"/>');
  });

  test("does not expose credential bindings in the catalog list", () => {
    const markup = renderCatalog();

    expect(markup).not.toContain(
      '<th class="px-3 py-2.5 text-xs font-medium">凭证</th>',
    );
    expect(markup).not.toContain("OpenAI primary");
  });
});
