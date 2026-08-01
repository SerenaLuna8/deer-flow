import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  usePathname: () => "/admin/settings/models",
  useRouter: () => ({ push: rs.fn() }),
}));
rs.mock("@/core/static-mode", () => ({ isStaticWebsiteOnly: () => false }));

import {
  AdminModelCatalogStateView,
  AdminModelSettingsPage,
  adminModelEditorFieldErrorAttributes,
  buildAdminModelCredentialEditorState,
  canCloseAdminModelEditor,
  canSubmitAdminModelEditor,
  getAdminModelEditorErrorField,
  parseAdminModelEditorForm,
  selectAdminModelCatalogItems,
  selectAdminModelCredentialOptions,
  type AdminModelCatalogState,
} from "@/components/admin/settings/admin-model-settings-page";
import * as adminModelSettings from "@/core/admin-settings/models";
import { AuthProvider, type User } from "@/core/auth/AuthProvider";
import { I18nProvider } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales/en-US";

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

function renderPage(
  user: User | null,
  locale: "zh-CN" | "en-US" = "zh-CN",
): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <QueryClientProvider client={new QueryClient()}>
        <AuthProvider initialUser={user}>
          <AdminModelSettingsPage />
        </AuthProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

function renderState(
  state: AdminModelCatalogState,
  locale: "zh-CN" | "en-US" = "zh-CN",
  credentialName = "OpenAI 生产凭据",
  retrying = false,
): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <AdminModelCatalogStateView
        state={state}
        credentials={[
          {
            id: CREDENTIAL_ID,
            display_name: credentialName,
            credential_type: "model_api_key",
            current_version_id: CREDENTIAL_VERSION_ID,
            version: 4,
            status: "active",
          },
        ]}
        pendingAction={null}
        onCreate={() => undefined}
        onEdit={() => undefined}
        onToggleStatus={() => undefined}
        onSetDefault={() => undefined}
        onRetry={() => undefined}
        retrying={retrying}
      />
    </I18nProvider>,
  );
}

function formData(values: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(values)) data.set(key, value);
  return data;
}

describe("admin model settings UI", () => {
  test("does not mount the catalog query before system_admin identity is known", () => {
    const catalogHook = rs.spyOn(adminModelSettings, "useAdminModelCatalog");
    expect(() => renderPage(null)).not.toThrow();
    expect(() =>
      renderPage({
        id: "ordinary-account",
        email: "member@example.com",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      }),
    ).not.toThrow();
    expect(catalogHook).not.toHaveBeenCalled();
    catalogHook.mockRestore();
  });

  test("renders Chinese loading, empty, error and safe catalog states", () => {
    const states: AdminModelCatalogState[] = [
      { status: "loading" },
      { status: "error" },
      {
        status: "ready",
        data: { items: [], catalog_revision: 1, request_id: "req-empty" },
      },
      {
        status: "ready",
        data: {
          items: [model],
          catalog_revision: 7,
          request_id: "req-ready",
        },
      },
    ];
    const html = states.map((state) => renderState(state)).join("\n");

    for (const label of [
      "正在加载模型目录",
      "暂时无法读取模型设置",
      "还没有可用模型",
      "创建模型",
      "模型目录概览",
      "已配置模型",
      "活动模型",
      "分析模型 Pro",
      "默认模型",
      "提供方模型",
      "凭据",
      "OpenAI 生产凭据",
      "OPENAI_API_KEY",
      "2026年7月31日",
    ]) {
      expect(html).toContain(label);
    }
    for (const englishSentence of [
      "System settings",
      "Manage available platform models",
      "No models are available yet",
      "Each entry shows only non-sensitive",
      "Provider 模型",
      "Credential",
      "Gateway",
    ]) {
      expect(html).not.toContain(englishSentence);
    }
    expect(html).not.toContain("https://models.example.invalid/v1");
    expect(html).not.toContain("ciphertext");
    expect(html).not.toContain("storage_locator");
    expect(html).toContain('<main id="admin-main"');
  });

  test("renders a compact catalog table with accessible action reasons and safe long text", () => {
    const longName = `分析模型-${"超长名称".repeat(24)}`;
    const suspendedModel = {
      ...model,
      id: "77777777-7777-4777-8777-777777777777",
      logical_name: `analysis-${"long-".repeat(16)}model`,
      display_name: longName,
      status: "suspended" as const,
      is_default: false,
    };
    const html = renderState({
      status: "ready",
      data: {
        items: [model, suspendedModel],
        catalog_revision: 8,
        request_id: "req-compact",
      },
    });

    expect(html).toContain('data-testid="admin-model-catalog-table"');
    expect(html).toContain('data-testid="admin-model-mobile-list"');
    expect(html).toContain(`title="${longName}"`);
    expect(html).toContain("[overflow-wrap:anywhere]");
    expect(html).toContain(
      `aria-describedby="admin-model-${MODEL_ID}-toggle-reason"`,
    );
    expect(
      html.match(new RegExp(`id="admin-model-${MODEL_ID}-toggle-reason"`, "g")),
    ).toHaveLength(1);
    expect(html).toContain(
      'aria-describedby="admin-model-77777777-7777-4777-8777-777777777777-default-reason"',
    );
    expect(html).toContain(`id="admin-model-${MODEL_ID}-mobile-toggle-reason"`);
    expect(html).toContain('data-testid="admin-model-catalog-toolbar"');
    expect(html).toContain('data-testid="admin-model-search"');
    expect(html).toContain('data-testid="admin-model-status-filter"');
    expect(html).toContain('data-testid="admin-model-refresh"');
    expect(html).toContain('data-density="compact"');
    expect(html).toContain('aria-label="编辑：分析模型 Pro"');
    expect(html).toContain('aria-label="暂停：分析模型 Pro"');
    expect(html).toContain('aria-label="当前默认：分析模型 Pro"');
    expect(html).toContain(">操作</span>");
    expect(html).not.toContain("min-w-[70rem]");
  });

  test("filters the real model catalog by localized search text and lifecycle status", () => {
    const suspendedModel = {
      ...model,
      id: "77777777-7777-4777-8777-777777777777",
      logical_name: "vision-backup",
      display_name: "视觉备用模型",
      provider_adapter: "anthropic" as const,
      provider_model: "claude-sonnet-4",
      status: "suspended" as const,
      is_default: false,
    };
    const items = [model, suspendedModel];

    expect(selectAdminModelCatalogItems(items, "视觉", "all")).toEqual([
      suspendedModel,
    ]);
    expect(selectAdminModelCatalogItems(items, "GPT-5.2", "active")).toEqual([
      model,
    ]);
    expect(selectAdminModelCatalogItems(items, "claude", "active")).toEqual([]);
    expect(selectAdminModelCatalogItems(items, "", "suspended")).toEqual([
      suspendedModel,
    ]);
  });

  test("marks catalog retry as busy and prevents duplicate retries", () => {
    const html = renderState({ status: "error" }, "zh-CN", undefined, true);

    expect(html).toContain('data-testid="admin-model-catalog-retry"');
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain("disabled");
    expect(html).toContain("animate-spin");
  });

  test("renders English model settings without Chinese UI copy", () => {
    const englishModel = {
      ...model,
      display_name: "Analysis Pro",
      description: "For deep analysis tasks",
    };
    const html = [
      renderState({ status: "loading" }, "en-US", "OpenAI Production"),
      renderState({ status: "error" }, "en-US", "OpenAI Production"),
      renderState(
        {
          status: "ready",
          data: { items: [], catalog_revision: 1, request_id: "req-empty" },
        },
        "en-US",
        "OpenAI Production",
      ),
      renderState(
        {
          status: "ready",
          data: {
            items: [englishModel],
            catalog_revision: 7,
            request_id: "req-ready",
          },
        },
        "en-US",
        "OpenAI Production",
      ),
    ].join("\n");

    for (const label of [
      "Loading model catalog",
      "Model settings are unavailable",
      "No models are available yet",
      "Create model",
      "Model catalog overview",
      "Configured models",
      "Active models",
      "Default model",
      "OpenAI Production",
      "Updated Jul 31, 2026",
    ]) {
      expect(html).toContain(label);
    }
    for (const chineseCopy of [
      "正在加载模型目录",
      "暂时无法读取模型设置",
      "还没有可用模型",
      "创建模型",
      "模型目录概览",
      "已配置模型",
      "活动模型",
      "默认模型",
      "更新于",
    ]) {
      expect(html).not.toContain(chineseCopy);
    }
    expect(html).not.toMatch(/[\u3400-\u9fff]/);
  });

  test("only exposes system model_api_key metadata to the model page", () => {
    const createdAt = "2026-07-31T08:00:00Z";
    const metadata = {
      scope: "system" as const,
      project_id: null,
      status: "active" as const,
      current_version_id: CREDENTIAL_VERSION_ID,
      version: 4,
      created_by_user_id: "system-admin",
      created_at: createdAt,
      updated_at: createdAt,
    };

    expect(
      selectAdminModelCredentialOptions({
        items: [
          {
            ...metadata,
            id: CREDENTIAL_ID,
            name: "openai-production",
            display_name: "OpenAI 生产凭据",
            credential_type: "model_api_key",
          },
          {
            ...metadata,
            id: "55555555-5555-4555-8555-555555555555",
            name: "database-production",
            display_name: "数据库生产凭据",
            credential_type: "database",
          },
          {
            ...metadata,
            id: "66666666-6666-4666-8666-666666666666",
            scope: "project",
            project_id: "77777777-7777-4777-8777-777777777777",
            name: "project-provider",
            display_name: "项目模型凭据",
            credential_type: "model_api_key",
          },
        ],
        request_id: "req-credentials",
      }),
    ).toEqual([
      {
        id: CREDENTIAL_ID,
        display_name: "OpenAI 生产凭据",
        credential_type: "model_api_key",
        current_version_id: CREDENTIAL_VERSION_ID,
        version: 4,
        status: "active",
      },
    ]);
  });

  test("preserves historical and unavailable exact credential bindings in editor state", () => {
    const newerVersionId = "88888888-8888-4888-8888-888888888888";
    const historical = buildAdminModelCredentialEditorState(model, [
      {
        id: CREDENTIAL_ID,
        display_name: "OpenAI 生产凭据",
        credential_type: "model_api_key",
        current_version_id: newerVersionId,
        version: 5,
        status: "active",
      },
    ]);

    expect(historical.credentialId).toBe(CREDENTIAL_ID);
    expect(historical.credentialVersionId).toBe(CREDENTIAL_VERSION_ID);
    expect(historical.choices).toEqual([
      expect.objectContaining({
        id: CREDENTIAL_ID,
        credential_version_id: CREDENTIAL_VERSION_ID,
        historical: true,
        unavailable: false,
      }),
      expect.objectContaining({
        id: CREDENTIAL_ID,
        credential_version_id: newerVersionId,
        historical: false,
        unavailable: false,
      }),
    ]);

    const unavailable = buildAdminModelCredentialEditorState(model, []);
    expect(unavailable.credentialId).toBe(CREDENTIAL_ID);
    expect(unavailable.credentialVersionId).toBe(CREDENTIAL_VERSION_ID);
    expect(unavailable.choices).toEqual([
      expect.objectContaining({
        id: CREDENTIAL_ID,
        credential_version_id: CREDENTIAL_VERSION_ID,
        historical: false,
        unavailable: true,
      }),
    ]);

    const revoked = buildAdminModelCredentialEditorState(model, [
      {
        id: CREDENTIAL_ID,
        display_name: "已撤销但仍被引用的凭据",
        credential_type: "model_api_key",
        current_version_id: CREDENTIAL_VERSION_ID,
        version: 4,
        status: "revoked",
      },
    ]);
    expect(revoked.choices).toEqual([
      expect.objectContaining({
        id: CREDENTIAL_ID,
        credential_version_id: CREDENTIAL_VERSION_ID,
        display_name: "已撤销但仍被引用的凭据",
        unavailable: true,
      }),
    ]);
  });

  test("requires a current credential binding without blocking CLI providers", () => {
    expect(canSubmitAdminModelEditor("loading", false)).toBe(false);
    expect(canSubmitAdminModelEditor("error", false)).toBe(false);
    expect(canSubmitAdminModelEditor("ready", true)).toBe(false);
    expect(canSubmitAdminModelEditor("ready", false)).toBe(true);
    expect(canSubmitAdminModelEditor("ready", false, true, false)).toBe(false);
    expect(canSubmitAdminModelEditor("error", false, false, true)).toBe(true);
    expect(canSubmitAdminModelEditor("loading", false, false, true)).toBe(true);
    expect(canCloseAdminModelEditor(true)).toBe(false);
    expect(canCloseAdminModelEditor(false)).toBe(true);
  });

  test("builds safe provider settings and credential references from the editor", () => {
    const parsed = parseAdminModelEditorForm(
      formData({
        logical_name: "analysis-pro",
        display_name: "分析模型 Pro",
        description: "适合深度分析任务",
        provider_adapter: "openai",
        provider_model: "gpt-5.2",
        status: "active",
        base_url: "https://models.example.invalid/v1",
        temperature: "0.2",
        max_tokens: "16384",
        request_timeout: "90",
        max_retries: "3",
        advanced_settings: '{"reasoning_effort":"high"}',
        supports_thinking: "on",
        supports_reasoning_effort: "on",
        credential_id: CREDENTIAL_ID,
        credential_version_id: CREDENTIAL_VERSION_ID,
        credential_env_key: "OPENAI_API_KEY",
      }),
    );

    expect(parsed).toEqual({
      logical_name: "analysis-pro",
      display_name: "分析模型 Pro",
      description: "适合深度分析任务",
      provider_adapter: "openai",
      provider_model: "gpt-5.2",
      settings: {
        reasoning_effort: "high",
        base_url: "https://models.example.invalid/v1",
        temperature: 0.2,
        max_tokens: 16384,
        request_timeout: 90,
        max_retries: 3,
      },
      supports_thinking: true,
      supports_reasoning_effort: true,
      supports_vision: false,
      credential_id: CREDENTIAL_ID,
      credential_version_id: CREDENTIAL_VERSION_ID,
      credential_env_key: "OPENAI_API_KEY",
      sort_order: 0,
      status: "active",
    });
  });

  test("rejects advanced JSON that could put plaintext credentials in cache", () => {
    let captured: unknown;
    try {
      parseAdminModelEditorForm(
        formData({
          logical_name: "analysis-pro",
          display_name: "分析模型 Pro",
          provider_adapter: "openai",
          provider_model: "gpt-5.2",
          status: "active",
          advanced_settings: '{"api_key":"must-not-enter-cache"}',
        }),
      );
    } catch (error) {
      captured = error;
    }

    expect(captured).toBeInstanceOf(Error);
    expect((captured as Error).message).toBe(
      "高级 JSON 只能包含支持的安全字段和精确类型",
    );
    expect(getAdminModelEditorErrorField(captured)).toBe("advanced_settings");
    expect(
      adminModelEditorFieldErrorAttributes(
        "advanced_settings",
        getAdminModelEditorErrorField(captured),
      ),
    ).toEqual({
      "aria-describedby": "admin-model-editor-error",
      "aria-invalid": true,
    });
  });

  test("associates editor field hints together with validation errors", () => {
    expect(
      adminModelEditorFieldErrorAttributes(
        "advanced_settings",
        "advanced_settings",
        "admin-model-advanced-settings-hint",
      ),
    ).toEqual({
      "aria-describedby":
        "admin-model-advanced-settings-hint admin-model-editor-error",
      "aria-invalid": true,
    });
    expect(
      adminModelEditorFieldErrorAttributes(
        "sort_order",
        null,
        "admin-model-sort-order-hint",
      ),
    ).toEqual({
      "aria-describedby": "admin-model-sort-order-hint",
    });
  });

  test("identifies the exact numeric field that failed client validation", () => {
    let captured: unknown;
    try {
      parseAdminModelEditorForm(
        formData({
          logical_name: "analysis-pro",
          display_name: "分析模型 Pro",
          provider_adapter: "openai",
          provider_model: "gpt-5.2",
          status: "active",
          temperature: "not-a-number",
          advanced_settings: "{}",
        }),
      );
    } catch (error) {
      captured = error;
    }

    expect(getAdminModelEditorErrorField(captured)).toBe("temperature");
  });

  test("uses caller-provided locale messages for pure form validation", () => {
    expect(() =>
      parseAdminModelEditorForm(
        formData({
          logical_name: "analysis-pro",
          display_name: "Analysis Pro",
          provider_adapter: "openai",
          provider_model: "gpt-5.2",
          status: "active",
          advanced_settings: '{"api_key":"must-not-enter-cache"}',
        }),
        enUS.adminModelSettings.validation,
      ),
    ).toThrow(
      "Advanced JSON may contain only supported safe fields with exact value types",
    );
  });
});
