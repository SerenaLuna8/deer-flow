"use client";

import {
  CircleCheckIcon,
  CircleIcon,
  MoreHorizontalIcon,
  PencilIcon,
  PlusIcon,
  PowerIcon,
  RefreshCwIcon,
  SearchIcon,
  StarIcon,
  Trash2Icon,
} from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import {
  ModelCreateDialog,
  ProviderEditorDialog,
  registryCopy,
  registryErrorText,
} from "@/components/admin/settings/admin-model-registry-page";
import { AdminPage, AdminPageHeader } from "@/components/admin/ui/admin-page";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useAdminModelProviders,
  useAdminProviderModels,
  useDeleteAdminModelProvider,
  useDeleteAdminProviderModel,
  useSetAdminProviderModelStatus,
  useTestAdminProviderModel,
} from "@/core/admin-settings/model-registry/hooks";
import type {
  AdminModelProviderItem,
  AdminProviderModelItem,
} from "@/core/admin-settings/model-registry/types";
import {
  useAdminModelCatalog,
  useCreateAdminModel,
  useDeleteAdminModel,
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
import { cn } from "@/lib/utils";

type EditorTarget = AdminModelItem | null;

type ModelDeleteTarget =
  | { kind: "text"; id: string; name: string }
  | { kind: "retrieval"; id: string; name: string };

const MODEL_TOAST_OPTIONS = {
  position: "top-center",
  richColors: true,
} as const;

const MODEL_SETTINGS_COPY = {
  "en-US": {
    pageTitle: "Model management",
    providersTitle: "Providers {count}",
    modelsTitle: "Models {count}",
    modelsDescription: "All models under this provider",
    modelList: "Models for the selected provider",
    modelName: "Model",
    modelType: "Type",
    modelStatus: "Status",
    modelCapabilities: "Capabilities",
    modelActions: "Actions",
    textModelType: "Text model",
    moreActions: "More actions",
    currentDefault: "Current default",
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
    noMatches: "No models match the current filter.",
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
    deleteModelTitle: "Delete model",
    deleteModelDescription:
      'Delete model "{name}"? It will be removed from the current model catalog and unavailable for new use. Historical records that already reference it will be retained. If a retrieval model is referenced by a knowledge base, deletion will be rejected.',
    deleteModelSucceeded: "Model deleted.",
    operationFailed: "Model operation failed.",
  },
  "zh-CN": {
    pageTitle: "模型管理",
    providersTitle: "供应商 {count}",
    modelsTitle: "模型 {count}",
    modelsDescription: "当前供应商下的全部模型",
    modelList: "当前供应商的模型",
    modelName: "模型",
    modelType: "类型",
    modelStatus: "状态",
    modelCapabilities: "能力",
    modelActions: "操作",
    textModelType: "文本模型",
    moreActions: "更多操作",
    currentDefault: "当前默认",
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
    noMatches: "当前筛选条件下没有匹配的模型。",
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
    deleteModelTitle: "删除模型",
    deleteModelDescription:
      "确定删除模型「{name}」？该模型会从当前模型目录中移除，不能再用于新的运行或配置。已经引用它的历史记录会被保留。检索模型正被知识库引用时，删除会被拒绝。",
    deleteModelSucceeded: "模型已删除。",
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

export function selectAdminProviderModelItems(
  items: readonly AdminProviderModelItem[],
  query: string,
  status: "all" | "active" | "suspended",
): AdminProviderModelItem[] {
  const normalized = query.trim().toLocaleLowerCase();
  return items.filter(
    (item) =>
      (status === "all" ||
        (status === "active"
          ? item.status === "active"
          : item.status === "disabled")) &&
      (!normalized ||
        item.model_name.toLocaleLowerCase().includes(normalized) ||
        item.model_type.toLocaleLowerCase().includes(normalized)),
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
    <>
      {directFields.map(renderField)}
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
    </>
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
  const [testResult, setTestResult] = useState<{
    status: "succeeded" | "failed";
    message: string;
  } | null>(null);
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
      setTestResult({
        status: result.status,
        message: adminModelConnectionTestResultMessage(result.status, locale),
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : copy.testFailed);
      setTestResult({
        status: "failed",
        message: adminModelConnectionTestResultMessage("failed", locale),
      });
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
          <DialogDescription className="sr-only">
            {copy.dialogDescription}
          </DialogDescription>
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
            />
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
            <p
              role="status"
              className={cn(
                "text-sm",
                testResult.status === "succeeded"
                  ? "text-success"
                  : "text-destructive",
              )}
            >
              {testResult.message}
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

const MODEL_ROW_LAYOUT =
  "grid min-w-0 items-center gap-2 xl:grid-cols-[minmax(12rem,2fr)_5.5rem_5.5rem_minmax(12rem,1.5fr)_15rem]";

function TextModelCapabilities({
  item,
  copy,
}: {
  item: AdminModelItem;
  copy: ReturnType<typeof adminModelSettingsCopy>;
}) {
  const capabilities = [
    item.supports_thinking ? (
      <Badge
        key="thinking"
        variant="outline"
        className="rounded-md border-violet-200 bg-violet-50 px-2 py-1 text-[11px] font-medium text-violet-700 dark:border-violet-400/30 dark:bg-violet-500/10 dark:text-violet-300"
      >
        <CircleCheckIcon aria-hidden />
        {copy.supportsThinking}
      </Badge>
    ) : null,
    item.supports_reasoning_effort ? (
      <Badge
        key="reasoning"
        variant="outline"
        className="rounded-md border-blue-200 bg-blue-50 px-2 py-1 text-[11px] font-medium text-blue-700 dark:border-blue-400/30 dark:bg-blue-500/10 dark:text-blue-300"
      >
        <CircleCheckIcon aria-hidden />
        {copy.supportsReasoningEffort}
      </Badge>
    ) : null,
    item.supports_vision ? (
      <Badge
        key="vision"
        variant="outline"
        className="rounded-md border-emerald-200 bg-emerald-50 px-2 py-1 text-[11px] font-medium text-emerald-700 dark:border-emerald-400/30 dark:bg-emerald-500/10 dark:text-emerald-300"
      >
        <CircleCheckIcon aria-hidden />
        {copy.supportsVision}
      </Badge>
    ) : null,
  ].filter(Boolean);

  if (capabilities.length === 0) {
    return <span className="text-muted-foreground text-xs">—</span>;
  }

  return (
    <div
      role="group"
      aria-label={copy.modelCapabilities}
      className="flex flex-wrap items-center gap-2"
    >
      {capabilities}
    </div>
  );
}

function TextModelRow({
  accountId,
  descriptor,
  item,
  onEdit,
  onDelete,
}: {
  accountId: string;
  descriptor: AdminModelProviderAdapterDescriptor | undefined;
  item: AdminModelItem;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const { locale } = useI18n();
  const copy = adminModelSettingsCopy(locale);
  const providerCopy = registryCopy(locale);
  const test = useTestAdminModelConnection(accountId);
  const status = useSetAdminModelStatus(accountId);
  const makeDefault = useSetAdminModelDefault(accountId);
  const error = status.error ?? makeDefault.error;
  const testInput = useMemo(() => {
    if (!descriptor) return null;
    const draft = createAdminModelProviderSettingsDraft(
      descriptor,
      item.settings,
      item.provider_adapter,
    );
    if (draft.unknown_provider || draft.incompatible_keys.length > 0) {
      return null;
    }
    try {
      return {
        provider_id: item.provider_id,
        provider_adapter: item.provider_adapter,
        provider_model: item.provider_model,
        max_input_tokens: item.max_input_tokens,
        settings: serializeAdminModelProviderSettingsDraft(descriptor, draft),
        supports_vision: item.supports_vision,
      };
    } catch {
      return null;
    }
  }, [descriptor, item]);

  const runConnectionTest = () => {
    if (!testInput) return;
    void test.execute(testInput).then(
      (result) => {
        const message = adminModelConnectionTestResultMessage(
          result.status,
          locale,
        );
        if (result.status === "succeeded") {
          toast.success(message, MODEL_TOAST_OPTIONS);
        } else {
          toast.error(message, MODEL_TOAST_OPTIONS);
        }
      },
      (caught: unknown) =>
        toast.error(
          caught instanceof Error ? caught.message : copy.testFailed,
          MODEL_TOAST_OPTIONS,
        ),
    );
  };

  return (
    <li
      data-model-kind="text"
      className="border-border/70 border-b px-4 py-3 last:border-b-0 sm:px-5"
    >
      <div className={MODEL_ROW_LAYOUT}>
        <div
          role="group"
          aria-label={`${copy.modelName}: ${item.display_name}`}
          className="flex min-w-0 items-center gap-2"
        >
          <span className="truncate text-sm font-medium">
            {item.display_name}
          </span>
          {item.is_default ? (
            <Badge
              variant="outline"
              className="border-selection/30 bg-selection-subtle text-selection rounded-md px-1.5 py-0.5 text-[11px]"
            >
              {copy.default}
            </Badge>
          ) : null}
        </div>
        <div
          role="group"
          aria-label={`${copy.modelType}: ${copy.textModelType}`}
        >
          <Badge
            variant="outline"
            className="rounded-md px-2 py-0.5 text-[11px] font-normal"
          >
            {copy.textModelType}
          </Badge>
        </div>
        <div
          role="group"
          aria-label={`${copy.modelStatus}: ${item.status === "active" ? copy.active : copy.suspended}`}
          className="flex items-center gap-1.5 text-xs"
        >
          <CircleIcon
            aria-hidden
            className={cn(
              "size-2 fill-current",
              item.status === "active"
                ? "text-success"
                : "text-muted-foreground",
            )}
          />
          {item.status === "active" ? copy.active : copy.suspended}
        </div>
        <TextModelCapabilities item={item} copy={copy} />
        <div
          role="group"
          aria-label={copy.modelActions}
          className="flex flex-wrap items-center justify-start gap-1.5 xl:flex-nowrap xl:justify-end"
        >
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
            <PowerIcon aria-hidden className="size-3.5" />
            {item.status === "active" ? copy.disable : copy.enable}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={
              item.is_default ||
              makeDefault.isPending ||
              item.status !== "active" ||
              item.secret_readiness !== "ready"
            }
            onClick={() => makeDefault.mutate({ modelId: item.id, input: {} })}
          >
            <StarIcon aria-hidden className="size-3.5" />
            {item.is_default ? copy.default : copy.setDefault}
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                size="icon-sm"
                variant="outline"
                aria-label={copy.moreActions}
              >
                <MoreHorizontalIcon aria-hidden className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                disabled={test.isPending || testInput === null}
                onSelect={runConnectionTest}
              >
                <CircleCheckIcon aria-hidden />
                {test.isPending ? providerCopy.testing : providerCopy.test}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={onEdit}>
                <PencilIcon aria-hidden />
                {copy.edit}
              </DropdownMenuItem>
              <DropdownMenuItem
                className="text-destructive focus:text-destructive"
                onSelect={onDelete}
              >
                <Trash2Icon aria-hidden />
                {providerCopy.delete}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
      {error ? (
        <p role="alert" className="text-destructive mt-2 text-xs">
          {error instanceof Error ? error.message : copy.operationFailed}
        </p>
      ) : null}
    </li>
  );
}

function RetrievalModelRow({
  model,
  copy,
  settingsCopy,
  testing,
  statusPending,
  onTest,
  onToggleStatus,
  onDelete,
}: {
  model: AdminProviderModelItem;
  copy: ReturnType<typeof registryCopy>;
  settingsCopy: ReturnType<typeof adminModelSettingsCopy>;
  testing: boolean;
  statusPending: boolean;
  onTest: () => void;
  onToggleStatus: () => void;
  onDelete: () => void;
}) {
  const active = model.status === "active";

  return (
    <li
      data-model-kind={model.model_type}
      className="border-border/70 border-b px-4 py-3 last:border-b-0 sm:px-5"
    >
      <div className={MODEL_ROW_LAYOUT}>
        <span
          aria-label={`${settingsCopy.modelName}: ${model.model_name}`}
          className="truncate text-sm font-medium"
        >
          {model.model_name}
        </span>
        <div
          role="group"
          aria-label={`${settingsCopy.modelType}: ${
            model.model_type === "embedding"
              ? copy.typeEmbedding
              : copy.typeRerank
          }`}
        >
          <Badge
            variant="outline"
            className="rounded-md px-2 py-0.5 text-[11px] font-normal"
          >
            {model.model_type === "embedding"
              ? copy.typeEmbedding
              : copy.typeRerank}
          </Badge>
        </div>
        <div
          role="group"
          aria-label={`${settingsCopy.modelStatus}: ${active ? copy.active : copy.disabled}`}
          className="flex flex-wrap items-center gap-1.5 text-xs"
        >
          <CircleIcon
            aria-hidden
            className={cn(
              "size-2 fill-current",
              active ? "text-success" : "text-muted-foreground",
            )}
          />
          <span>{active ? copy.active : copy.disabled}</span>
          {model.in_use ? (
            <Badge variant="outline" className="rounded-md px-1.5 text-[10px]">
              {copy.inUse}
            </Badge>
          ) : null}
        </div>
        <span
          aria-label={`${settingsCopy.modelCapabilities}: —`}
          className="text-muted-foreground text-xs"
        >
          —
        </span>
        <div
          role="group"
          aria-label={settingsCopy.modelActions}
          className="flex flex-wrap items-center justify-start gap-1.5 xl:flex-nowrap xl:justify-end"
        >
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={statusPending || (active && model.in_use)}
            onClick={onToggleStatus}
          >
            <PowerIcon aria-hidden className="size-3.5" />
            {active ? copy.disable : copy.enable}
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                size="icon-sm"
                variant="outline"
                aria-label={settingsCopy.moreActions}
              >
                <MoreHorizontalIcon aria-hidden className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem disabled={testing} onSelect={onTest}>
                <CircleCheckIcon aria-hidden />
                {testing ? copy.testing : copy.test}
              </DropdownMenuItem>
              <DropdownMenuItem
                className="text-destructive focus:text-destructive"
                disabled={model.in_use}
                onSelect={onDelete}
              >
                <Trash2Icon aria-hidden />
                {copy.delete}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </li>
  );
}

function ProviderSidebar({
  providers,
  selectedProviderId,
  copy,
  providerCopy,
  onSelect,
}: {
  providers: readonly AdminModelProviderItem[];
  selectedProviderId: string;
  copy: ReturnType<typeof adminModelSettingsCopy>;
  providerCopy: ReturnType<typeof registryCopy>;
  onSelect: (providerId: string) => void;
}) {
  return (
    <aside
      aria-label={copy.providersTitle.replace(
        "{count}",
        String(providers.length),
      )}
      className="border-border/70 border-b p-4 lg:border-r lg:border-b-0"
    >
      <h2 className="text-xs font-semibold">
        {copy.providersTitle.replace("{count}", String(providers.length))}
      </h2>
      <div
        className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-1"
        data-testid="admin-model-provider-cards"
      >
        {providers.map((provider) => {
          const selected = provider.id === selectedProviderId;
          return (
            <button
              key={provider.id}
              type="button"
              aria-pressed={selected}
              data-testid="admin-model-provider-selector"
              className={cn(
                "hover:bg-accent/60 focus-visible:border-ring focus-visible:ring-ring/50 w-full rounded-lg border border-l-2 border-transparent px-3 py-3 text-left transition-colors outline-none focus-visible:ring-[3px]",
                selected &&
                  "border-l-selection bg-selection-subtle hover:bg-selection-subtle",
              )}
              onClick={() => onSelect(provider.id)}
            >
              <span className="flex min-w-0 items-center justify-between gap-3">
                <span className="truncate text-sm font-medium">
                  {provider.name}
                </span>
                <span className="text-muted-foreground flex shrink-0 items-center gap-1.5 text-xs">
                  <CircleIcon
                    aria-hidden
                    className="text-success size-2 fill-current"
                  />
                  {provider.active_model_count} / {provider.model_count}{" "}
                  {copy.active}
                </span>
              </span>
              <span className="text-muted-foreground mt-1.5 block truncate font-mono text-[11px]">
                {provider.base_url}
              </span>
              <span
                className={cn(
                  "mt-1.5 flex items-center gap-1.5 text-xs",
                  provider.api_key_configured
                    ? "text-muted-foreground"
                    : "text-destructive",
                )}
              >
                <CircleIcon
                  aria-hidden
                  className={cn(
                    "size-2 fill-current",
                    provider.api_key_configured
                      ? "text-success"
                      : "text-destructive",
                  )}
                />
                {provider.api_key_configured
                  ? providerCopy.keyConfigured
                  : copy.notConfigured}
              </span>
            </button>
          );
        })}
      </div>
    </aside>
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
  const deleteTextModel = useDeleteAdminModel(accountId);
  const deleteRetrievalModel = useDeleteAdminProviderModel(accountId);
  const setRetrievalModelStatus = useSetAdminProviderModelStatus(accountId);
  const testRetrievalModel = useTestAdminProviderModel(accountId);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<"all" | "active" | "suspended">("all");
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(
    null,
  );
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
  const [modelToDelete, setModelToDelete] = useState<ModelDeleteTarget | null>(
    null,
  );
  const modelListRef = useRef<HTMLUListElement>(null);
  const modelDeleteInFlightRef = useRef(false);
  const [testingIds, setTestingIds] = useState<ReadonlySet<string>>(new Set());

  const providerItems = providers.data ?? [];
  const selectedProvider =
    providerItems.find((provider) => provider.id === selectedProviderId) ??
    providerItems[0] ??
    null;
  const retrievalModels = useAdminProviderModels(
    accountId,
    selectedProvider?.id ?? "",
    Boolean(user && selectedProvider),
  );
  const textModels = useMemo(() => {
    if (!selectedProvider) return [];
    return selectAdminModelCatalogItems(
      (catalog.data?.items ?? []).filter(
        (item) => item.provider_id === selectedProvider.id,
      ),
      search,
      status,
    );
  }, [catalog.data?.items, search, selectedProvider, status]);
  const providerModels = useMemo(
    () =>
      selectAdminProviderModelItems(retrievalModels.data ?? [], search, status),
    [retrievalModels.data, search, status],
  );
  if (!user) return null;

  const loading = catalog.isLoading || providers.isLoading;
  const loadError = catalog.error ?? providers.error;

  const refresh = () => {
    void catalog.refetch();
    void providers.refetch();
    if (selectedProvider) void retrievalModels.refetch();
  };

  const runRetrievalModelTest = (modelId: string) => {
    setTestingIds((current) => new Set(current).add(modelId));
    void testRetrievalModel
      .mutateAsync(modelId)
      .then(
        (result) =>
          result.ok
            ? toast.success(result.message, MODEL_TOAST_OPTIONS)
            : toast.error(result.message, MODEL_TOAST_OPTIONS),
        (error: unknown) =>
          toast.error(
            registryErrorText(error, providerCopy),
            MODEL_TOAST_OPTIONS,
          ),
      )
      .finally(() =>
        setTestingIds((current) => {
          const next = new Set(current);
          next.delete(modelId);
          return next;
        }),
      );
  };

  const modelDeletePending =
    modelToDelete?.kind === "text"
      ? deleteTextModel.isPending
      : modelToDelete?.kind === "retrieval"
        ? deleteRetrievalModel.isPending
        : false;
  const modelDeleteError =
    modelToDelete?.kind === "text"
      ? deleteTextModel.error
      : modelToDelete?.kind === "retrieval"
        ? deleteRetrievalModel.error
        : null;
  const modelDeleteErrorMessage = modelDeleteError
    ? modelToDelete?.kind === "retrieval"
      ? registryErrorText(modelDeleteError, providerCopy)
      : modelDeleteError instanceof Error
        ? modelDeleteError.message
        : copy.operationFailed
    : null;

  return (
    <AdminPage className="lg:flex lg:min-h-[calc(100dvh-3.5rem)] lg:flex-col">
      <AdminPageHeader
        className="border-b-0 pb-0 sm:items-center [&_h1]:text-xl"
        title={copy.pageTitle}
        actions={
          <>
            <Button
              type="button"
              variant="outline"
              disabled={
                catalog.isFetching ||
                providers.isFetching ||
                retrievalModels.isFetching
              }
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
      {loading ? (
        <Skeleton className="h-[36rem] rounded-lg" />
      ) : loadError ? (
        <section className="border-border/80 bg-card space-y-3 rounded-lg border p-4">
          <p role="alert" className="text-destructive text-sm">
            {loadError instanceof Error
              ? loadError.message
              : copy.catalogLoadFailed}
          </p>
          <Button type="button" variant="outline" onClick={refresh}>
            {copy.retry}
          </Button>
        </section>
      ) : providerItems.length === 0 || !selectedProvider ? (
        <p className="text-muted-foreground rounded-lg border border-dashed p-8 text-center text-sm">
          {providerCopy.empty}
        </p>
      ) : (
        <section
          className="border-border/80 bg-card grid overflow-hidden rounded-lg border lg:flex-1 lg:grid-cols-[17rem_minmax(0,1fr)]"
          data-testid="admin-model-provider-workbench"
        >
          <ProviderSidebar
            providers={providerItems}
            selectedProviderId={selectedProvider.id}
            copy={copy}
            providerCopy={providerCopy}
            onSelect={setSelectedProviderId}
          />
          <section
            className="min-w-0"
            aria-labelledby="selected-model-provider-title"
            data-testid="admin-model-provider-card"
          >
            <header className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6">
              <h2
                id="selected-model-provider-title"
                className="truncate text-lg font-semibold tracking-tight"
              >
                {selectedProvider.name}
              </h2>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    setProviderEditor({ open: true, target: selectedProvider })
                  }
                >
                  <PencilIcon aria-hidden className="size-3.5" />
                  {providerCopy.editProvider}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="text-destructive"
                  onClick={() => {
                    deleteProvider.reset();
                    setProviderToDelete(selectedProvider);
                  }}
                >
                  <Trash2Icon aria-hidden className="size-3.5" />
                  {providerCopy.deleteProvider}
                </Button>
              </div>
            </header>
            <div className="px-4 pb-4 sm:px-6">
              <div>
                <h3 className="text-sm font-semibold">
                  {copy.modelsTitle.replace(
                    "{count}",
                    String(selectedProvider.model_count),
                  )}
                </h3>
                <p className="text-muted-foreground mt-1 text-xs">
                  {copy.modelsDescription}
                </p>
              </div>
              <div className="mt-3 flex flex-col gap-3 xl:flex-row xl:items-center">
                <div className="flex min-w-0 flex-1 flex-col gap-3 sm:flex-row">
                  <label className="relative min-w-0 flex-1">
                    <SearchIcon
                      aria-hidden
                      className="text-muted-foreground absolute top-2 left-3 size-4"
                    />
                    <Input
                      className="h-8 pl-9 text-xs"
                      value={search}
                      aria-label={copy.searchModels}
                      placeholder={copy.searchPlaceholder}
                      onChange={(event) => setSearch(event.target.value)}
                    />
                  </label>
                  <select
                    className="border-input bg-background h-8 rounded-md border px-3 text-xs"
                    aria-label={copy.filterStatus}
                    value={status}
                    onChange={(event) =>
                      setStatus(event.target.value as typeof status)
                    }
                  >
                    <option value="all">{copy.allStatuses}</option>
                    <option value="active">{copy.active}</option>
                    <option value="suspended">{copy.suspended}</option>
                  </select>
                </div>
                <div className="flex flex-wrap items-center gap-2 xl:justify-end">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      setEditor({
                        open: true,
                        target: null,
                        initialProviderId: selectedProvider.id,
                      })
                    }
                  >
                    <PlusIcon aria-hidden className="size-3.5" />
                    {providerCopy.addTextModel}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setModelCreateProvider(selectedProvider)}
                  >
                    <PlusIcon aria-hidden className="size-3.5" />
                    {providerCopy.addModel}
                  </Button>
                </div>
              </div>
            </div>
            <div
              aria-hidden
              className={cn(
                MODEL_ROW_LAYOUT,
                "border-border/70 hidden border-y px-5 py-3 text-left text-xs font-semibold xl:grid",
              )}
            >
              <span>{copy.modelName}</span>
              <span>{copy.modelType}</span>
              <span>{copy.modelStatus}</span>
              <span>{copy.modelCapabilities}</span>
              <span>{copy.modelActions}</span>
            </div>
            <ul
              ref={modelListRef}
              aria-label={copy.modelList}
              tabIndex={-1}
              className="focus:outline-none"
              data-testid="admin-provider-model-list"
            >
              {textModels.map((item) => (
                <TextModelRow
                  key={`text:${item.id}`}
                  accountId={accountId}
                  descriptor={catalog.data?.provider_adapters.find(
                    (descriptor) => descriptor.id === item.provider_adapter,
                  )}
                  item={item}
                  onEdit={() =>
                    setEditor({
                      open: true,
                      target: item,
                      initialProviderId: null,
                    })
                  }
                  onDelete={() => {
                    deleteTextModel.reset();
                    deleteRetrievalModel.reset();
                    setModelToDelete({
                      kind: "text",
                      id: item.id,
                      name: item.display_name,
                    });
                  }}
                />
              ))}
              {providerModels.map((model) => (
                <RetrievalModelRow
                  key={`retrieval:${model.id}`}
                  model={model}
                  copy={providerCopy}
                  settingsCopy={copy}
                  testing={testingIds.has(model.id)}
                  statusPending={setRetrievalModelStatus.isPending}
                  onTest={() => runRetrievalModelTest(model.id)}
                  onToggleStatus={() =>
                    setRetrievalModelStatus.mutate(
                      {
                        modelId: model.id,
                        status:
                          model.status === "active" ? "disabled" : "active",
                      },
                      {
                        onError: (error: unknown) =>
                          toast.error(registryErrorText(error, providerCopy)),
                      },
                    )
                  }
                  onDelete={() => {
                    deleteTextModel.reset();
                    deleteRetrievalModel.reset();
                    setModelToDelete({
                      kind: "retrieval",
                      id: model.id,
                      name: model.model_name,
                    });
                  }}
                />
              ))}
              {retrievalModels.isLoading ? (
                <li className="border-border/70 border-b px-5 py-3">
                  <Skeleton className="h-8" />
                </li>
              ) : null}
              {retrievalModels.error ? (
                <li className="border-border/70 border-b px-5 py-3">
                  <p role="alert" className="text-destructive text-xs">
                    {providerCopy.modelsLoadFailed}
                  </p>
                </li>
              ) : null}
              {!retrievalModels.isLoading &&
              !retrievalModels.error &&
              textModels.length === 0 &&
              providerModels.length === 0 ? (
                <li className="text-muted-foreground px-5 py-10 text-center text-xs">
                  {copy.noMatches}
                </li>
              ) : null}
            </ul>
          </section>
        </section>
      )}
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
          if (!open && !modelDeleteInFlightRef.current) {
            setModelToDelete(null);
          }
        }}
      >
        <DialogContent
          onEscapeKeyDown={(event) => {
            if (modelDeleteInFlightRef.current) event.preventDefault();
          }}
          onInteractOutside={(event) => {
            if (modelDeleteInFlightRef.current) event.preventDefault();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            requestAnimationFrame(() => modelListRef.current?.focus());
          }}
        >
          <DialogHeader>
            <DialogTitle>{copy.deleteModelTitle}</DialogTitle>
            <DialogDescription>
              {copy.deleteModelDescription.replace(
                "{name}",
                modelToDelete?.name ?? "",
              )}
            </DialogDescription>
          </DialogHeader>
          {modelDeleteErrorMessage ? (
            <p role="alert" className="text-destructive text-sm">
              {modelDeleteErrorMessage}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={modelDeletePending}
              onClick={() => {
                if (!modelDeleteInFlightRef.current) setModelToDelete(null);
              }}
            >
              {providerCopy.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={modelDeletePending}
              onClick={() => {
                if (modelToDelete === null) return;
                const target = modelToDelete;
                modelDeleteInFlightRef.current = true;
                const options = {
                  onSuccess: () => {
                    setModelToDelete((current) =>
                      current?.kind === target.kind && current.id === target.id
                        ? null
                        : current,
                    );
                    toast.success(
                      copy.deleteModelSucceeded,
                      MODEL_TOAST_OPTIONS,
                    );
                  },
                  onSettled: () => {
                    modelDeleteInFlightRef.current = false;
                  },
                };
                if (target.kind === "text") {
                  deleteTextModel.mutate(target.id, options);
                } else {
                  deleteRetrievalModel.mutate(target.id, options);
                }
              }}
            >
              {modelDeletePending ? providerCopy.deleting : providerCopy.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </AdminPage>
  );
}
