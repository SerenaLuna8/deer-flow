"use client";

import { PencilIcon, PlusIcon, RefreshCwIcon, SearchIcon } from "lucide-react";
import { useMemo, useState } from "react";

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
  useAdminModelCatalog,
  useClearAdminModelApiKey,
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
  type ReplaceAdminModelInput,
} from "@/core/admin-settings/models";
import { consumeWriteOnlyInput } from "@/core/api/write-only-input";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Locale } from "@/core/i18n";
import { useI18n } from "@/core/i18n/hooks";

type EditorTarget = AdminModelItem | null;

const MODEL_SETTINGS_COPY = {
  "en-US": {
    pageTitle: "Model settings",
    sectionTitle: "System models",
    searchModels: "Search models",
    searchPlaceholder: "Search by name, provider, or model ID",
    filterStatus: "Filter models by status",
    allStatuses: "All statuses",
    active: "Active",
    suspended: "Suspended",
    enable: "Enable",
    disable: "Disable",
    refresh: "Refresh",
    addModel: "Add model",
    catalogLoadFailed: "The model catalog could not be loaded.",
    retry: "Retry",
    noMatches: "No matching models.",
    editModel: "Edit model",
    dialogDescription:
      "The model domain encrypts and stores the API Key. Leave it blank when editing to preserve the saved value; connection tests always require a temporary re-entry.",
    displayName: "Display name",
    provider: "Provider",
    providerModelId: "Provider model ID",
    maximumInputTokens: "Maximum input tokens",
    maximumInputTokensHint:
      "The model's maximum input context and the denominator for context-percentage summarization; this is not the maximum output token limit.",
    providerSettings: "Provider settings",
    advancedSettings: "Advanced settings",
    providerDefault: "Provider default",
    enabledValue: "Enabled",
    disabledValue: "Disabled",
    preservedSettings:
      "{count} structured advanced setting(s) will be preserved unchanged without displaying raw JSON.",
    incompatibleSettings:
      "This model contains settings that the current Provider form cannot safely edit: {keys}. Saving and connection testing are disabled.",
    unknownProvider:
      "This model uses an unknown historical Provider ({provider}). Saving and connection testing are disabled.",
    supportsThinking: "Thinking",
    supportsReasoningEffort: "Reasoning effort",
    supportsVision: "Vision",
    apiKey: "API Key",
    preserveKeyPlaceholder: "Leave blank to preserve the saved Key",
    inputKeyPlaceholder: "Enter API Key",
    apiKeyHint:
      "A connection test immediately clears this Key and never saves it. Re-enter it after testing to create a model or replace the saved Key; a blank edit preserves the existing Key.",
    testingConnection: "Testing…",
    testConnection: "Test connection",
    cancel: "Cancel",
    saving: "Saving…",
    save: "Save",
    invalidProviderSettings: "Provider settings are invalid.",
    invalidMaximumInputTokens:
      "Maximum input tokens must be a whole number from 1 to 2,000,000.",
    apiKeyRequired:
      "Enter an API Key before saving a new model for this provider.",
    invalidModelConfiguration: "Model configuration is invalid.",
    saveFailed: "The model could not be saved.",
    testKeyRequired:
      "Enter a temporary API Key for the connection test; the saved database value cannot be used.",
    testFailed: "Connection test failed.",
    connectionSucceeded:
      "Connection test succeeded. The test Key was cleared from the form; re-enter the API Key before saving.",
    connectionFailed:
      "Connection test failed. The test Key was cleared from the form; re-enter the API Key to retry or save.",
    connectionSucceededEdit:
      "Connection test succeeded. The test Key was cleared and not saved. Leave the field blank to preserve the saved Key, or re-enter a Key to replace it.",
    connectionFailedEdit:
      "Connection test failed. The test Key was cleared and not saved. Leave the field blank to preserve the saved Key, or re-enter a Key to retry or replace it.",
    default: "Default",
    configured: "Configured",
    notConfigured: "Not configured",
    secretRevision: "Secret revision",
    readiness: "Readiness",
    ready: "Ready",
    unready: "Not ready",
    configurationRevision: "Configuration revision",
    edit: "Edit",
    setDefault: "Set as default",
    clearApiKey: "Clear API Key",
    operationFailed: "Model operation failed.",
    clearDialogTitle: "Clear API Key?",
    clearDialogDescription:
      "Clearing the Key makes this model unavailable to new Runs. Runs already running or preparing receive no special handling.",
    clearing: "Clearing…",
    confirmClear: "Confirm clear",
  },
  "zh-CN": {
    pageTitle: "模型配置",
    sectionTitle: "系统模型",
    searchModels: "搜索模型",
    searchPlaceholder: "搜索名称、Provider 或模型 ID",
    filterStatus: "筛选模型状态",
    allStatuses: "全部状态",
    active: "启用",
    suspended: "停用",
    enable: "启用",
    disable: "停用",
    refresh: "刷新",
    addModel: "新增模型",
    catalogLoadFailed: "模型目录读取失败。",
    retry: "重试",
    noMatches: "没有匹配的模型。",
    editModel: "编辑模型",
    dialogDescription:
      "API Key 直接由模型域加密保存。编辑时留空表示保留；连接测试必须临时重新输入。",
    displayName: "显示名称",
    provider: "Provider",
    providerModelId: "Provider 模型 ID",
    maximumInputTokens: "最大输入 Token",
    maximumInputTokensHint:
      "模型可接收的最大输入上下文，也是按上下文占比触发摘要时的分母；不是最大输出 Token。",
    providerSettings: "Provider 设置",
    advancedSettings: "高级设置",
    providerDefault: "Provider 默认",
    enabledValue: "启用",
    disabledValue: "禁用",
    preservedSettings: "将原样保留 {count} 项结构化高级设置，不展示原始 JSON。",
    incompatibleSettings:
      "当前模型包含此 Provider 表单无法安全编辑的设置：{keys}。已禁止保存和连接测试。",
    unknownProvider:
      "当前模型使用未知的历史 Provider（{provider}）。已禁止保存和连接测试。",
    supportsThinking: "思考模式",
    supportsReasoningEffort: "推理强度",
    supportsVision: "视觉输入",
    apiKey: "API Key",
    preserveKeyPlaceholder: "留空以保留已保存的 Key",
    inputKeyPlaceholder: "输入 API Key",
    apiKeyHint:
      "连接测试会立即清空这里的 Key，且不会保存。新增模型或替换已保存 Key 时，测试后必须重新输入；编辑时留空只会保留原 Key。",
    testingConnection: "正在测试…",
    testConnection: "测试连接",
    cancel: "取消",
    saving: "正在保存…",
    save: "保存",
    invalidProviderSettings: "Provider 设置无效。",
    invalidMaximumInputTokens:
      "最大输入 Token 必须是 1 到 2,000,000 之间的整数。",
    apiKeyRequired: "新增此 Provider 的模型必须输入 API Key 后才能保存。",
    invalidModelConfiguration: "模型配置无效。",
    saveFailed: "模型保存失败。",
    testKeyRequired:
      "连接测试必须临时重新输入 API Key，不能使用数据库中已保存的值。",
    testFailed: "连接测试失败。",
    connectionSucceeded:
      "连接测试成功。测试用 Key 已从表单清除；保存前必须重新输入 API Key。",
    connectionFailed:
      "连接测试失败。测试用 Key 已从表单清除；如需重试或保存，请重新输入 API Key。",
    connectionSucceededEdit:
      "连接测试成功。测试用 Key 已清空且不会保存；留空可保留原 Key，重新输入才会替换。",
    connectionFailedEdit:
      "连接测试失败。测试用 Key 已清空且不会保存；留空可保留原 Key，重新输入可再次测试或替换。",
    default: "默认",
    configured: "已配置",
    notConfigured: "未配置",
    secretRevision: "秘密 revision",
    readiness: "就绪状态",
    ready: "就绪",
    unready: "未就绪",
    configurationRevision: "配置 revision",
    edit: "编辑",
    setDefault: "设为默认",
    clearApiKey: "清除 API Key",
    operationFailed: "模型操作失败。",
    clearDialogTitle: "清除 API Key？",
    clearDialogDescription:
      "清除后此模型会变为未就绪，新的 Run 不能使用它。已经运行或正在准备的 Run 不做额外处理。",
    clearing: "正在清除…",
    confirmClear: "确认清除",
  },
} as const satisfies Record<Locale, Record<string, string>>;

export function adminModelSettingsCopy(locale: Locale) {
  return MODEL_SETTINGS_COPY[locale];
}

const BUILTIN_PROVIDER_SETTING_LABELS: Record<
  string,
  Record<Locale, string>
> = {
  base_url: { "en-US": "Base URL", "zh-CN": "Base URL" },
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
  output_version: { "en-US": "Output version", "zh-CN": "输出版本" },
  use_responses_api: {
    "en-US": "Use Responses API",
    "zh-CN": "使用 Responses API",
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

export function consumeAdminModelEditorSubmission(
  form: FormData,
  descriptor: AdminModelProviderAdapterDescriptor | undefined,
  providerSettingsDraft: AdminModelProviderSettingsDraft,
  apiKey: string,
  clearApiKey: () => void,
  locale: Locale = "zh-CN",
) {
  // Consume the write-only value before any local field validation can return.
  // The returned local is used only by the immediate imperative request.
  const submittedKey = consumeWriteOnlyInput(apiKey, clearApiKey);
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
    common: {
      display_name: formString(form, "display_name").trim(),
      provider_adapter:
        descriptor?.id ?? providerSettingsDraft.provider_adapter,
      provider_model: formString(form, "provider_model").trim(),
      max_input_tokens: maxInputTokens,
      settings,
      supports_thinking: form.get("supports_thinking") === "on",
      supports_reasoning_effort: form.get("supports_reasoning_effort") === "on",
      supports_vision: form.get("supports_vision") === "on",
    },
    submittedKey,
  };
}

export function isAdminModelEditorSaveDisabled({
  apiKey,
  creating,
  pending,
  providerRequiresApiKey,
  providerSettingsIncompatible,
  testPending,
}: {
  apiKey: string;
  creating: boolean;
  pending: boolean;
  providerRequiresApiKey: boolean;
  providerSettingsIncompatible: boolean;
  testPending: boolean;
}): boolean {
  return (
    pending ||
    testPending ||
    providerSettingsIncompatible ||
    (creating && providerRequiresApiKey && apiKey.trim() === "")
  );
}

export function adminModelConnectionTestResultMessage(
  status: "failed" | "succeeded",
  locale: Locale,
  mode: "create" | "edit" = "create",
): string {
  const copy = adminModelSettingsCopy(locale);
  if (mode === "edit") {
    return status === "succeeded"
      ? copy.connectionSucceededEdit
      : copy.connectionFailedEdit;
  }
  return status === "succeeded"
    ? copy.connectionSucceeded
    : copy.connectionFailed;
}

export function adminModelConnectionTestErrorState(
  error: unknown,
  locale: Locale,
  mode: "create" | "edit" = "create",
): { error: string; result: string } {
  return {
    error:
      error instanceof Error
        ? error.message
        : adminModelSettingsCopy(locale).testFailed,
    result: adminModelConnectionTestResultMessage("failed", locale, mode),
  };
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
  onOpenChange,
  onSaved,
}: {
  accountId: string;
  open: boolean;
  target: EditorTarget;
  descriptors: AdminModelProviderAdapterDescriptor[];
  onOpenChange: (open: boolean) => void;
  onSaved: () => Promise<unknown>;
}) {
  const { locale } = useI18n();
  const copy = adminModelSettingsCopy(locale);
  const initialProvider = target?.provider_adapter ?? descriptors[0]?.id ?? "";
  const initialDescriptor = descriptors.find(
    (item) => item.id === initialProvider,
  );
  const [provider, setProvider] = useState(initialProvider);
  const [providerSettingsDraft, setProviderSettingsDraft] = useState(() =>
    createAdminModelProviderSettingsDraft(
      initialDescriptor,
      target?.settings ?? {},
      initialProvider,
    ),
  );
  const [apiKey, setApiKey] = useState("");
  const [pending, setPending] = useState(false);
  const [testPending, setTestPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const create = useCreateAdminModel(accountId);
  const replace = useReplaceAdminModel(accountId);
  const test = useTestAdminModelConnection(accountId);
  const selectedDescriptor = descriptors.find((item) => item.id === provider);
  const providerRequiresApiKey = selectedDescriptor?.api_key_required ?? false;
  const providerSettingsIncompatible =
    providerSettingsDraft.unknown_provider ||
    providerSettingsDraft.incompatible_keys.length > 0;

  function changeProvider(nextProvider: string) {
    const nextDescriptor = descriptors.find((item) => item.id === nextProvider);
    setProvider(nextProvider);
    setProviderSettingsDraft(
      createAdminModelProviderSettingsDraft(nextDescriptor, {}, nextProvider),
    );
    setApiKey("");
    setError(null);
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
    if (!target && providerRequiresApiKey && apiKey.trim() === "") {
      setError(copy.apiKeyRequired);
      return;
    }
    let submission: ReturnType<typeof consumeAdminModelEditorSubmission>;
    try {
      submission = consumeAdminModelEditorSubmission(
        new FormData(event.currentTarget),
        selectedDescriptor,
        providerSettingsDraft,
        apiKey,
        () => setApiKey(""),
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
    const { common, submittedKey } = submission;
    setPending(true);
    try {
      if (target) {
        await replace.execute({
          modelId: target.id,
          input: {
            ...common,
            api_key: submittedKey || null,
          } as ReplaceAdminModelInput,
        });
      } else {
        await create.execute({
          ...common,
          status: "active",
          api_key: submittedKey || null,
        } as CreateAdminModelInput);
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
    if (apiKey === "") {
      setError(copy.testKeyRequired);
      return;
    }
    let submission: ReturnType<typeof consumeAdminModelEditorSubmission>;
    try {
      submission = consumeAdminModelEditorSubmission(
        new FormData(form),
        selectedDescriptor,
        providerSettingsDraft,
        apiKey,
        () => setApiKey(""),
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
    const { common, submittedKey } = submission;
    setTestPending(true);
    try {
      const result = await test.execute({
        provider_adapter: common.provider_adapter,
        provider_model: common.provider_model,
        max_input_tokens: common.max_input_tokens,
        settings: common.settings,
        supports_vision: common.supports_vision,
        api_key: submittedKey,
      });
      setTestResult(
        adminModelConnectionTestResultMessage(
          result.status,
          locale,
          target ? "edit" : "create",
        ),
      );
    } catch (caught) {
      const failure = adminModelConnectionTestErrorState(
        caught,
        locale,
        target ? "edit" : "create",
      );
      setError(failure.error);
      setTestResult(failure.result);
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
              {copy.provider}
              <select
                className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                aria-label={copy.provider}
                value={provider}
                onChange={(event) => changeProvider(event.target.value)}
              >
                {!selectedDescriptor && provider ? (
                  <option value={provider}>{provider}</option>
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
              key={provider}
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
                ? copy.unknownProvider.replace("{provider}", provider)
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
          <label className="grid gap-2 text-sm">
            {copy.apiKey}
            <Input
              type="password"
              autoComplete="new-password"
              value={apiKey}
              required={!target && providerRequiresApiKey}
              aria-describedby="admin-model-api-key-hint"
              placeholder={
                target?.api_key_configured
                  ? copy.preserveKeyPlaceholder
                  : copy.inputKeyPlaceholder
              }
              onChange={(event) => setApiKey(event.target.value)}
            />
            <span
              id="admin-model-api-key-hint"
              className="text-muted-foreground text-xs leading-5"
            >
              {copy.apiKeyHint}
            </span>
          </label>
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
          <DialogFooter className="flex-wrap">
            <Button
              type="button"
              variant="outline"
              disabled={
                pending ||
                testPending ||
                apiKey === "" ||
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
                apiKey,
                creating: !target,
                pending,
                providerRequiresApiKey,
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
  const clear = useClearAdminModelApiKey(accountId);
  const [confirmClear, setConfirmClear] = useState(false);
  const error = status.error ?? makeDefault.error ?? clear.error;
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
            <dt className="text-muted-foreground text-xs">API Key</dt>
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
          {item.api_key_configured ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setConfirmClear(true)}
            >
              {copy.clearApiKey}
            </Button>
          ) : null}
        </div>
        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {error instanceof Error ? error.message : copy.operationFailed}
          </p>
        ) : null}
      </CardContent>
      <Dialog open={confirmClear} onOpenChange={setConfirmClear}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{copy.clearDialogTitle}</DialogTitle>
            <DialogDescription>{copy.clearDialogDescription}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setConfirmClear(false)}
            >
              {copy.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={clear.isPending}
              onClick={() =>
                clear.mutate(item.id, {
                  onSuccess: () => setConfirmClear(false),
                })
              }
            >
              {clear.isPending ? copy.clearing : copy.confirmClear}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

export function AdminModelSettingsPage() {
  const { user } = useAuth();
  const { locale } = useI18n();
  const copy = adminModelSettingsCopy(locale);
  const accountId = user?.id ?? "default";
  const catalog = useAdminModelCatalog(accountId);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<"all" | "active" | "suspended">("all");
  const [editorOpen, setEditorOpen] = useState(false);
  const [target, setTarget] = useState<EditorTarget>(null);
  const items = useMemo(
    () =>
      selectAdminModelCatalogItems(catalog.data?.items ?? [], search, status),
    [catalog.data?.items, search, status],
  );
  if (!user) return null;
  return (
    <AdminPage>
      <AdminPageHeader title={copy.pageTitle} />
      <AdminSection title={copy.sectionTitle}>
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
          <Button
            type="button"
            variant="outline"
            disabled={catalog.isFetching}
            onClick={() => void catalog.refetch()}
          >
            <RefreshCwIcon aria-hidden className="size-4" />
            {copy.refresh}
          </Button>
          <Button
            type="button"
            onClick={() => {
              setTarget(null);
              setEditorOpen(true);
            }}
          >
            <PlusIcon aria-hidden className="size-4" />
            {copy.addModel}
          </Button>
        </div>
        {catalog.isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-36" />
            <Skeleton className="h-36" />
          </div>
        ) : catalog.error ? (
          <div className="space-y-3">
            <p role="alert" className="text-destructive text-sm">
              {catalog.error instanceof Error
                ? catalog.error.message
                : copy.catalogLoadFailed}
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={() => void catalog.refetch()}
            >
              {copy.retry}
            </Button>
          </div>
        ) : items.length === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed p-8 text-center text-sm">
            {copy.noMatches}
          </p>
        ) : (
          <div className="grid gap-4 xl:grid-cols-2">
            {items.map((item) => (
              <ModelCard
                key={item.id}
                accountId={accountId}
                item={item}
                onEdit={() => {
                  setTarget(item);
                  setEditorOpen(true);
                }}
              />
            ))}
          </div>
        )}
      </AdminSection>
      {editorOpen && catalog.data ? (
        <ModelEditorDialog
          key={`${target?.id ?? "new"}:${target?.revision ?? 0}`}
          accountId={accountId}
          open
          target={target}
          descriptors={catalog.data.provider_adapters}
          onOpenChange={setEditorOpen}
          onSaved={() => catalog.refetch()}
        />
      ) : null}
    </AdminPage>
  );
}
