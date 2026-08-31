"use client";

import {
  PencilIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
  Trash2Icon,
} from "lucide-react";
import { useMemo, useState } from "react";

import {
  ModelCreateDialog,
  ProviderEditorDialog,
  ProviderModelList,
  registryCopy,
  registryErrorText,
} from "@/components/admin/settings/admin-model-registry-page";
import {
  AdminPage,
  AdminPageHeader,
  AdminSection,
} from "@/components/admin/ui/admin-page";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useAdminModelProviders,
  useDeleteAdminModelProvider,
  useDeleteAdminProviderModel,
} from "@/core/admin-settings/model-registry/hooks";
import type {
  AdminModelProviderItem,
  AdminProviderModelItem,
} from "@/core/admin-settings/model-registry/types";
import {
  useAdminModelCatalog,
  useCreateAdminModel,
  useReplaceAdminModel,
  useSetAdminModelDefault,
  useSetAdminModelStatus,
  useTestAdminModelConnection,
  ADMIN_MODEL_MAX_INPUT_TOKENS,
  createAdminModelProviderSettingsDraft,
  resetAdminModelProviderSettingDraftValue,
  serializeAdminModelProviderSettingsDraft,
  updateAdminModelProviderSettingDraftValue,
  type AdminModelItem,
  type AdminModelProviderAdapterDescriptor,
  type AdminModelProviderSettingField,
  type AdminModelProviderSettingsDraft,
  type CreateAdminModelInput,
} from "@/core/admin-settings/models";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Locale } from "@/core/i18n";
import { useI18n } from "@/core/i18n/hooks";

type EditorTarget = AdminModelItem | null;

const MODEL_SETTINGS_COPY = {
  "en-US": {
    pageTitle: "Model management",
    searchModels: "Search models",
    searchPlaceholder: "Search by name, adapter, or model ID",
    filterStatus: "Filter models by status",
    allStatuses: "All statuses",
    active: "Active",
    suspended: "Suspended",
    enable: "Enable",
    disable: "Disable",
    refresh: "Refresh",
    addModel: "Add text model",
    catalogLoadFailed: "The model catalog could not be loaded.",
    retry: "Retry",
    noMatches: "No text models match the current filter.",
    editModel: "Edit model",
    dialogDescription:
      "A text model binds one provider; the provider owns the endpoint and the API Key, and the model stores no credential of its own. Connection tests use the provider's saved Key.",
    displayName: "Display name",
    providerBinding: "Provider",
    providerEndpoint: "Endpoint",
    providerMissing:
      "No provider is available. Create a model provider before adding a text model.",
    rebindWarning:
      "Rebinding re-encrypts this model's credential with the new provider's Key and switches its endpoint; Runs frozen on the old material become stale.",
    adapter: "Adapter",
    providerModelId: "Model ID at the provider",
    maximumInputTokens: "Maximum input tokens",
    maximumInputTokensHint:
      "The model's maximum input context and the denominator for context-usage percentages; this is not the maximum output token limit.",
    providerSettings: "Adapter settings",
    advancedSettings: "Advanced settings",
    providerDefault: "Provider default",
    enabledValue: "Enabled",
    disabledValue: "Disabled",
    preservedSettings:
      "{count} structured advanced setting(s) will be preserved unchanged without displaying raw JSON.",
    incompatibleSettings:
      "This model contains settings that the current adapter form cannot safely edit: {keys}. Saving and connection testing are disabled.",
    unknownProvider:
      "This model uses an unknown historical adapter ({provider}). Saving and connection testing are disabled.",
    supportsThinking: "Thinking",
    supportsReasoningEffort: "Reasoning effort",
    supportsVision: "Vision",
    testingConnection: "Testing…",
    testConnection: "Test connection",
    testConnectionHint:
      "Uses the bound provider's saved Key and may incur provider charges; nothing is saved by a test.",
    cancel: "Cancel",
    saving: "Saving…",
    save: "Save",
    invalidProviderSettings: "Provider settings are invalid.",
    invalidMaximumInputTokens:
      "Maximum input tokens must be a whole number from 1 to 2,000,000.",
    invalidModelConfiguration: "Model configuration is invalid.",
    saveFailed: "The model could not be saved.",
    testFailed: "Connection test failed.",
    connectionSucceeded:
      "Connection test succeeded using the provider's saved Key.",
    connectionFailed:
      "Connection test failed using the provider's saved Key. Check the provider's endpoint and Key, or this model's target.",
    default: "Default",
    credential: "Credential",
    configured: "Configured",
    notConfigured: "Not configured",
    secretRevision: "Secret revision",
    readiness: "Readiness",
    ready: "Ready",
    unready: "Not ready",
    configurationRevision: "Configuration revision",
    edit: "Edit",
    setDefault: "Set as default",
    operationFailed: "Model operation failed.",
  },
  "zh-CN": {
    pageTitle: "模型管理",
    searchModels: "搜索模型",
    searchPlaceholder: "搜索名称、适配器或模型 ID",
    filterStatus: "筛选模型状态",
    allStatuses: "全部状态",
    active: "启用",
    suspended: "停用",
    enable: "启用",
    disable: "停用",
    refresh: "刷新",
    addModel: "添加文本模型",
    catalogLoadFailed: "模型目录读取失败。",
    retry: "重试",
    noMatches: "当前筛选条件下没有匹配的文本模型。",
    editModel: "编辑模型",
    dialogDescription:
      "文本模型绑定一个供应商；服务地址和 API Key 由供应商统一持有，模型自身不保存任何凭据。连接测试直接使用供应商已保存的 Key。",
    displayName: "显示名称",
    providerBinding: "所属供应商",
    providerEndpoint: "服务地址",
    providerMissing: "还没有可用的供应商。请先创建模型供应商，再添加文本模型。",
    rebindWarning:
      "改绑供应商会用新供应商的 Key 重新加密此模型的凭据并切换服务地址；冻结旧材料的 Run 会失效。",
    adapter: "适配器",
    providerModelId: "供应商侧模型 ID",
    maximumInputTokens: "最大输入 Token",
    maximumInputTokensHint:
      "模型可接收的最大输入上下文，也是上下文用量百分比的分母；不是最大输出 Token。",
    providerSettings: "适配器设置",
    advancedSettings: "高级设置",
    providerDefault: "Provider 默认",
    enabledValue: "启用",
    disabledValue: "禁用",
    preservedSettings: "将原样保留 {count} 项结构化高级设置，不展示原始 JSON。",
    incompatibleSettings:
      "当前模型包含此适配器表单无法安全编辑的设置：{keys}。已禁止保存和连接测试。",
    unknownProvider:
      "当前模型使用未知的历史适配器（{provider}）。已禁止保存和连接测试。",
    supportsThinking: "思考模式",
    supportsReasoningEffort: "推理强度",
    supportsVision: "视觉输入",
    testingConnection: "正在测试…",
    testConnection: "测试连接",
    testConnectionHint:
      "使用绑定供应商已保存的 Key 发起真实请求，可能产生供应商计费；测试不保存任何配置。",
    cancel: "取消",
    saving: "正在保存…",
    save: "保存",
    invalidProviderSettings: "Provider 设置无效。",
    invalidMaximumInputTokens:
      "最大输入 Token 必须是 1 到 2,000,000 之间的整数。",
    invalidModelConfiguration: "模型配置无效。",
    saveFailed: "模型保存失败。",
    testFailed: "连接测试失败。",
    connectionSucceeded: "连接测试成功（使用供应商已保存的 Key）。",
    connectionFailed:
      "连接测试失败（使用供应商已保存的 Key）。请检查供应商的服务地址与 Key，或此模型的目标配置。",
    default: "默认",
    credential: "凭据",
    configured: "已配置",
    notConfigured: "未配置",
    secretRevision: "秘密 revision",
    readiness: "就绪状态",
    ready: "就绪",
    unready: "未就绪",
    configurationRevision: "配置 revision",
    edit: "编辑",
    setDefault: "设为默认",
    operationFailed: "模型操作失败。",
  },
} as const satisfies Record<Locale, Record<string, string>>;

export function adminModelSettingsCopy(locale: Locale) {
  return MODEL_SETTINGS_COPY[locale];
}

const BUILTIN_PROVIDER_SETTING_LABELS: Record<
  string,
  Record<Locale, string>
> = {
  max_tokens: {
    "en-US": "Maximum output tokens",
    "zh-CN": "最大输出 Token",
  },
  temperature: { "en-US": "Temperature", "zh-CN": "温度" },
  request_timeout: {
    "en-US": "Request timeout (seconds)",
    "zh-CN": "请求超时（秒）",
  },
  default_request_timeout: {
    "en-US": "Default request timeout (seconds)",
    "zh-CN": "默认请求超时（秒）",
  },
  stream_chunk_timeout: {
    "en-US": "Stream chunk timeout (seconds)",
    "zh-CN": "流式分片超时（秒）",
  },
  timeout: {
    "en-US": "Timeout (seconds)",
    "zh-CN": "超时（秒）",
  },
  reasoning_effort: {
    "en-US": "Reasoning effort",
    "zh-CN": "推理强度",
  },
  reasoning_summary: {
    "en-US": "Reasoning summary",
    "zh-CN": "推理摘要",
  },
  cumulative_stream_usage: {
    "en-US": "Cumulative stream usage",
    "zh-CN": "累计流用量",
  },
};

export function adminModelProviderSettingLabel(
  name: string,
  fallback: string,
  locale: Locale,
): string {
  return BUILTIN_PROVIDER_SETTING_LABELS[name]?.[locale] ?? fallback;
}

export function selectAdminModelCatalogItems(
  items: readonly AdminModelItem[],
  query: string,
  status: "all" | "active" | "suspended",
): AdminModelItem[] {
  const normalized = query.trim().toLocaleLowerCase();
  return items.filter(
    (item) =>
      (status === "all" || item.status === status) &&
      (!normalized ||
        item.display_name.toLocaleLowerCase().includes(normalized) ||
        item.provider_model.toLocaleLowerCase().includes(normalized) ||
        item.provider_adapter.toLocaleLowerCase().includes(normalized)),
  );
}

function formString(form: FormData, name: string, fallback = ""): string {
  const value = form.get(name);
  return typeof value === "string" ? value : fallback;
}

/**
 * Validates and serializes the model editor form. The submission carries no
 * credential: the API Key lives on the bound Provider and is materialized
 * server-side for both saves and connection tests.
 */
export function consumeAdminModelEditorSubmission(
  form: FormData,
  descriptor: AdminModelProviderAdapterDescriptor | undefined,
  providerSettingsDraft: AdminModelProviderSettingsDraft,
  locale: Locale = "zh-CN",
) {
  let settings: CreateAdminModelInput["settings"];
  try {
    settings = serializeAdminModelProviderSettingsDraft(
      descriptor,
      providerSettingsDraft,
    );
  } catch {
    throw new Error(adminModelSettingsCopy(locale).invalidProviderSettings);
  }
  const maxInputTokensText = formString(form, "max_input_tokens").trim();
  if (!/^[1-9][0-9]*$/u.test(maxInputTokensText)) {
    throw new Error(adminModelSettingsCopy(locale).invalidMaximumInputTokens);
  }
  const maxInputTokens = Number(maxInputTokensText);
  if (
    !Number.isSafeInteger(maxInputTokens) ||
    maxInputTokens > ADMIN_MODEL_MAX_INPUT_TOKENS
  ) {
    throw new Error(adminModelSettingsCopy(locale).invalidMaximumInputTokens);
  }
  return {
    display_name: formString(form, "display_name").trim(),
    provider_adapter: descriptor?.id ?? providerSettingsDraft.provider_adapter,
    provider_model: formString(form, "provider_model").trim(),
    max_input_tokens: maxInputTokens,
    settings,
    supports_thinking: form.get("supports_thinking") === "on",
    supports_reasoning_effort: form.get("supports_reasoning_effort") === "on",
    supports_vision: form.get("supports_vision") === "on",
  };
}

export function isAdminModelEditorSaveDisabled({
  pending,
  providerMissing,
  providerSettingsIncompatible,
  testPending,
}: {
  pending: boolean;
  providerMissing: boolean;
  providerSettingsIncompatible: boolean;
  testPending: boolean;
}): boolean {
  return (
    pending || testPending || providerSettingsIncompatible || providerMissing
  );
}

export function adminModelConnectionTestResultMessage(
  status: "failed" | "succeeded",
  locale: Locale,
): string {
  const copy = adminModelSettingsCopy(locale);
  return status === "succeeded"
    ? copy.connectionSucceeded
    : copy.connectionFailed;
}

function ProviderSettingInput({
  field,
  locale,
  value,
  onChange,
  onReset,
}: {
  field: AdminModelProviderSettingField;
  locale: Locale;
  value: string;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const copy = adminModelSettingsCopy(locale);
  const label = adminModelProviderSettingLabel(field.name, field.label, locale);
  const selectClassName =
    "border-input bg-background h-9 rounded-md border px-3 text-sm";

  let control: React.ReactNode;
  if (field.input_type === "boolean") {
    control = (
      <select
        className={selectClassName}
        value={value}
        aria-label={label}
        onChange={(event) => onChange(event.target.value)}
      >
        {field.default_mode === "provider" ? (
          <option value="">{copy.providerDefault}</option>
        ) : null}
        <option value="true">{copy.enabledValue}</option>
        <option value="false">{copy.disabledValue}</option>
      </select>
    );
  } else if (field.input_type === "enum") {
    control = (
      <select
        className={selectClassName}
        value={value}
        aria-label={label}
        onChange={(event) => onChange(event.target.value)}
      >
        {field.default_mode === "provider" ? (
          <option value="">{copy.providerDefault}</option>
        ) : null}
        {field.options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  } else {
    control = (
      <Input
        type={
          field.input_type === "integer" || field.input_type === "number"
            ? "number"
            : field.input_type === "url"
              ? "url"
              : "text"
        }
        value={value}
        min={field.minimum ?? undefined}
        max={field.maximum ?? undefined}
        step={field.step ?? undefined}
        placeholder={
          field.default_mode === "provider" ? copy.providerDefault : undefined
        }
        aria-label={label}
        onChange={(event) => onChange(event.target.value)}
        onBlur={(event) => {
          if (
            field.default_mode === "platform" &&
            event.currentTarget.value.trim() === ""
          ) {
            onReset();
          }
        }}
      />
    );
  }

  return (
    <label className="grid gap-2 text-sm">
      {label}
      {control}
    </label>
  );
}

function ProviderSettingsFields({
  descriptor,
  draft,
  locale,
  onChange,
  onReset,
}: {
  descriptor: AdminModelProviderAdapterDescriptor;
  draft: AdminModelProviderSettingsDraft;
  locale: Locale;
  onChange: (name: string, value: string) => void;
  onReset: (name: string) => void;
}) {
  const copy = adminModelSettingsCopy(locale);
  const editableFields = descriptor.setting_fields.filter(
    (field) =>
      field.form_control === "input" &&
      field.input_type !== "json" &&
      !Object.hasOwn(draft.preserved_settings, field.name) &&
      !draft.incompatible_keys.includes(field.name),
  );
  const directFields = editableFields.filter((field) => !field.advanced);
  const advancedFields = editableFields.filter((field) => field.advanced);
  const preservedCount = Object.keys(draft.preserved_settings).length;

  if (
    directFields.length === 0 &&
    advancedFields.length === 0 &&
    preservedCount === 0
  ) {
    return null;
  }

  const renderField = (field: AdminModelProviderSettingField) => (
    <ProviderSettingInput
      key={field.name}
      field={field}
      locale={locale}
      value={draft.values[field.name] ?? ""}
      onChange={(value) => onChange(field.name, value)}
      onReset={() => onReset(field.name)}
    />
  );

  return (
    <section className="space-y-3 rounded-lg border p-4">
      <h3 className="text-sm font-medium">{copy.providerSettings}</h3>
      {directFields.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-2">
          {directFields.map(renderField)}
        </div>
      ) : null}
      {advancedFields.length > 0 || preservedCount > 0 ? (
        <details
          key={descriptor.id}
          className="group rounded-md border px-3 py-2"
        >
          <summary className="cursor-pointer text-sm font-medium">
            {copy.advancedSettings}
          </summary>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            {advancedFields.map(renderField)}
          </div>
          {preservedCount > 0 ? (
            <p className="text-muted-foreground mt-3 text-xs leading-5">
              {copy.preservedSettings.replace(
                "{count}",
                String(preservedCount),
              )}
            </p>
          ) : null}
        </details>
      ) : null}
    </section>
  );
}

function ModelEditorDialog({
  accountId,
  open,
  target,
  descriptors,
  providers,
  initialProviderId,
  onOpenChange,
  onSaved,
}: {
  accountId: string;
  open: boolean;
  target: EditorTarget;
  descriptors: AdminModelProviderAdapterDescriptor[];
  providers: readonly AdminModelProviderItem[];
  initialProviderId: string | null;
  onOpenChange: (open: boolean) => void;
  onSaved: () => Promise<unknown>;
}) {
  const { locale } = useI18n();
  const copy = adminModelSettingsCopy(locale);
  const initialAdapter = target?.provider_adapter ?? descriptors[0]?.id ?? "";
  const initialDescriptor = descriptors.find(
    (item) => item.id === initialAdapter,
  );
  const [adapter, setAdapter] = useState(initialAdapter);
  const [providerId, setProviderId] = useState(
    () => target?.provider_id ?? initialProviderId ?? providers[0]?.id ?? "",
  );
  const [providerSettingsDraft, setProviderSettingsDraft] = useState(() =>
    createAdminModelProviderSettingsDraft(
      initialDescriptor,
      target?.settings ?? {},
      initialAdapter,
    ),
  );
  const [pending, setPending] = useState(false);
  const [testPending, setTestPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const create = useCreateAdminModel(accountId);
  const replace = useReplaceAdminModel(accountId);
  const test = useTestAdminModelConnection(accountId);
  const selectedDescriptor = descriptors.find((item) => item.id === adapter);
  const selectedProvider = providers.find((item) => item.id === providerId);
  const providerMissing = selectedProvider === undefined;
  const rebinding =
    target !== null && providerId !== "" && providerId !== target.provider_id;
  const providerSettingsIncompatible =
    providerSettingsDraft.unknown_provider ||
    providerSettingsDraft.incompatible_keys.length > 0;

  function changeAdapter(nextAdapter: string) {
    const nextDescriptor = descriptors.find((item) => item.id === nextAdapter);
    setAdapter(nextAdapter);
    setProviderSettingsDraft(
      createAdminModelProviderSettingsDraft(nextDescriptor, {}, nextAdapter),
    );
    setError(null);
    setTestResult(null);
  }

  function changeProviderBinding(nextProviderId: string) {
    setProviderId(nextProviderId);
    setError(null);
    // The provider decides the endpoint the test would hit, so a previous
    // verdict no longer describes the current form.
    setTestResult(null);
  }

  function updateProviderSetting(name: string, value: string) {
    setProviderSettingsDraft((current) =>
      updateAdminModelProviderSettingDraftValue(current, name, value),
    );
  }

  function resetProviderSetting(name: string) {
    if (!selectedDescriptor) return;
    setProviderSettingsDraft((current) =>
      resetAdminModelProviderSettingDraftValue(
        selectedDescriptor,
        current,
        name,
      ),
    );
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (providerMissing) {
      setError(copy.providerMissing);
      return;
    }
    let common: ReturnType<typeof consumeAdminModelEditorSubmission>;
    try {
      common = consumeAdminModelEditorSubmission(
        new FormData(event.currentTarget),
        selectedDescriptor,
        providerSettingsDraft,
        locale,
      );
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : copy.invalidModelConfiguration,
      );
      return;
    }
    setPending(true);
    try {
      if (target) {
        await replace.execute({
          modelId: target.id,
          input: { ...common, provider_id: providerId },
        });
      } else {
        await create.execute({
          ...common,
          status: "active",
          provider_id: providerId,
        });
      }
      await onSaved();
      onOpenChange(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : copy.saveFailed);
    } finally {
      setPending(false);
    }
  }

  async function testConnection(form: HTMLFormElement) {
    setError(null);
    setTestResult(null);
    if (providerMissing) {
      setError(copy.providerMissing);
      return;
    }
    let common: ReturnType<typeof consumeAdminModelEditorSubmission>;
    try {
      common = consumeAdminModelEditorSubmission(
        new FormData(form),
        selectedDescriptor,
        providerSettingsDraft,
        locale,
      );
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : copy.invalidModelConfiguration,
      );
      return;
    }
    setTestPending(true);
    try {
      const result = await test.execute({
        provider_id: providerId,
        provider_adapter: common.provider_adapter,
        provider_model: common.provider_model,
        max_input_tokens: common.max_input_tokens,
        settings: common.settings,
        supports_vision: common.supports_vision,
      });
      setTestResult(adminModelConnectionTestResultMessage(result.status, locale));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : copy.testFailed);
      setTestResult(adminModelConnectionTestResultMessage("failed", locale));
    } finally {
      setTestPending(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !pending && !testPending && onOpenChange(next)}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{target ? copy.editModel : copy.addModel}</DialogTitle>
          <DialogDescription>{copy.dialogDescription}</DialogDescription>
        </DialogHeader>
        <form className="space-y-4" onSubmit={save}>
          <label className="grid gap-2 text-sm">
            {copy.providerBinding}
            <select
              className="border-input bg-background h-9 rounded-md border px-3 text-sm"
              aria-label={copy.providerBinding}
              value={providerId}
              onChange={(event) => changeProviderBinding(event.target.value)}
            >
              {providers.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
            {selectedProvider ? (
              <span className="text-muted-foreground truncate font-mono text-xs">
                {copy.providerEndpoint}: {selectedProvider.base_url}
              </span>
            ) : null}
          </label>
          {providerMissing ? (
            <p role="alert" className="text-destructive text-sm">
              {copy.providerMissing}
            </p>
          ) : null}
          {rebinding ? (
            <p
              data-testid="admin-model-rebind-warning"
              className="text-muted-foreground text-xs leading-5"
            >
              {copy.rebindWarning}
            </p>
          ) : null}
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="grid gap-2 text-sm">
              {copy.displayName}
              <Input
                name="display_name"
                required
                defaultValue={target?.display_name ?? ""}
              />
            </label>
            <label className="grid gap-2 text-sm">
              {copy.adapter}
              <select
                className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                aria-label={copy.adapter}
                value={adapter}
                onChange={(event) => changeAdapter(event.target.value)}
              >
                {!selectedDescriptor && adapter ? (
                  <option value={adapter}>{adapter}</option>
                ) : null}
                {descriptors.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.id}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="grid gap-2 text-sm">
            {copy.providerModelId}
            <Input
              name="provider_model"
              required
              defaultValue={target?.provider_model ?? ""}
            />
          </label>
          <label className="grid gap-2 text-sm">
            {copy.maximumInputTokens}
            <Input
              name="max_input_tokens"
              type="number"
              min={1}
              max={ADMIN_MODEL_MAX_INPUT_TOKENS}
              step={1}
              required
              defaultValue={target?.max_input_tokens ?? ""}
              aria-describedby="admin-model-max-input-tokens-hint"
            />
            <span
              id="admin-model-max-input-tokens-hint"
              className="text-muted-foreground text-xs leading-5"
            >
              {copy.maximumInputTokensHint}
            </span>
          </label>
          {selectedDescriptor ? (
            <ProviderSettingsFields
              key={adapter}
              descriptor={selectedDescriptor}
              draft={providerSettingsDraft}
              locale={locale}
              onChange={updateProviderSetting}
              onReset={resetProviderSetting}
            />
          ) : null}
          {providerSettingsIncompatible ? (
            <p role="alert" className="text-destructive text-sm">
              {providerSettingsDraft.unknown_provider
                ? copy.unknownProvider.replace("{provider}", adapter)
                : copy.incompatibleSettings.replace(
                    "{keys}",
                    providerSettingsDraft.incompatible_keys.join(", "),
                  )}
            </p>
          ) : null}
          <div className="grid gap-2 sm:grid-cols-3">
            <label className="flex items-center gap-2 text-sm">
              <input
                name="supports_thinking"
                type="checkbox"
                defaultChecked={target?.supports_thinking}
              />
              {copy.supportsThinking}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                name="supports_reasoning_effort"
                type="checkbox"
                defaultChecked={target?.supports_reasoning_effort}
              />
              {copy.supportsReasoningEffort}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                name="supports_vision"
                type="checkbox"
                defaultChecked={target?.supports_vision}
              />
              {copy.supportsVision}
            </label>
          </div>
          {error ? (
            <p role="alert" className="text-destructive text-sm">
              {error}
            </p>
          ) : null}
          {testResult ? (
            <p role="status" className="text-sm">
              {testResult}
            </p>
          ) : null}
          <p className="text-muted-foreground text-xs leading-5">
            {copy.testConnectionHint}
          </p>
          <DialogFooter className="flex-wrap">
            <Button
              type="button"
              variant="outline"
              disabled={
                pending ||
                testPending ||
                providerMissing ||
                providerSettingsIncompatible
              }
              onClick={(event) =>
                void testConnection(event.currentTarget.form!)
              }
            >
              {testPending ? copy.testingConnection : copy.testConnection}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={pending || testPending}
              onClick={() => onOpenChange(false)}
            >
              {copy.cancel}
            </Button>
            <Button
              type="submit"
              disabled={isAdminModelEditorSaveDisabled({
                pending,
                providerMissing,
                providerSettingsIncompatible,
                testPending,
              })}
            >
              {pending ? copy.saving : copy.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ModelCard({
  accountId,
  item,
  onEdit,
}: {
  accountId: string;
  item: AdminModelItem;
  onEdit: () => void;
}) {
  const { locale } = useI18n();
  const copy = adminModelSettingsCopy(locale);
  const status = useSetAdminModelStatus(accountId);
  const makeDefault = useSetAdminModelDefault(accountId);
  const error = status.error ?? makeDefault.error;
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="truncate">{item.display_name}</CardTitle>
            <p className="text-muted-foreground mt-1 truncate font-mono text-xs">
              {item.provider_adapter} / {item.provider_model}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {item.is_default ? <Badge>{copy.default}</Badge> : null}
            <Badge variant={item.status === "active" ? "default" : "secondary"}>
              {item.status === "active" ? copy.active : copy.suspended}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-muted-foreground text-xs">{copy.credential}</dt>
            <dd>
              {item.api_key_configured ? copy.configured : copy.notConfigured}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">
              {copy.maximumInputTokens}
            </dt>
            <dd>{item.max_input_tokens.toLocaleString(locale)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">
              {copy.secretRevision}
            </dt>
            <dd>{item.secret_revision}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">{copy.readiness}</dt>
            <dd>
              {item.secret_readiness === "ready" ? copy.ready : copy.unready}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">
              {copy.configurationRevision}
            </dt>
            <dd>{item.revision}</dd>
          </div>
        </dl>
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="outline" onClick={onEdit}>
            <PencilIcon aria-hidden className="size-4" />
            {copy.edit}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={status.isPending || item.is_default}
            onClick={() =>
              status.mutate({
                modelId: item.id,
                input: {
                  status: item.status === "active" ? "suspended" : "active",
                },
              })
            }
          >
            {item.status === "active" ? copy.disable : copy.enable}
          </Button>
          {!item.is_default ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={
                makeDefault.isPending ||
                item.status !== "active" ||
                item.secret_readiness !== "ready"
              }
              onClick={() =>
                makeDefault.mutate({ modelId: item.id, input: {} })
              }
            >
              {copy.setDefault}
            </Button>
          ) : null}
        </div>
        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {error instanceof Error ? error.message : copy.operationFailed}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function AdminModelSettingsPage() {
  const { user } = useAuth();
  const { locale } = useI18n();
  const copy = adminModelSettingsCopy(locale);
  const providerCopy = registryCopy(locale);
  const accountId = user?.id ?? "default";
  const catalog = useAdminModelCatalog(accountId);
  const providers = useAdminModelProviders(accountId, Boolean(user));
  const deleteProvider = useDeleteAdminModelProvider(accountId);
  const deleteModel = useDeleteAdminProviderModel(accountId);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<"all" | "active" | "suspended">("all");
  const [editor, setEditor] = useState<{
    open: boolean;
    target: EditorTarget;
    initialProviderId: string | null;
  }>({ open: false, target: null, initialProviderId: null });
  const [providerEditor, setProviderEditor] = useState<{
    open: boolean;
    target: AdminModelProviderItem | null;
  }>({ open: false, target: null });
  const [modelCreateProvider, setModelCreateProvider] =
    useState<AdminModelProviderItem | null>(null);
  const [providerToDelete, setProviderToDelete] =
    useState<AdminModelProviderItem | null>(null);
  const [modelToDelete, setModelToDelete] =
    useState<AdminProviderModelItem | null>(null);
  const textModels = useMemo(
    () =>
      selectAdminModelCatalogItems(catalog.data?.items ?? [], search, status),
    [catalog.data?.items, search, status],
  );
  if (!user) return null;

  const providerItems = providers.data ?? [];
  const loading = catalog.isLoading || providers.isLoading;
  const loadError = catalog.error ?? providers.error;

  const refresh = () => {
    void catalog.refetch();
    void providers.refetch();
  };

  return (
    <AdminPage>
      <AdminPageHeader
        title={copy.pageTitle}
        actions={
          <>
            <Button
              type="button"
              variant="outline"
              disabled={catalog.isFetching || providers.isFetching}
              onClick={refresh}
            >
              <RefreshCwIcon aria-hidden className="size-4" />
              {copy.refresh}
            </Button>
            <Button
              type="button"
              onClick={() => setProviderEditor({ open: true, target: null })}
            >
              <PlusIcon aria-hidden className="size-4" />
              {providerCopy.addProvider}
            </Button>
          </>
        }
      />
      <AdminSection
        title={providerCopy.sectionTitle}
        description={providerCopy.sectionDescription}
      >
        <div className="mb-5 flex flex-col gap-3 sm:flex-row">
          <label className="relative min-w-0 flex-1">
            <SearchIcon
              aria-hidden
              className="text-muted-foreground absolute top-2.5 left-3 size-4"
            />
            <Input
              className="pl-9"
              value={search}
              aria-label={copy.searchModels}
              placeholder={copy.searchPlaceholder}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <select
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
            aria-label={copy.filterStatus}
            value={status}
            onChange={(event) => setStatus(event.target.value as typeof status)}
          >
            <option value="all">{copy.allStatuses}</option>
            <option value="active">{copy.active}</option>
            <option value="suspended">{copy.suspended}</option>
          </select>
        </div>
        {loading ? (
          <div className="space-y-3">
            <Skeleton className="h-48" />
            <Skeleton className="h-48" />
          </div>
        ) : loadError ? (
          <div className="space-y-3">
            <p role="alert" className="text-destructive text-sm">
              {loadError instanceof Error
                ? loadError.message
                : copy.catalogLoadFailed}
            </p>
            <Button type="button" variant="outline" onClick={refresh}>
              {copy.retry}
            </Button>
          </div>
        ) : providerItems.length === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed p-8 text-center text-sm">
            {providerCopy.empty}
          </p>
        ) : (
          <div className="grid gap-4" data-testid="admin-model-provider-cards">
            {providerItems.map((provider) => (
              <ProviderCard
                key={provider.id}
                accountId={accountId}
                provider={provider}
                textModels={textModels.filter(
                  (item) => item.provider_id === provider.id,
                )}
                providerCopy={providerCopy}
                onEditProvider={() =>
                  setProviderEditor({ open: true, target: provider })
                }
                onDeleteProvider={() => {
                  deleteProvider.reset();
                  setProviderToDelete(provider);
                }}
                onAddTextModel={() =>
                  setEditor({
                    open: true,
                    target: null,
                    initialProviderId: provider.id,
                  })
                }
                onAddRetrievalModel={() => setModelCreateProvider(provider)}
                onEditTextModel={(item) =>
                  setEditor({
                    open: true,
                    target: item,
                    initialProviderId: null,
                  })
                }
                onDeleteRetrievalModel={(model) => {
                  deleteModel.reset();
                  setModelToDelete(model);
                }}
              />
            ))}
          </div>
        )}
      </AdminSection>
      {editor.open && catalog.data ? (
        <ModelEditorDialog
          key={`${editor.target?.id ?? "new"}:${editor.target?.revision ?? 0}:${editor.initialProviderId ?? ""}`}
          accountId={accountId}
          open
          target={editor.target}
          descriptors={catalog.data.provider_adapters}
          providers={providerItems}
          initialProviderId={editor.initialProviderId}
          onOpenChange={(open) =>
            setEditor((current) => ({ ...current, open }))
          }
          onSaved={() => catalog.refetch()}
        />
      ) : null}
      {providerEditor.open && catalog.data ? (
        <ProviderEditorDialog
          key={providerEditor.target?.id ?? "new-provider"}
          accountId={accountId}
          target={providerEditor.target}
          open
          onClose={() => setProviderEditor({ open: false, target: null })}
          copy={providerCopy}
          descriptors={catalog.data.provider_adapters}
          boundTextModels={(catalog.data.items ?? []).filter(
            (item) => item.provider_id === providerEditor.target?.id,
          )}
        />
      ) : null}
      {modelCreateProvider ? (
        <ModelCreateDialog
          accountId={accountId}
          provider={modelCreateProvider}
          open
          onClose={() => setModelCreateProvider(null)}
          copy={providerCopy}
        />
      ) : null}
      <Dialog
        open={providerToDelete !== null}
        onOpenChange={(open) => {
          if (!open) setProviderToDelete(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{providerCopy.deleteProviderTitle}</DialogTitle>
            <DialogDescription>
              {providerCopy.deleteProviderDescription(
                providerToDelete?.name ?? "",
              )}
            </DialogDescription>
          </DialogHeader>
          {deleteProvider.error ? (
            <p role="alert" className="text-destructive text-sm">
              {registryErrorText(deleteProvider.error, providerCopy)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setProviderToDelete(null)}
            >
              {providerCopy.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleteProvider.isPending}
              onClick={() => {
                if (providerToDelete === null) return;
                deleteProvider.mutate(providerToDelete.id, {
                  onSuccess: () => setProviderToDelete(null),
                });
              }}
            >
              {deleteProvider.isPending
                ? providerCopy.deleting
                : providerCopy.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={modelToDelete !== null}
        onOpenChange={(open) => {
          if (!open) setModelToDelete(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{providerCopy.deleteModelTitle}</DialogTitle>
            <DialogDescription>
              {providerCopy.deleteModelDescription(
                modelToDelete?.model_name ?? "",
              )}
            </DialogDescription>
          </DialogHeader>
          {deleteModel.error ? (
            <p role="alert" className="text-destructive text-sm">
              {registryErrorText(deleteModel.error, providerCopy)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setModelToDelete(null)}
            >
              {providerCopy.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleteModel.isPending}
              onClick={() => {
                if (modelToDelete === null) return;
                deleteModel.mutate(modelToDelete.id, {
                  onSuccess: () => setModelToDelete(null),
                });
              }}
            >
              {deleteModel.isPending
                ? providerCopy.deleting
                : providerCopy.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </AdminPage>
  );
}

function ProviderCard({
  accountId,
  provider,
  textModels,
  providerCopy,
  onEditProvider,
  onDeleteProvider,
  onAddTextModel,
  onAddRetrievalModel,
  onEditTextModel,
  onDeleteRetrievalModel,
}: {
  accountId: string;
  provider: AdminModelProviderItem;
  textModels: readonly AdminModelItem[];
  providerCopy: ReturnType<typeof registryCopy>;
  onEditProvider: () => void;
  onDeleteProvider: () => void;
  onAddTextModel: () => void;
  onAddRetrievalModel: () => void;
  onEditTextModel: (item: AdminModelItem) => void;
  onDeleteRetrievalModel: (model: AdminProviderModelItem) => void;
}) {
  return (
    <Card data-testid="admin-model-provider-card">
      <CardHeader className="pb-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <CardTitle className="truncate">{provider.name}</CardTitle>
              {provider.api_key_configured ? (
                <Badge variant="outline">{providerCopy.keyConfigured}</Badge>
              ) : null}
              <Badge variant="secondary">
                {providerCopy.modelCount(provider.model_count)} ·{" "}
                {providerCopy.activeModelCount(provider.active_model_count)}
              </Badge>
            </div>
            <p className="text-muted-foreground mt-1 truncate font-mono text-xs">
              {provider.base_url}
            </p>
            <p className="text-muted-foreground mt-0.5 text-xs">
              {providerCopy.timeout(provider.request_timeout_seconds)}
            </p>
            {provider.endpoint_frozen ? (
              <p className="text-muted-foreground mt-1 text-xs leading-5">
                {providerCopy.endpointFrozen}
              </p>
            ) : null}
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-1.5">
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={onAddTextModel}
            >
              <PlusIcon aria-hidden className="size-4" />
              {providerCopy.addTextModel}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={onAddRetrievalModel}
            >
              <PlusIcon aria-hidden className="size-4" />
              {providerCopy.addModel}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={onEditProvider}
            >
              <PencilIcon aria-hidden className="size-4" />
              {providerCopy.editProvider}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="text-destructive"
              onClick={onDeleteProvider}
            >
              <Trash2Icon aria-hidden className="size-4" />
              {providerCopy.deleteProvider}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <section>
          <h3 className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
            {providerCopy.textModelsTitle}
          </h3>
          {textModels.length === 0 ? (
            <p className="text-muted-foreground mt-3 rounded-lg border border-dashed px-3 py-4 text-center text-xs">
              {providerCopy.textModelsEmpty}
            </p>
          ) : (
            <div className="mt-3 grid gap-4 xl:grid-cols-2">
              {textModels.map((item) => (
                <ModelCard
                  key={item.id}
                  accountId={accountId}
                  item={item}
                  onEdit={() => onEditTextModel(item)}
                />
              ))}
            </div>
          )}
        </section>
        <section>
          <h3 className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
            {providerCopy.retrievalModelsTitle}
          </h3>
          <ProviderModelList
            accountId={accountId}
            provider={provider}
            copy={providerCopy}
            onDeleteModel={onDeleteRetrievalModel}
          />
        </section>
      </CardContent>
    </Card>
  );
}
