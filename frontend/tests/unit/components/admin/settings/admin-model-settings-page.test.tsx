import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AdminModelProviderAdapterOptions,
  AdminModelProviderSettingInput,
  AdminModelCatalogStateView,
  findAdminModelProviderAdapterDescriptor,
  parseAdminModelConnectionTestForm,
  parseAdminModelEditorForm,
  selectAdminModelCatalogItems,
  selectAdminModelVisibleSettingFields,
  type AdminModelCredentialOption,
} from "@/components/admin/settings/admin-model-settings-page";
import {
  createAdminModelInputSchema,
  testAdminModelConnectionInputSchema,
  type AdminModelCatalog,
  type AdminModelProviderAdapterDescriptor,
  type AdminModelProviderSettingField,
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

function settingField(
  value: Partial<AdminModelProviderSettingField> &
    Pick<AdminModelProviderSettingField, "name" | "label" | "input_type">,
): AdminModelProviderSettingField {
  return {
    advanced: false,
    minimum: null,
    maximum: null,
    step: null,
    options: [],
    ...value,
  };
}

const BASE_URL_FIELD = settingField({
  name: "base_url",
  label: "Base URL",
  input_type: "url",
});
const OPENAI_DESCRIPTOR: AdminModelProviderAdapterDescriptor = {
  id: "openai",
  credential_required: true,
  setting_fields: [
    BASE_URL_FIELD,
    settingField({
      name: "max_tokens",
      label: "Max tokens",
      input_type: "integer",
      minimum: 1,
      maximum: 2_000_000,
      step: 1,
    }),
    settingField({
      name: "request_timeout",
      label: "Request timeout (seconds)",
      input_type: "number",
      minimum: 0.1,
      maximum: 3_600,
      step: 0.1,
    }),
    settingField({
      name: "temperature",
      label: "Temperature",
      input_type: "number",
      minimum: -2,
      maximum: 2,
      step: 0.01,
    }),
    settingField({
      name: "use_responses_api",
      label: "Use Responses API",
      input_type: "boolean",
      advanced: true,
    }),
  ],
};

const VENDOR_QUALITY_FIELD = settingField({
  name: "vendor_quality",
  label: "Vendor quality",
  input_type: "integer",
  minimum: 1,
  maximum: 7,
  step: 1,
});
const NEW_VENDOR_DESCRIPTOR: AdminModelProviderAdapterDescriptor = {
  id: "new_vendor_v2",
  credential_required: false,
  setting_fields: [VENDOR_QUALITY_FIELD],
};

function renderCatalog(): string {
  const catalog: AdminModelCatalog = {
    catalog_revision: 1,
    request_id: "test-request",
    provider_adapters: [OPENAI_DESCRIPTOR],
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

function renderRetiredVisionCatalog(): string {
  const catalog: AdminModelCatalog = {
    catalog_revision: 1,
    request_id: "test-request",
    provider_adapters: [OPENAI_DESCRIPTOR],
    items: [
      {
        id: "33333333-3333-4333-8333-333333333334",
        display_name: "Historical fake vision model",
        provider_adapter: "vision_bridge_fake",
        provider_model: "vision-bridge-fake-v1",
        settings: {},
        supports_thinking: false,
        supports_reasoning_effort: false,
        supports_vision: true,
        credential_id: null,
        credential_version_id: null,
        credential_env_key: null,
        status: "suspended",
        is_default: false,
        revision: 1,
        version_number: 1,
        updated_at: "2026-08-16T00:00:00+00:00",
      },
      {
        id: "33333333-3333-4333-8333-333333333335",
        display_name: "Historical compatible vision model",
        provider_adapter: "vision_openai_compatible_v1",
        provider_model: "small-vlm",
        settings: { base_url: "https://vision.example.test/v1" },
        supports_thinking: false,
        supports_reasoning_effort: false,
        supports_vision: true,
        credential_id: credentials[0]!.id,
        credential_version_id: credentials[0]!.current_version_id,
        credential_env_key: "VISION_API_KEY",
        status: "suspended",
        is_default: false,
        revision: 1,
        version_number: 1,
        updated_at: "2026-08-16T00:00:00+00:00",
      },
    ],
  };
  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
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
  test("renders provider options only from backend descriptors", () => {
    const markup = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <select>
          <AdminModelProviderAdapterOptions
            providerAdapters={[NEW_VENDOR_DESCRIPTOR]}
            value="new_vendor_v2"
          />
        </select>
      </I18nProvider>,
    );

    expect(markup).toContain('value="new_vendor_v2"');
    expect(markup).not.toContain('value="openai"');
    expect(markup).not.toContain("vision_bridge_fake");
    expect(markup).not.toContain("vision_openai_compatible_v1");
    expect(
      findAdminModelProviderAdapterDescriptor(
        [NEW_VENDOR_DESCRIPTOR],
        "openai",
      ),
    ).toBeUndefined();
    expect(
      selectAdminModelVisibleSettingFields(NEW_VENDOR_DESCRIPTOR).map(
        (field) => field.name,
      ),
    ).toEqual(["vendor_quality"]);
  });

  test("renders and submits a custom field from a newly registered backend adapter", () => {
    const markup = renderToStaticMarkup(
      <AdminModelProviderSettingInput
        field={VENDOR_QUALITY_FIELD}
        value="5"
        invalidFieldId={null}
      />,
    );
    expect(markup).toContain('name="vendor_quality"');
    expect(markup).toContain('type="number"');
    expect(markup).toContain('min="1"');
    expect(markup).toContain('max="7"');
    expect(markup).toContain("Vendor quality");

    const formData = new FormData();
    formData.set("display_name", "New vendor model");
    formData.set("provider_adapter", "new_vendor_v2");
    formData.set("provider_model", "vendor-model-1");
    formData.set("vendor_quality", "5");
    formData.set("advanced_settings", "{}");
    formData.set("status", "suspended");

    const input = parseAdminModelEditorForm(formData, NEW_VENDOR_DESCRIPTOR);
    expect(createAdminModelInputSchema.parse(input)).toMatchObject({
      provider_adapter: "new_vendor_v2",
      settings: {
        vendor_quality: 5,
      },
      credential_id: null,
    });

    formData.set("vendor_quality", "8");
    expect(() =>
      parseAdminModelEditorForm(formData, NEW_VENDOR_DESCRIPTOR),
    ).toThrow();
    formData.set("vendor_quality", "5");
    formData.set("credential_id", credentials[0]!.id);
    formData.set("credential_version_id", credentials[0]!.current_version_id!);
    formData.set("credential_env_key", "VENDOR_API_KEY");
    expect(() =>
      parseAdminModelEditorForm(formData, NEW_VENDOR_DESCRIPTOR),
    ).toThrow();
  });

  test("still renders historical Vision Bridge adapters as retired", () => {
    const markup = renderRetiredVisionCatalog();

    expect(markup).toContain("Historical fake vision model");
    expect(markup).toContain("Historical compatible vision model");
    expect(markup).toContain("vision-bridge-fake-v1");
    expect(markup).toContain("small-vlm");
    expect(markup).toContain(
      enUS.adminModelSettings.editor.retiredProviderAdapter,
    );
    expect(markup).toContain('aria-disabled="true"');
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
      provider_adapters: [OPENAI_DESCRIPTOR],
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

  test("enforces descriptor setting metadata before the mutation schema", () => {
    const baseUrlOnlyDescriptor: AdminModelProviderAdapterDescriptor = {
      ...OPENAI_DESCRIPTOR,
      setting_fields: [BASE_URL_FIELD],
    };
    const formData = new FormData();
    formData.set("display_name", "Restricted model");
    formData.set("provider_adapter", "openai");
    formData.set("provider_model", "gpt-restricted");
    formData.set("base_url", "https://api.example.test/v1");
    formData.set("advanced_settings", '{"temperature":0.2}');
    formData.set("status", "suspended");
    formData.set("credential_id", credentials[0]!.id);
    formData.set("credential_version_id", credentials[0]!.current_version_id!);
    formData.set("credential_env_key", "OPENAI_API_KEY");

    expect(baseUrlOnlyDescriptor.credential_required).toBe(true);
    expect(() =>
      parseAdminModelEditorForm(
        formData,
        baseUrlOnlyDescriptor,
        enUS.adminModelSettings.validation,
      ),
    ).toThrow(enUS.adminModelSettings.validation.advancedJsonUnsafe);
  });

  test("keeps the vision probe intent in connection-test requests", () => {
    const formData = new FormData();
    formData.set("display_name", "Test model");
    formData.set("provider_adapter", "openai");
    formData.set("provider_model", "gpt-vision-test");
    formData.set("base_url", "https://api.example.test/v1");
    formData.set("advanced_settings", "{}");
    formData.set("status", "active");
    formData.set("supports_vision", "on");
    formData.set("credential_id", credentials[0]!.id);
    formData.set("credential_version_id", credentials[0]!.current_version_id!);
    formData.set("credential_env_key", "OPENAI_API_KEY");

    const input = parseAdminModelConnectionTestForm(
      formData,
      OPENAI_DESCRIPTOR,
    );

    expect(testAdminModelConnectionInputSchema.parse(input)).toMatchObject({
      supports_vision: true,
    });
  });
});
