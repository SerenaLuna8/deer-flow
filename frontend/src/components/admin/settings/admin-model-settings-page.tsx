"use client";

import {
  ActivityIcon,
  BotIcon,
  CheckCircle2Icon,
  ChevronDownIcon,
  CirclePauseIcon,
  CirclePlayIcon,
  DatabaseIcon,
  Layers3Icon,
  PencilIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
  ShieldCheckIcon,
  StarIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
import { Textarea } from "@/components/ui/textarea";
import {
  AdminModelSettingsApiError,
  adminModelSettingsSchemaForProvider,
  createAdminModelInputSchema,
  useAdminModelCatalog,
  useCreateAdminModel,
  useReplaceAdminModel,
  useSetAdminModelDefault,
  useSetAdminModelStatus,
  useTestAdminModelConnection,
  type AdminModelCatalog,
  type AdminModelConnectionTestResponse,
  type AdminModelItem,
  type AdminModelProviderAdapterDescriptor,
  type AdminModelProviderSettingField,
  type AdminModelSettingValue,
  type CreateAdminModelInput,
  type TestAdminModelConnectionInput,
} from "@/core/admin-settings/models";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Locale, Translations } from "@/core/i18n";
import { useI18n } from "@/core/i18n/hooks";
import { useAdminAssets, type AdminCredentialList } from "@/core/shared-assets";
import { cn } from "@/lib/utils";

type AdminModelTranslations = Translations["adminModelSettings"];
export type AdminModelFormValidationMessages =
  AdminModelTranslations["validation"];

const DEFAULT_FORM_VALIDATION_MESSAGES: AdminModelFormValidationMessages = {
  invalidNumber: (label) => `${label}格式不正确`,
  temperature: "Temperature",
  maxTokens: "最大 Token",
  requestTimeout: "请求超时",
  maxRetries: "重试次数",
  advancedJsonInvalid: "高级 JSON 格式不正确",
  advancedJsonObject: "高级 JSON 必须是对象",
  advancedJsonUnsafe: "高级 JSON 只能包含支持的安全字段和精确类型",
  invalidForm: "请检查必填项、显示名称、模型 ID 和 Credential 绑定",
  invalidConfiguration: "模型配置格式不正确",
};

export interface AdminModelCredentialOption {
  id: string;
  display_name: string;
  credential_type: "model_api_key";
  current_version_id: string | null;
  version: number;
  status: "active" | "revoked";
}

export type AdminModelCredentialLoadStatus = "loading" | "error" | "ready";

export interface AdminModelCredentialChoice extends AdminModelCredentialOption {
  credential_version_id: string;
  historical: boolean;
  unavailable: boolean;
}

export interface AdminModelCredentialEditorState {
  credentialId: string;
  credentialVersionId: string;
  choices: AdminModelCredentialChoice[];
}

export function selectAdminModelCredentialOptions(
  credentials: AdminCredentialList | undefined,
): AdminModelCredentialOption[] {
  if (!credentials) return [];
  return credentials.items.flatMap((credential) =>
    credential.scope === "system" &&
    credential.project_id === null &&
    credential.credential_type === "model_api_key"
      ? [
          {
            id: credential.id,
            display_name: credential.display_name,
            credential_type: credential.credential_type,
            current_version_id: credential.current_version_id,
            version: credential.version,
            status: credential.status,
          },
        ]
      : [],
  );
}

export function buildAdminModelCredentialEditorState(
  model: AdminModelItem | null,
  credentials: AdminModelCredentialOption[],
): AdminModelCredentialEditorState {
  const credentialId = model?.credential_id ?? "";
  const credentialVersionId = model?.credential_version_id ?? "";
  const usable = credentials.filter(
    (credential) =>
      credential.status === "active" && credential.current_version_id !== null,
  );
  const current = credentialId
    ? credentials.find((credential) => credential.id === credentialId)
    : undefined;
  const choices: AdminModelCredentialChoice[] = usable.flatMap((credential) => {
    const currentVersionId = credential.current_version_id;
    if (currentVersionId === null) return [];
    const historicalChoice =
      credential.id === credentialId &&
      credentialVersionId !== "" &&
      currentVersionId !== credentialVersionId
        ? [
            {
              ...credential,
              credential_version_id: credentialVersionId,
              historical: true,
              unavailable: false,
            },
          ]
        : [];
    return [
      ...historicalChoice,
      {
        ...credential,
        credential_version_id: currentVersionId,
        historical: false,
        unavailable: false,
      },
    ];
  });

  if (
    credentialId &&
    !choices.some(
      (choice) =>
        choice.id === credentialId &&
        choice.credential_version_id === credentialVersionId,
    )
  ) {
    choices.unshift(
      current
        ? {
            ...current,
            credential_version_id: credentialVersionId,
            historical: false,
            unavailable: true,
          }
        : {
            id: credentialId,
            display_name: "",
            credential_type: "model_api_key",
            current_version_id: credentialVersionId || null,
            version: 0,
            status: "revoked",
            credential_version_id: credentialVersionId,
            historical: false,
            unavailable: true,
          },
    );
  }

  return { credentialId, credentialVersionId, choices };
}

export function canSubmitAdminModelEditor(
  credentialStatus: AdminModelCredentialLoadStatus,
  pending: boolean,
  providerRequiresCredential = true,
  credentialBindingReady = true,
): boolean {
  return (
    !pending &&
    (!providerRequiresCredential ||
      (credentialStatus === "ready" && credentialBindingReady))
  );
}

export function canCloseAdminModelEditor(operationPending: boolean): boolean {
  return !operationPending;
}

export type AdminModelCatalogState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: AdminModelCatalog };

export type AdminModelStatusFilter = "active" | "all" | "suspended";

export function selectAdminModelCatalogItems(
  models: AdminModelItem[],
  search: string,
  status: AdminModelStatusFilter,
): AdminModelItem[] {
  const normalizedSearch = search.trim().toLocaleLowerCase();
  return models.filter((model) => {
    if (status !== "all" && model.status !== status) return false;
    if (normalizedSearch === "") return true;
    return [
      model.display_name,
      model.provider_adapter,
      model.provider_model,
      model.credential_env_key ?? "",
    ].some((value) => value.toLocaleLowerCase().includes(normalizedSearch));
  });
}

const MODEL_EDITOR_ERROR_ID = "admin-model-editor-error";
const MODEL_EDITOR_CREDENTIAL_STATUS_ID =
  "admin-model-editor-credential-status";
const MODEL_EDITOR_ENVIRONMENT_KEY_HINT_ID = "admin-model-environment-key-hint";
const MODEL_EDITOR_ADVANCED_SETTINGS_HINT_ID =
  "admin-model-advanced-settings-hint";

class AdminModelEditorFormError extends Error {
  readonly fieldId: string | null;

  constructor(message: string, fieldId: string | null) {
    super(message);
    this.name = "AdminModelEditorFormError";
    this.fieldId = fieldId;
  }
}

export function getAdminModelEditorErrorField(error: unknown): string | null {
  return error instanceof AdminModelEditorFormError ? error.fieldId : null;
}

export function adminModelEditorFieldErrorAttributes(
  fieldId: string,
  invalidFieldId: string | null,
  hintId?: string,
): {
  "aria-describedby"?: string;
  "aria-invalid"?: true;
} {
  const invalid = invalidFieldId === fieldId;
  const describedBy = [hintId, invalid ? MODEL_EDITOR_ERROR_ID : undefined]
    .filter((value): value is string => Boolean(value))
    .join(" ");
  return {
    ...(describedBy ? { "aria-describedby": describedBy } : {}),
    ...(invalid ? { "aria-invalid": true as const } : {}),
  };
}

function readFormString(formData: FormData, key: string): string {
  const value = formData.get(key);
  return typeof value === "string" ? value.trim() : "";
}

function readOptionalNumber(
  formData: FormData,
  key: string,
  label: string,
  invalidNumber: (label: string) => string,
  options: { integer?: boolean; min?: number; max?: number } = {},
): number | undefined {
  const raw = readFormString(formData, key);
  if (raw === "") return undefined;
  const value = Number(raw);
  if (
    !Number.isFinite(value) ||
    (options.integer === true && !Number.isInteger(value)) ||
    (options.min !== undefined && value < options.min) ||
    (options.max !== undefined && value > options.max)
  ) {
    throw new AdminModelEditorFormError(invalidNumber(label), key);
  }
  return value;
}

export function findAdminModelProviderAdapterDescriptor(
  providerAdapters: AdminModelProviderAdapterDescriptor[],
  adapter: string,
): AdminModelProviderAdapterDescriptor | undefined {
  return providerAdapters.find((item) => item.id === adapter);
}

export function selectAdminModelVisibleSettingFields(
  descriptor: AdminModelProviderAdapterDescriptor | undefined,
): AdminModelProviderSettingField[] {
  return descriptor?.setting_fields.filter((field) => !field.advanced) ?? [];
}

function providerAdapterLabel(
  adapter: string,
  labels: AdminModelTranslations["adapters"],
): string {
  switch (adapter) {
    case "openai":
      return "OpenAI";
    case "anthropic":
      return "Anthropic";
    case "deepseek":
      return "DeepSeek";
    case "patched_openai":
      return labels.patchedOpenAI;
    case "patched_deepseek":
      return labels.patchedDeepSeek;
    case "vllm":
      return "vLLM";
    default:
      return adapter;
  }
}

export function AdminModelProviderAdapterOptions({
  providerAdapters,
  value,
}: {
  providerAdapters: AdminModelProviderAdapterDescriptor[];
  value: string;
}) {
  const labels = useI18n().t.adminModelSettings;
  const selected = findAdminModelProviderAdapterDescriptor(
    providerAdapters,
    value,
  );
  return (
    <>
      {!selected && value ? (
        <option value={value} disabled>
          {providerAdapterLabel(value, labels.adapters)} ·{" "}
          {labels.editor.retiredProviderAdapter}
        </option>
      ) : null}
      {providerAdapters.map((adapter) => (
        <option key={adapter.id} value={adapter.id}>
          {providerAdapterLabel(adapter.id, labels.adapters)}
        </option>
      ))}
    </>
  );
}

function formatUpdatedAt(value: string, locale: Locale): string {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function parseAdvancedSettings(
  value: string,
  descriptor: AdminModelProviderAdapterDescriptor,
  messages: AdminModelFormValidationMessages,
): Record<string, AdminModelSettingValue> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value === "" ? "{}" : value);
  } catch {
    throw new AdminModelEditorFormError(
      messages.advancedJsonInvalid,
      "advanced_settings",
    );
  }
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new AdminModelEditorFormError(
      messages.advancedJsonObject,
      "advanced_settings",
    );
  }
  const safe =
    adminModelSettingsSchemaForProvider(descriptor).safeParse(parsed);
  const advancedFields = new Set(
    descriptor.setting_fields
      .filter((field) => field.advanced)
      .map((field) => field.name),
  );
  if (
    !safe.success ||
    Object.keys(parsed as Record<string, unknown>).some(
      (key) => !advancedFields.has(key),
    )
  ) {
    throw new AdminModelEditorFormError(
      messages.advancedJsonUnsafe,
      "advanced_settings",
    );
  }
  return safe.data;
}

function readProviderSettingFormValue(
  formData: FormData,
  field: AdminModelProviderSettingField,
  messages: AdminModelFormValidationMessages,
): AdminModelSettingValue | undefined {
  if (field.input_type === "json") return undefined;
  const raw = readFormString(formData, field.name);
  if (raw === "") return undefined;
  if (field.input_type === "boolean") {
    if (raw === "true") return true;
    if (raw === "false") return false;
    throw new AdminModelEditorFormError(
      messages.advancedJsonUnsafe,
      field.name,
    );
  }
  if (field.input_type === "integer" || field.input_type === "number") {
    return readOptionalNumber(
      formData,
      field.name,
      field.label,
      messages.invalidNumber,
      {
        integer: field.input_type === "integer",
        ...(field.minimum === null ? {} : { min: field.minimum }),
        ...(field.maximum === null ? {} : { max: field.maximum }),
      },
    );
  }
  return raw;
}

/**
 * Converts uncontrolled form fields into the only model payload allowed to
 * enter TanStack mutation state. The final strict schema is the cache safety
 * boundary: JSON with credential-like keys never leaves this function.
 */
export function parseAdminModelEditorForm(
  formData: FormData,
  providerDescriptor: AdminModelProviderAdapterDescriptor,
  messages: AdminModelFormValidationMessages = DEFAULT_FORM_VALIDATION_MESSAGES,
): CreateAdminModelInput {
  const providerAdapter = readFormString(formData, "provider_adapter");
  if (providerAdapter !== providerDescriptor.id) {
    throw new AdminModelEditorFormError(
      messages.invalidForm,
      "provider_adapter",
    );
  }
  const settings = parseAdvancedSettings(
    readFormString(formData, "advanced_settings"),
    providerDescriptor,
    messages,
  );
  for (const field of selectAdminModelVisibleSettingFields(
    providerDescriptor,
  )) {
    const value = readProviderSettingFormValue(formData, field, messages);
    if (value !== undefined) settings[field.name] = value;
  }
  const descriptorSettings =
    adminModelSettingsSchemaForProvider(providerDescriptor).safeParse(settings);
  if (!descriptorSettings.success) {
    throw new AdminModelEditorFormError(
      messages.advancedJsonUnsafe,
      "advanced_settings",
    );
  }

  const credentialId = readFormString(formData, "credential_id");
  const credentialVersionId = readFormString(formData, "credential_version_id");
  const credentialEnvKey = readFormString(formData, "credential_env_key");
  const credentialFieldCount = [
    credentialId,
    credentialVersionId,
    credentialEnvKey,
  ].filter(Boolean).length;
  const credentialBindingPresent = credentialFieldCount === 3;
  if (
    (credentialFieldCount !== 0 && !credentialBindingPresent) ||
    (providerDescriptor.credential_required && !credentialBindingPresent) ||
    (!providerDescriptor.credential_required && credentialFieldCount !== 0)
  ) {
    throw new AdminModelEditorFormError(messages.invalidForm, "credential_id");
  }
  const result = createAdminModelInputSchema.safeParse({
    display_name: readFormString(formData, "display_name"),
    provider_adapter: providerAdapter,
    provider_model: readFormString(formData, "provider_model"),
    settings: descriptorSettings.data,
    supports_thinking: formData.has("supports_thinking"),
    supports_reasoning_effort: formData.has("supports_reasoning_effort"),
    supports_vision: formData.has("supports_vision"),
    status: readFormString(formData, "status"),
    credential_id: credentialId || null,
    credential_version_id: credentialVersionId || null,
    credential_env_key: credentialEnvKey || null,
  });
  if (!result.success) {
    const firstPath = result.error.issues[0]?.path[0];
    const fieldId =
      firstPath === "credential_version_id"
        ? "credential_id"
        : typeof firstPath === "string"
          ? firstPath
          : null;
    throw new AdminModelEditorFormError(messages.invalidForm, fieldId);
  }
  return result.data;
}

export function parseAdminModelConnectionTestForm(
  formData: FormData,
  providerDescriptor: AdminModelProviderAdapterDescriptor,
  messages: AdminModelFormValidationMessages = DEFAULT_FORM_VALIDATION_MESSAGES,
): TestAdminModelConnectionInput {
  const input = parseAdminModelEditorForm(
    formData,
    providerDescriptor,
    messages,
  );
  return {
    provider_adapter: input.provider_adapter,
    provider_model: input.provider_model,
    settings: input.settings,
    supports_vision: input.supports_vision,
    credential_id: input.credential_id,
    credential_version_id: input.credential_version_id,
    credential_env_key: input.credential_env_key,
  };
}

function splitProviderSettings(
  settings: AdminModelItem["settings"],
  providerDescriptor: AdminModelProviderAdapterDescriptor | undefined,
): {
  visible: Record<string, string>;
  advanced: string;
} {
  const remainder: Record<string, AdminModelSettingValue> = { ...settings };
  delete remainder.max_retries;
  const visible = Object.fromEntries(
    selectAdminModelVisibleSettingFields(providerDescriptor).map((field) => {
      const value = remainder[field.name];
      delete remainder[field.name];
      return [
        field.name,
        typeof value === "string" ||
        typeof value === "number" ||
        typeof value === "boolean"
          ? String(value)
          : "",
      ];
    }),
  );
  return {
    visible,
    advanced: JSON.stringify(remainder, null, 2),
  };
}

function safeActionError(
  error: unknown,
  messages: AdminModelTranslations["actionErrors"],
): string {
  if (error instanceof AdminModelSettingsApiError) {
    if (error.code === "AUTH_REQUIRED") return messages.authRequired;
    if (error.status === 409) return messages.conflict;
    if (error.status === 422) return messages.invalid;
  }
  return messages.generic;
}

function CatalogMetric({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: string;
  icon: typeof BotIcon;
}) {
  return (
    <div className="bg-card flex min-w-0 items-center gap-3 px-4 py-4">
      <span className="bg-selection-subtle text-selection flex size-9 shrink-0 items-center justify-center rounded-lg">
        <Icon aria-hidden className="size-4" />
      </span>
      <div className="min-w-0">
        <p className="text-muted-foreground text-xs font-medium">{label}</p>
        <p
          className="mt-0.5 truncate text-base font-semibold tracking-tight"
          title={value}
        >
          {value}
        </p>
      </div>
    </div>
  );
}

function ModelIdentity({
  model,
  providerSupported,
}: {
  model: AdminModelItem;
  providerSupported: boolean;
}) {
  const { t } = useI18n();
  const labels = t.adminModelSettings;
  return (
    <div className="min-w-0">
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span
          className="min-w-0 font-medium [overflow-wrap:anywhere]"
          title={model.display_name}
        >
          {model.display_name}
        </span>
        {model.is_default ? (
          <Badge className="bg-selection text-selection-foreground">
            <StarIcon aria-hidden />
            {labels.card.defaultModel}
          </Badge>
        ) : null}
        {!providerSupported ? (
          <Badge variant="outline" className="text-muted-foreground">
            {labels.editor.retiredProviderAdapter}
          </Badge>
        ) : null}
      </div>
    </div>
  );
}

function ModelStatusBadge({ model }: { model: AdminModelItem }) {
  const labels = useI18n().t.adminModelSettings;
  return (
    <Badge
      variant="outline"
      className={
        model.status === "active"
          ? "border-success/30 bg-success/10"
          : "bg-muted text-muted-foreground"
      }
    >
      <span
        aria-hidden
        className={
          model.status === "active"
            ? "bg-success size-1.5 rounded-full"
            : "bg-muted-foreground/60 size-1.5 rounded-full"
        }
      />
      {model.status === "active" ? labels.card.active : labels.card.suspended}
    </Badge>
  );
}

function ModelCapabilities({ model }: { model: AdminModelItem }) {
  const labels = useI18n().t.adminModelSettings;
  const hasCapabilities =
    model.supports_thinking ||
    model.supports_reasoning_effort ||
    model.supports_vision;
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {model.supports_thinking ? (
        <Badge variant="outline" className="bg-muted/40">
          {labels.card.thinking}
        </Badge>
      ) : null}
      {model.supports_reasoning_effort ? (
        <Badge variant="outline" className="bg-muted/40">
          {labels.card.reasoningEffort}
        </Badge>
      ) : null}
      {model.supports_vision ? (
        <Badge variant="outline" className="bg-muted/40">
          {labels.card.vision}
        </Badge>
      ) : null}
      {!hasCapabilities ? (
        <span className="text-muted-foreground text-xs">
          {labels.card.noCapabilities}
        </span>
      ) : null}
    </div>
  );
}

function ModelActions({
  model,
  providerSupported,
  pendingAction,
  compact = false,
  idSuffix = "",
  onEdit,
  onToggleStatus,
  onSetDefault,
}: {
  model: AdminModelItem;
  providerSupported: boolean;
  pendingAction: string | null;
  compact?: boolean;
  idSuffix?: string;
  onEdit: (model: AdminModelItem) => void;
  onToggleStatus: (model: AdminModelItem) => void;
  onSetDefault: (model: AdminModelItem) => void;
}) {
  const { t } = useI18n();
  const labels = t.adminModelSettings;
  const pending = pendingAction !== null;
  const toggleUnavailable =
    model.is_default || (!providerSupported && model.status !== "active");
  const defaultUnavailable =
    !providerSupported || model.is_default || model.status !== "active";
  const toggleReasonId = `admin-model-${model.id}${idSuffix}-toggle-reason`;
  const defaultReasonId = `admin-model-${model.id}${idSuffix}-default-reason`;
  const editLabel = labels.card.actionFor(labels.card.edit, model.display_name);
  const toggleLabel = labels.card.actionFor(
    model.status === "active" ? labels.card.pause : labels.card.enable,
    model.display_name,
  );
  const defaultLabel = labels.card.actionFor(
    model.is_default ? labels.card.currentDefault : labels.card.setDefault,
    model.display_name,
  );

  return (
    <div
      className={
        compact ? "flex flex-nowrap gap-1.5" : "flex flex-wrap gap-1.5"
      }
    >
      <Button
        type="button"
        variant="outline"
        size={compact ? "icon-sm" : "sm"}
        disabled={pending}
        aria-label={editLabel}
        title={compact ? editLabel : undefined}
        onClick={() => onEdit(model)}
      >
        <PencilIcon aria-hidden />
        {compact ? null : labels.card.edit}
      </Button>
      <Button
        type="button"
        variant="outline"
        size={compact ? "icon-sm" : "sm"}
        disabled={pending}
        aria-label={toggleLabel}
        title={compact ? toggleLabel : undefined}
        aria-disabled={toggleUnavailable || undefined}
        aria-describedby={toggleUnavailable ? toggleReasonId : undefined}
        className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
        onClick={() => {
          if (!toggleUnavailable) onToggleStatus(model);
        }}
      >
        {model.status === "active" ? (
          <CirclePauseIcon aria-hidden />
        ) : (
          <CirclePlayIcon aria-hidden />
        )}
        {compact
          ? null
          : model.status === "active"
            ? labels.card.pause
            : labels.card.enable}
      </Button>
      <Button
        type="button"
        variant="secondary"
        size={compact ? "icon-sm" : "sm"}
        disabled={pending}
        aria-label={defaultLabel}
        title={compact ? defaultLabel : undefined}
        aria-disabled={defaultUnavailable || undefined}
        aria-describedby={defaultUnavailable ? defaultReasonId : undefined}
        className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
        onClick={() => {
          if (!defaultUnavailable) onSetDefault(model);
        }}
      >
        <StarIcon aria-hidden />
        {compact
          ? null
          : model.is_default
            ? labels.card.currentDefault
            : labels.card.setDefault}
      </Button>
      {toggleUnavailable ? (
        <span id={toggleReasonId} className="sr-only">
          {model.is_default
            ? labels.card.defaultCannotPause
            : labels.editor.retiredProviderAdapter}
        </span>
      ) : null}
      {defaultUnavailable ? (
        <span id={defaultReasonId} className="sr-only">
          {!providerSupported
            ? labels.editor.retiredProviderAdapter
            : model.is_default
              ? labels.card.currentDefault
              : labels.card.suspended}
        </span>
      ) : null}
    </div>
  );
}

function ModelCatalog({
  models,
  providerAdapters,
  pendingAction,
  onEdit,
  onToggleStatus,
  onSetDefault,
}: {
  models: AdminModelItem[];
  providerAdapters: AdminModelProviderAdapterDescriptor[];
  pendingAction: string | null;
  onEdit: (model: AdminModelItem) => void;
  onToggleStatus: (model: AdminModelItem) => void;
  onSetDefault: (model: AdminModelItem) => void;
}) {
  const { locale, t } = useI18n();
  const labels = t.adminModelSettings;
  return (
    <>
      <div
        data-testid="admin-model-catalog-table"
        data-density="compact"
        className="border-border/70 bg-card hidden overflow-hidden rounded-xl border lg:block"
      >
        <table className="w-full table-fixed border-collapse text-left text-sm">
          <colgroup>
            <col className="w-[17%]" />
            <col className="w-[19%]" />
            <col className="w-[8%]" />
            <col className="w-[19%]" />
            <col className="w-[11%]" />
            <col className="w-[14%]" />
            <col className="w-[12%]" />
          </colgroup>
          <thead className="bg-muted/40 text-muted-foreground">
            <tr className="border-border/70 border-b">
              <th className="px-4 py-2.5 text-xs font-medium">
                {labels.overview.configured}
              </th>
              <th className="px-3 py-2.5 text-xs font-medium">
                {labels.card.providerModel}
              </th>
              <th className="px-3 py-2.5 text-xs font-medium">
                {labels.card.status}
              </th>
              <th className="px-3 py-2.5 text-xs font-medium">
                {labels.card.capabilities}
              </th>
              <th className="px-3 py-2.5 text-xs font-medium">
                {labels.card.version}
              </th>
              <th className="px-3 py-2.5 text-xs font-medium">
                {labels.card.updatedAtColumn}
              </th>
              <th className="px-1.5 py-2.5 text-right text-xs font-medium">
                <span className="sr-only">{labels.card.actions}</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-border/70 divide-y">
            {models.map((model) => {
              const providerSupported =
                findAdminModelProviderAdapterDescriptor(
                  providerAdapters,
                  model.provider_adapter,
                ) !== undefined;
              return (
                <tr
                  key={model.id}
                  data-testid={`admin-model-${model.id}`}
                  className="hover:bg-muted/20 align-middle transition-colors"
                >
                  <td className="px-4 py-3.5">
                    <ModelIdentity
                      model={model}
                      providerSupported={providerSupported}
                    />
                  </td>
                  <td className="px-3 py-3.5">
                    <p
                      className="truncate font-mono text-xs font-medium"
                      title={model.provider_model}
                    >
                      {model.provider_model}
                    </p>
                  </td>
                  <td className="px-3 py-3.5">
                    <ModelStatusBadge model={model} />
                  </td>
                  <td className="px-3 py-3.5">
                    <ModelCapabilities model={model} />
                  </td>
                  <td className="px-3 py-3.5 text-xs">
                    <p
                      className="truncate font-medium"
                      title={labels.card.versionMeta(
                        model.version_number,
                        model.revision,
                      )}
                    >
                      v{model.version_number} · r{model.revision}
                    </p>
                  </td>
                  <td className="px-3 py-3.5 text-xs">
                    <p
                      className="text-muted-foreground truncate"
                      title={labels.card.updatedAt(
                        formatUpdatedAt(model.updated_at, locale),
                      )}
                    >
                      {formatUpdatedAt(model.updated_at, locale)}
                    </p>
                  </td>
                  <td className="px-1.5 py-3.5">
                    <ModelActions
                      model={model}
                      providerSupported={providerSupported}
                      pendingAction={pendingAction}
                      compact
                      onEdit={onEdit}
                      onToggleStatus={onToggleStatus}
                      onSetDefault={onSetDefault}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div
        data-testid="admin-model-mobile-list"
        data-density="compact"
        className="border-border/70 bg-card divide-border/70 divide-y overflow-hidden rounded-xl border lg:hidden"
      >
        {models.map((model) => {
          const providerSupported =
            findAdminModelProviderAdapterDescriptor(
              providerAdapters,
              model.provider_adapter,
            ) !== undefined;
          return (
            <article
              key={model.id}
              data-testid={`admin-model-mobile-${model.id}`}
              className="space-y-3 p-4"
            >
              <ModelIdentity
                model={model}
                providerSupported={providerSupported}
              />
              <p className="text-muted-foreground text-[0.6875rem]">
                {labels.card.updatedAt(
                  formatUpdatedAt(model.updated_at, locale),
                )}
              </p>
              <div className="grid gap-2 text-xs">
                <div className="min-w-0">
                  <span className="text-muted-foreground">
                    {labels.card.providerModel}
                  </span>
                  <p
                    className="mt-0.5 font-mono break-all"
                    title={model.provider_model}
                  >
                    {model.provider_model}
                  </p>
                </div>
              </div>
              <div className="grid gap-2 text-xs">
                <div>
                  <span className="text-muted-foreground">
                    {labels.card.status}
                  </span>
                  <div className="mt-0.5">
                    <ModelStatusBadge model={model} />
                  </div>
                </div>
                <div>
                  <span className="text-muted-foreground">
                    {labels.card.capabilities}
                  </span>
                  <div className="mt-0.5">
                    <ModelCapabilities model={model} />
                  </div>
                </div>
              </div>
              <ModelActions
                model={model}
                providerSupported={providerSupported}
                pendingAction={pendingAction}
                idSuffix="-mobile"
                onEdit={onEdit}
                onToggleStatus={onToggleStatus}
                onSetDefault={onSetDefault}
              />
            </article>
          );
        })}
      </div>
    </>
  );
}

export function AdminModelCatalogStateView({
  state,
  pendingAction,
  actionError,
  successMessage,
  onCreate,
  onEdit,
  onToggleStatus,
  onSetDefault,
  onRetry,
  retrying = false,
}: {
  state: AdminModelCatalogState;
  pendingAction: string | null;
  actionError?: string | null;
  successMessage?: string | null;
  onCreate: () => void;
  onEdit: (model: AdminModelItem) => void;
  onToggleStatus: (model: AdminModelItem) => void;
  onSetDefault: (model: AdminModelItem) => void;
  onRetry: () => void;
  retrying?: boolean;
}) {
  const { t } = useI18n();
  const labels = t.adminModelSettings;
  const catalogLabels = t.adminAssets.catalog;
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] =
    useState<AdminModelStatusFilter>("all");
  const catalog = state.status === "ready" ? state.data : null;
  const activeModels =
    catalog?.items.filter((model) => model.status === "active") ?? [];
  const defaultModel = catalog?.items.find((model) => model.is_default);
  const visibleModels = catalog
    ? selectAdminModelCatalogItems(catalog.items, search, statusFilter)
    : [];

  return (
    <main
      id="admin-main"
      className="mx-auto w-full max-w-[96rem] min-w-0 space-y-5 px-4 py-5 md:px-6 lg:py-6"
    >
      <header className="border-border/70 flex flex-col gap-4 border-b pb-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <span className="bg-selection-subtle text-selection mt-0.5 flex size-10 shrink-0 items-center justify-center rounded-xl">
            <BotIcon aria-hidden className="size-5" />
          </span>
          <div className="min-w-0">
            <p className="text-muted-foreground text-xs font-semibold tracking-[0.14em] uppercase">
              {labels.header.eyebrow}
            </p>
            <h1 className="mt-0.5 text-xl font-semibold tracking-tight">
              {labels.header.title}
            </h1>
          </div>
        </div>
        <Button
          type="button"
          disabled={
            state.status !== "ready" ||
            state.data.provider_adapters.length === 0
          }
          onClick={onCreate}
        >
          <PlusIcon aria-hidden />
          {labels.header.create}
        </Button>
      </header>

      <section aria-labelledby="model-catalog-overview">
        <h2 id="model-catalog-overview" className="sr-only">
          {labels.overview.label}
        </h2>
        <div className="border-border/70 bg-border/70 grid gap-px overflow-hidden rounded-xl border md:grid-cols-2 xl:grid-cols-4">
          <CatalogMetric
            label={labels.overview.configured}
            value={catalog ? String(catalog.items.length) : "—"}
            icon={Layers3Icon}
          />
          <CatalogMetric
            label={labels.overview.active}
            value={catalog ? String(activeModels.length) : "—"}
            icon={ActivityIcon}
          />
          <CatalogMetric
            label={labels.overview.defaultModel}
            value={
              defaultModel?.display_name ??
              (catalog ? labels.overview.notSet : "—")
            }
            icon={StarIcon}
          />
          <CatalogMetric
            label={labels.overview.revision}
            value={catalog ? `r${catalog.catalog_revision}` : "—"}
            icon={ShieldCheckIcon}
          />
        </div>
      </section>

      {actionError ? (
        <p
          role="alert"
          className="border-destructive/30 bg-destructive/5 text-destructive rounded-lg border px-4 py-3 text-sm"
        >
          {actionError}
        </p>
      ) : null}
      {successMessage ? (
        <p
          role="status"
          className="border-success/30 bg-success/10 rounded-lg border px-4 py-3 text-sm"
        >
          <CheckCircle2Icon
            aria-hidden
            className="text-success mr-1.5 inline-block size-4"
          />
          {successMessage}
        </p>
      ) : null}

      {state.status === "loading" ? (
        <section
          aria-label={labels.states.loading}
          className="grid gap-3 lg:grid-cols-2"
        >
          <span className="sr-only">{labels.states.loading}</span>
          <Skeleton className="h-80 w-full rounded-xl" />
          <Skeleton className="h-80 w-full rounded-xl" />
        </section>
      ) : state.status === "error" ? (
        <Card className="gap-0 border-dashed py-0 shadow-none">
          <CardHeader className="px-5 pt-5">
            <div className="bg-muted mb-3 flex size-10 items-center justify-center rounded-lg">
              <DatabaseIcon
                aria-hidden
                className="text-muted-foreground size-4"
              />
            </div>
            <CardTitle className="text-base">
              {labels.states.unavailableTitle}
            </CardTitle>
            <CardDescription>
              {labels.states.unavailableDescription}
            </CardDescription>
          </CardHeader>
          <CardContent className="px-5 pb-5">
            <Button
              data-testid="admin-model-catalog-retry"
              type="button"
              variant="outline"
              disabled={retrying}
              aria-busy={retrying}
              onClick={() => {
                if (!retrying) onRetry();
              }}
            >
              <RefreshCwIcon
                aria-hidden
                className={retrying ? "animate-spin" : undefined}
              />
              {labels.states.retry}
            </Button>
          </CardContent>
        </Card>
      ) : state.data.items.length === 0 ? (
        <Card className="gap-0 border-dashed py-0 shadow-none">
          <CardHeader className="items-center px-5 py-12 text-center">
            <span className="bg-muted mb-2 flex size-11 items-center justify-center rounded-xl">
              <BotIcon aria-hidden className="text-muted-foreground size-5" />
            </span>
            <CardTitle className="text-base">
              {labels.states.emptyTitle}
            </CardTitle>
            <CardDescription>{labels.states.emptyDescription}</CardDescription>
          </CardHeader>
        </Card>
      ) : (
        <section aria-label={labels.states.catalogLabel} className="space-y-3">
          <div
            data-testid="admin-model-catalog-toolbar"
            className="border-border/70 bg-card flex flex-col gap-2 rounded-xl border p-2 sm:flex-row sm:items-center"
          >
            <label className="relative min-w-0 flex-1">
              <span className="sr-only">{catalogLabels.searchPlaceholder}</span>
              <SearchIcon
                aria-hidden
                className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                data-testid="admin-model-search"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder={catalogLabels.searchPlaceholder}
                className="border-transparent bg-transparent pl-9 shadow-none sm:max-w-md"
              />
            </label>
            <label className="min-w-0">
              <span className="sr-only">{labels.editor.status}</span>
              <select
                data-testid="admin-model-status-filter"
                aria-label={labels.editor.status}
                value={statusFilter}
                onChange={(event) =>
                  setStatusFilter(event.target.value as AdminModelStatusFilter)
                }
                className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm sm:w-40"
              >
                <option value="all">{catalogLabels.filterAll}</option>
                <option value="active">{labels.card.active}</option>
                <option value="suspended">{labels.card.suspended}</option>
              </select>
            </label>
            <Button
              data-testid="admin-model-refresh"
              type="button"
              variant="outline"
              size="sm"
              disabled={retrying}
              aria-busy={retrying}
              onClick={() => {
                if (!retrying) onRetry();
              }}
            >
              <RefreshCwIcon
                aria-hidden
                className={retrying ? "animate-spin" : undefined}
              />
              {retrying ? catalogLabels.refreshing : catalogLabels.refresh}
            </Button>
          </div>

          {visibleModels.length > 0 ? (
            <ModelCatalog
              models={visibleModels}
              providerAdapters={state.data.provider_adapters}
              pendingAction={pendingAction}
              onEdit={onEdit}
              onToggleStatus={onToggleStatus}
              onSetDefault={onSetDefault}
            />
          ) : (
            <div className="border-border/70 bg-muted/15 text-muted-foreground rounded-xl border border-dashed px-4 py-12 text-center text-sm">
              {catalogLabels.noResults}
            </div>
          )}

          <div className="text-muted-foreground flex min-h-5 items-center justify-between gap-3 text-xs">
            <span>
              {visibleModels.length > 0
                ? catalogLabels.resultRange(
                    1,
                    visibleModels.length,
                    state.data.items.length,
                  )
                : catalogLabels.noResults}
            </span>
          </div>
        </section>
      )}
    </main>
  );
}

function LabeledField({
  label,
  htmlFor,
  hint,
  hintId,
  className,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  hintId?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <label htmlFor={htmlFor} className="text-sm font-medium">
        {label}
      </label>
      {children}
      {hint ? (
        <p id={hintId} className="text-muted-foreground text-xs">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function AdminModelProviderSettingInput({
  field,
  value,
  invalidFieldId,
}: {
  field: AdminModelProviderSettingField;
  value: string;
  invalidFieldId: string | null;
}) {
  const errorAttributes = adminModelEditorFieldErrorAttributes(
    field.name,
    invalidFieldId,
  );
  if (field.input_type === "boolean") {
    return (
      <LabeledField label={field.label} htmlFor={field.name}>
        <select
          id={field.name}
          name={field.name}
          {...errorAttributes}
          defaultValue={value}
          className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
        >
          <option value="">Default</option>
          <option value="true">True</option>
          <option value="false">False</option>
        </select>
      </LabeledField>
    );
  }
  if (field.input_type === "enum") {
    return (
      <LabeledField label={field.label} htmlFor={field.name}>
        <select
          id={field.name}
          name={field.name}
          {...errorAttributes}
          defaultValue={value}
          className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
        >
          <option value="">Default</option>
          {field.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </LabeledField>
    );
  }
  const numeric =
    field.input_type === "integer" || field.input_type === "number";
  return (
    <LabeledField label={field.label} htmlFor={field.name}>
      <Input
        id={field.name}
        name={field.name}
        {...errorAttributes}
        type={numeric ? "number" : field.input_type === "url" ? "url" : "text"}
        {...(numeric && field.minimum !== null ? { min: field.minimum } : {})}
        {...(numeric && field.maximum !== null ? { max: field.maximum } : {})}
        {...(numeric
          ? {
              step: field.step ?? (field.input_type === "integer" ? 1 : "any"),
            }
          : {})}
        defaultValue={value}
      />
    </LabeledField>
  );
}

export function ModelEditorDialog({
  open,
  model,
  providerAdapters,
  credentials,
  credentialStatus,
  credentialsRefreshing,
  pending,
  mutationError,
  onOpenChange,
  onRetryCredentials,
  onSubmit,
  onTestConnection,
}: {
  open: boolean;
  model: AdminModelItem | null;
  providerAdapters: AdminModelProviderAdapterDescriptor[];
  credentials: AdminModelCredentialOption[];
  credentialStatus: AdminModelCredentialLoadStatus;
  credentialsRefreshing: boolean;
  pending: boolean;
  mutationError: string | null;
  onOpenChange: (open: boolean) => void;
  onRetryCredentials: () => void;
  onSubmit: (input: CreateAdminModelInput) => Promise<boolean>;
  onTestConnection: (
    input: TestAdminModelConnectionInput,
  ) => Promise<AdminModelConnectionTestResponse>;
}) {
  const { t } = useI18n();
  const labels = t.adminModelSettings;
  const initialProviderAdapter =
    model?.provider_adapter ?? providerAdapters[0]?.id ?? "";
  const initialProviderDescriptor = findAdminModelProviderAdapterDescriptor(
    providerAdapters,
    initialProviderAdapter,
  );
  const providerSettings = useMemo(
    () =>
      splitProviderSettings(model?.settings ?? {}, initialProviderDescriptor),
    [initialProviderDescriptor, model],
  );
  const initialCredentialState = buildAdminModelCredentialEditorState(
    model,
    credentials,
  );
  const [formError, setFormError] = useState<string | null>(null);
  const [invalidFieldId, setInvalidFieldId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [testingConnection, setTestingConnection] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<
    AdminModelConnectionTestResponse["status"] | null
  >(null);
  const [advancedSettingsOpen, setAdvancedSettingsOpen] = useState(false);
  const [credentialId, setCredentialId] = useState(
    initialCredentialState.credentialId,
  );
  const [credentialVersionId, setCredentialVersionId] = useState(
    initialCredentialState.credentialVersionId,
  );
  const [credentialEnvKey, setCredentialEnvKey] = useState(
    model?.credential_env_key ?? "OPENAI_API_KEY",
  );
  const [providerAdapter, setProviderAdapter] = useState<string>(
    initialProviderAdapter,
  );
  const selectedProviderAdapter = findAdminModelProviderAdapterDescriptor(
    providerAdapters,
    providerAdapter,
  );
  const providerAdapterSupported = selectedProviderAdapter != null;
  const providerAcceptsCredential =
    selectedProviderAdapter?.credential_required ??
    (model?.provider_adapter === providerAdapter &&
      model.credential_id !== null);
  const visibleSettingFields = selectAdminModelVisibleSettingFields(
    selectedProviderAdapter,
  );
  const formRef = useRef<HTMLFormElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const credentialChoices = useMemo(
    () => buildAdminModelCredentialEditorState(model, credentials).choices,
    [credentials, model],
  );
  const credentialChoiceValue =
    credentialId && credentialVersionId
      ? `${credentialId}:${credentialVersionId}`
      : "";
  const selectedCredentialChoice = credentialChoices.find(
    (choice) =>
      choice.id === credentialId &&
      choice.credential_version_id === credentialVersionId,
  );
  const credentialBindingReady =
    !providerAcceptsCredential ||
    (credentialId !== "" &&
      selectedCredentialChoice !== undefined &&
      !selectedCredentialChoice.historical &&
      !selectedCredentialChoice.unavailable);
  const closingBlocked = pending || submitting || testingConnection;
  const editorCanSubmit =
    providerAdapterSupported &&
    canSubmitAdminModelEditor(
      credentialStatus,
      closingBlocked,
      providerAcceptsCredential,
      credentialBindingReady,
    );

  useEffect(() => {
    if (!formError && !mutationError) return;
    const frame = window.requestAnimationFrame(() => {
      const field = invalidFieldId
        ? formRef.current?.elements.namedItem(invalidFieldId)
        : null;
      const fieldTarget =
        field != null && "focus" in field ? (field as HTMLElement) : undefined;
      const target = fieldTarget ?? errorRef.current;
      target?.focus();
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [formError, invalidFieldId, mutationError]);

  function changeCredential(nextValue: string): void {
    const selected = credentialChoices.find(
      (credential) =>
        `${credential.id}:${credential.credential_version_id}` === nextValue,
    );
    setCredentialId(selected?.id ?? "");
    setCredentialVersionId(selected?.credential_version_id ?? "");
    if (selected && credentialEnvKey === "") {
      setCredentialEnvKey("OPENAI_API_KEY");
    }
  }

  function changeProviderAdapter(nextAdapter: string): void {
    setProviderAdapter(nextAdapter);
    if (
      !findAdminModelProviderAdapterDescriptor(providerAdapters, nextAdapter)
        ?.credential_required
    ) {
      setCredentialId("");
      setCredentialVersionId("");
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!editorCanSubmit || !selectedProviderAdapter) return;
    setFormError(null);
    setInvalidFieldId(null);
    setSubmitting(true);
    try {
      const input = parseAdminModelEditorForm(
        new FormData(event.currentTarget),
        selectedProviderAdapter,
        labels.validation,
      );
      if (await onSubmit(input)) {
        onOpenChange(false);
        return;
      }
      setSubmitting(false);
    } catch (error) {
      setSubmitting(false);
      const invalidField = getAdminModelEditorErrorField(error);
      setInvalidFieldId(invalidField);
      if (invalidField === "advanced_settings") {
        setAdvancedSettingsOpen(true);
      }
      setFormError(
        error instanceof Error
          ? error.message
          : labels.validation.invalidConfiguration,
      );
    }
  }

  async function testConnection(): Promise<void> {
    if (!formRef.current || !editorCanSubmit || !selectedProviderAdapter)
      return;
    setFormError(null);
    setInvalidFieldId(null);
    setConnectionStatus(null);
    setTestingConnection(true);
    try {
      const input = parseAdminModelConnectionTestForm(
        new FormData(formRef.current),
        selectedProviderAdapter,
        labels.validation,
      );
      const result = await onTestConnection(input);
      setConnectionStatus(result.status);
    } catch (error) {
      const invalidField = getAdminModelEditorErrorField(error);
      setInvalidFieldId(invalidField);
      if (invalidField === "advanced_settings") {
        setAdvancedSettingsOpen(true);
      }
      if (invalidField) {
        setFormError(
          error instanceof Error
            ? error.message
            : labels.validation.invalidConfiguration,
        );
      } else {
        setConnectionStatus("failed");
      }
    } finally {
      setTestingConnection(false);
    }
  }

  function requestOpenChange(nextOpen: boolean): void {
    if (!nextOpen && !canCloseAdminModelEditor(closingBlocked)) return;
    onOpenChange(nextOpen);
  }

  return (
    <Dialog open={open} onOpenChange={requestOpenChange}>
      <DialogContent
        closeLabel={t.adminOperations.ui.close}
        data-testid="admin-model-editor-workspace"
        className={cn(
          "data-[state=closed]:slide-out-to-right data-[state=closed]:zoom-out-100 data-[state=open]:slide-in-from-right data-[state=open]:zoom-in-100 inset-y-0 top-0 right-0 left-auto h-dvh max-h-dvh w-full max-w-none translate-x-0 translate-y-0 grid-rows-[auto_minmax(0,1fr)] gap-0 overflow-hidden rounded-none border-y-0 border-r-0 p-0 sm:w-[min(48rem,96vw)] sm:max-w-none lg:w-[min(58rem,78vw)] xl:w-[min(64rem,72vw)]",
          closingBlocked && "[&_[data-slot=dialog-close]]:hidden",
        )}
        aria-busy={closingBlocked}
        onEscapeKeyDown={(event) => {
          if (closingBlocked) event.preventDefault();
        }}
        onPointerDownOutside={(event) => {
          if (closingBlocked) event.preventDefault();
        }}
      >
        <DialogHeader className="border-border/70 bg-muted/20 border-b py-5 pr-14 pl-6">
          <DialogTitle>
            {model ? labels.editor.editTitle : labels.editor.createTitle}
          </DialogTitle>
          <DialogDescription>{labels.editor.description}</DialogDescription>
        </DialogHeader>

        <form
          ref={formRef}
          className="flex min-h-0 flex-col"
          onSubmit={(event) => void submit(event)}
        >
          <fieldset
            disabled={!canCloseAdminModelEditor(closingBlocked)}
            className="contents"
          >
            <div className="grid min-h-0 auto-rows-max grid-cols-1 overflow-y-auto p-4 sm:p-6 xl:grid-cols-2">
              <fieldset
                data-testid="admin-model-editor-basic-information"
                className="border-border/70 space-y-4 border-t pt-5 pb-6 xl:col-span-2"
              >
                <legend className="px-0 text-sm font-semibold">
                  {labels.editor.basicInformation}
                </legend>
                <p className="text-muted-foreground mb-4 text-xs">
                  {labels.editor.basicDescription}
                </p>
                <div className="grid gap-4 sm:grid-cols-2">
                  <LabeledField
                    label={labels.editor.displayName}
                    htmlFor="display_name"
                  >
                    <Input
                      id="display_name"
                      name="display_name"
                      {...adminModelEditorFieldErrorAttributes(
                        "display_name",
                        invalidFieldId,
                      )}
                      required
                      defaultValue={model?.display_name ?? ""}
                      placeholder={labels.editor.displayNamePlaceholder}
                    />
                  </LabeledField>
                  <LabeledField
                    label={labels.editor.providerAdapter}
                    htmlFor="provider_adapter"
                    hint={
                      providerAdapterSupported
                        ? undefined
                        : labels.editor.retiredProviderAdapterHint
                    }
                  >
                    <select
                      id="provider_adapter"
                      name="provider_adapter"
                      {...adminModelEditorFieldErrorAttributes(
                        "provider_adapter",
                        invalidFieldId,
                      )}
                      required
                      value={providerAdapter}
                      onChange={(event) =>
                        changeProviderAdapter(event.target.value)
                      }
                      className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
                    >
                      <AdminModelProviderAdapterOptions
                        providerAdapters={providerAdapters}
                        value={providerAdapter}
                      />
                    </select>
                  </LabeledField>
                  <LabeledField
                    label={labels.editor.providerModel}
                    htmlFor="provider_model"
                  >
                    <Input
                      id="provider_model"
                      name="provider_model"
                      {...adminModelEditorFieldErrorAttributes(
                        "provider_model",
                        invalidFieldId,
                      )}
                      required
                      defaultValue={model?.provider_model ?? ""}
                      placeholder="gpt-5.2"
                    />
                  </LabeledField>
                  <LabeledField label={labels.editor.status} htmlFor="status">
                    {model ? (
                      <>
                        <Input
                          id="status"
                          value={
                            model.status === "active"
                              ? labels.card.active
                              : labels.card.suspended
                          }
                          readOnly
                        />
                        <input
                          type="hidden"
                          name="status"
                          value={model.status}
                        />
                      </>
                    ) : (
                      <select
                        id="status"
                        name="status"
                        {...adminModelEditorFieldErrorAttributes(
                          "status",
                          invalidFieldId,
                        )}
                        defaultValue="suspended"
                        className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
                      >
                        <option value="active">{labels.editor.active}</option>
                        <option value="suspended">
                          {labels.editor.suspended}
                        </option>
                      </select>
                    )}
                  </LabeledField>
                </div>
              </fieldset>

              <fieldset
                data-testid="admin-model-editor-credential-binding"
                className="border-border/70 space-y-4 border-t pt-5 pb-6 xl:col-span-2"
              >
                <legend className="px-0 text-sm font-semibold">
                  {labels.editor.credentialBinding}
                </legend>
                <div className="grid gap-4 sm:grid-cols-2">
                  <LabeledField
                    label={labels.editor.systemCredential}
                    htmlFor="credential_id"
                    hint={
                      !providerAcceptsCredential
                        ? labels.editor.providerDoesNotUseCredential
                        : credentialStatus !== "ready"
                          ? labels.editor.credentialsUnavailableHint
                          : selectedCredentialChoice?.historical
                            ? `${labels.card.credentialHistorical} — ${labels.editor.credentialSelectionHint}`
                            : selectedCredentialChoice?.unavailable
                              ? `${labels.card.credentialUnavailable} — ${labels.editor.credentialSelectionHint}`
                              : labels.editor.credentialSelectionHint
                    }
                    hintId={MODEL_EDITOR_CREDENTIAL_STATUS_ID}
                  >
                    <div
                      className="space-y-2"
                      aria-busy={
                        credentialStatus === "loading" || credentialsRefreshing
                      }
                    >
                      <select
                        id="credential_id"
                        name="credential_choice"
                        value={credentialChoiceValue}
                        disabled={
                          credentialStatus !== "ready" ||
                          !providerAcceptsCredential
                        }
                        {...adminModelEditorFieldErrorAttributes(
                          "credential_id",
                          invalidFieldId,
                          MODEL_EDITOR_CREDENTIAL_STATUS_ID,
                        )}
                        onChange={(event) =>
                          changeCredential(event.target.value)
                        }
                        className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
                      >
                        <option value="">
                          {providerAcceptsCredential
                            ? labels.editor.selectCredential
                            : labels.editor.providerDoesNotUseCredential}
                        </option>
                        {credentialChoices.map((credential) => (
                          <option
                            key={`${credential.id}:${credential.credential_version_id}`}
                            value={`${credential.id}:${credential.credential_version_id}`}
                            disabled={
                              credential.unavailable &&
                              (credential.id !== credentialId ||
                                credential.credential_version_id !==
                                  credentialVersionId)
                            }
                          >
                            {credential.unavailable
                              ? credential.display_name
                                ? `${credential.display_name} · ${labels.card.credentialUnavailable}`
                                : labels.card.credentialUnavailable
                              : credential.historical
                                ? `${credential.display_name} · ${labels.card.credentialHistorical}`
                                : `${credential.display_name} · v${credential.version}`}
                          </option>
                        ))}
                      </select>
                      {providerAcceptsCredential &&
                      credentialStatus === "error" ? (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={credentialsRefreshing}
                          aria-busy={credentialsRefreshing}
                          onClick={() => {
                            if (!credentialsRefreshing) onRetryCredentials();
                          }}
                        >
                          <RefreshCwIcon
                            aria-hidden
                            className={
                              credentialsRefreshing ? "animate-spin" : undefined
                            }
                          />
                          {labels.states.retry}
                        </Button>
                      ) : null}
                    </div>
                    <input
                      type="hidden"
                      name="credential_id"
                      value={credentialId}
                    />
                    <input
                      type="hidden"
                      name="credential_version_id"
                      value={credentialVersionId}
                    />
                  </LabeledField>
                  <LabeledField
                    label={labels.editor.environmentKey}
                    htmlFor="credential_env_key"
                    hint={labels.editor.environmentKeyHint}
                    hintId={MODEL_EDITOR_ENVIRONMENT_KEY_HINT_ID}
                  >
                    <Input
                      id="credential_env_key"
                      name="credential_env_key"
                      {...adminModelEditorFieldErrorAttributes(
                        "credential_env_key",
                        invalidFieldId,
                        MODEL_EDITOR_ENVIRONMENT_KEY_HINT_ID,
                      )}
                      value={credentialEnvKey}
                      disabled={
                        credentialStatus !== "ready" ||
                        !providerAcceptsCredential ||
                        credentialId === ""
                      }
                      required={
                        providerAcceptsCredential && credentialId !== ""
                      }
                      onChange={(event) =>
                        setCredentialEnvKey(event.target.value.toUpperCase())
                      }
                      placeholder="OPENAI_API_KEY"
                    />
                  </LabeledField>
                </div>
                <div className="border-border/70 flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
                  <p className="text-muted-foreground text-xs">
                    {labels.editor.testConnectionDescription}
                  </p>
                  <div className="flex flex-wrap items-center gap-3">
                    <Button
                      data-testid="admin-model-test-connection"
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={!editorCanSubmit}
                      aria-busy={testingConnection}
                      aria-describedby={
                        providerAcceptsCredential &&
                        (credentialStatus !== "ready" ||
                          !credentialBindingReady)
                          ? MODEL_EDITOR_CREDENTIAL_STATUS_ID
                          : undefined
                      }
                      onClick={() => void testConnection()}
                    >
                      <ActivityIcon
                        aria-hidden
                        className={
                          testingConnection ? "animate-spin" : undefined
                        }
                      />
                      {testingConnection
                        ? labels.editor.testingConnection
                        : labels.editor.testConnection}
                    </Button>
                    {connectionStatus ? (
                      <p
                        data-testid="admin-model-test-connection-status"
                        role={
                          connectionStatus === "succeeded" ? "status" : "alert"
                        }
                        className={cn(
                          "text-sm",
                          connectionStatus === "succeeded"
                            ? "text-success"
                            : "text-destructive",
                        )}
                      >
                        {connectionStatus === "succeeded" ? (
                          <CheckCircle2Icon
                            aria-hidden
                            className="mr-1 inline-block size-4"
                          />
                        ) : null}
                        {connectionStatus === "succeeded"
                          ? labels.editor.connectionSucceeded
                          : labels.editor.connectionFailed}
                      </p>
                    ) : null}
                  </div>
                </div>
              </fieldset>

              <fieldset
                data-testid="admin-model-editor-capabilities-and-runtime"
                className="border-border/70 space-y-4 border-t pt-5 pb-6 xl:col-span-2"
              >
                <legend className="px-0 text-sm font-semibold">
                  {labels.editor.capabilitiesAndRuntime}
                </legend>
                <div className="grid gap-3 sm:grid-cols-3">
                  {[
                    [
                      "supports_thinking",
                      labels.editor.supportsThinking,
                      model?.supports_thinking,
                    ],
                    [
                      "supports_reasoning_effort",
                      labels.editor.supportsReasoningEffort,
                      model?.supports_reasoning_effort,
                    ],
                    [
                      "supports_vision",
                      labels.editor.supportsVision,
                      model?.supports_vision,
                    ],
                  ].map(([name, label, checked]) => (
                    <label
                      key={String(name)}
                      className="border-border/70 bg-background hover:bg-muted/30 flex items-center gap-2 rounded-lg border p-3 text-sm transition-colors"
                    >
                      <input
                        type="checkbox"
                        name={String(name)}
                        defaultChecked={Boolean(checked)}
                        className="accent-selection size-4"
                      />
                      {String(label)}
                    </label>
                  ))}
                </div>
                {visibleSettingFields.length > 0 ? (
                  <div className="border-border/70 space-y-4 border-t pt-5">
                    <div>
                      <p className="text-sm font-medium">
                        {labels.editor.commonProviderSettings}
                      </p>
                    </div>
                    <div className="grid gap-4 sm:grid-cols-2">
                      {visibleSettingFields.map((field) => (
                        <AdminModelProviderSettingInput
                          key={field.name}
                          field={field}
                          value={providerSettings.visible[field.name] ?? ""}
                          invalidFieldId={invalidFieldId}
                        />
                      ))}
                    </div>
                  </div>
                ) : null}
              </fieldset>

              <details
                data-testid="admin-model-editor-advanced-settings"
                open={advancedSettingsOpen}
                onToggle={(event) =>
                  setAdvancedSettingsOpen(event.currentTarget.open)
                }
                className="border-border/70 group border-t xl:col-span-2"
              >
                <summary className="flex cursor-pointer list-none items-center justify-between gap-4 py-4 text-sm font-semibold [&::-webkit-details-marker]:hidden">
                  <span>{labels.editor.advancedJson}</span>
                  <ChevronDownIcon
                    aria-hidden
                    className="text-muted-foreground size-4 transition-transform group-open:rotate-180"
                  />
                </summary>
                <div className="border-border/70 border-t pt-4 pb-6">
                  <LabeledField
                    label={labels.editor.advancedJson}
                    htmlFor="advanced_settings"
                    hint={labels.editor.advancedJsonHint}
                    hintId={MODEL_EDITOR_ADVANCED_SETTINGS_HINT_ID}
                  >
                    <Textarea
                      id="advanced_settings"
                      name="advanced_settings"
                      {...adminModelEditorFieldErrorAttributes(
                        "advanced_settings",
                        invalidFieldId,
                        MODEL_EDITOR_ADVANCED_SETTINGS_HINT_ID,
                      )}
                      className="bg-muted/20 min-h-36 font-mono text-xs"
                      defaultValue={providerSettings.advanced}
                      spellCheck={false}
                    />
                  </LabeledField>
                </div>
              </details>

              {formError || mutationError ? (
                <p
                  ref={errorRef}
                  id={MODEL_EDITOR_ERROR_ID}
                  role="alert"
                  tabIndex={-1}
                  className="border-destructive/30 bg-destructive/5 text-destructive rounded-lg border px-4 py-3 text-sm"
                >
                  {formError ?? mutationError}
                </p>
              ) : null}
            </div>
          </fieldset>

          <DialogFooter className="border-border/70 bg-background border-t px-6 py-4">
            <Button
              type="button"
              variant="outline"
              disabled={closingBlocked}
              onClick={() => requestOpenChange(false)}
            >
              {labels.editor.cancel}
            </Button>
            <Button
              type="submit"
              disabled={!editorCanSubmit}
              aria-disabled={!editorCanSubmit}
              aria-describedby={
                providerAcceptsCredential &&
                (credentialStatus !== "ready" || !credentialBindingReady)
                  ? MODEL_EDITOR_CREDENTIAL_STATUS_ID
                  : undefined
              }
            >
              {closingBlocked
                ? labels.editor.saving
                : model
                  ? labels.editor.saveChanges
                  : labels.editor.createModel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function AuthorizedAdminModelSettingsPage({
  accountId,
}: {
  accountId: string;
}) {
  const { t } = useI18n();
  const labels = t.adminModelSettings;
  const catalog = useAdminModelCatalog(accountId);
  const credentialsQuery = useAdminAssets(accountId, "credentials");
  const createModel = useCreateAdminModel(accountId);
  const replaceModel = useReplaceAdminModel(accountId);
  const testConnection = useTestAdminModelConnection(accountId);
  const changeStatus = useSetAdminModelStatus(accountId);
  const setDefault = useSetAdminModelDefault(accountId);
  const [editor, setEditor] = useState<{
    open: boolean;
    model: AdminModelItem | null;
  }>({ open: false, model: null });
  const [actionError, setActionError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const credentials = selectAdminModelCredentialOptions(
    credentialsQuery.data as AdminCredentialList | undefined,
  );
  const credentialStatus: AdminModelCredentialLoadStatus =
    credentialsQuery.isError
      ? "error"
      : credentialsQuery.data
        ? "ready"
        : "loading";

  const state: AdminModelCatalogState = catalog.isLoading
    ? { status: "loading" }
    : catalog.error || !catalog.data
      ? { status: "error" }
      : { status: "ready", data: catalog.data };
  const pendingAction = createModel.isPending
    ? "create"
    : replaceModel.isPending
      ? "replace"
      : changeStatus.isPending
        ? "status"
        : setDefault.isPending
          ? "default"
          : null;

  function beginAction(): void {
    setActionError(null);
    setSuccessMessage(null);
  }

  async function submitEditor(input: CreateAdminModelInput): Promise<boolean> {
    beginAction();
    try {
      if (editor.model) {
        await replaceModel.mutateAsync({
          modelId: editor.model.id,
          input: {
            display_name: input.display_name,
            provider_adapter: input.provider_adapter,
            provider_model: input.provider_model,
            settings: input.settings,
            supports_thinking: input.supports_thinking,
            supports_reasoning_effort: input.supports_reasoning_effort,
            supports_vision: input.supports_vision,
            credential_id: input.credential_id,
            credential_version_id: input.credential_version_id,
            credential_env_key: input.credential_env_key,
            expected_revision: editor.model.revision,
          },
        });
        setSuccessMessage(labels.success.updated(input.display_name));
      } else {
        await createModel.mutateAsync(input);
        setSuccessMessage(labels.success.created(input.display_name));
      }
      return true;
    } catch (error) {
      setActionError(safeActionError(error, labels.actionErrors));
      return false;
    }
  }

  async function testEditorConnection(
    input: TestAdminModelConnectionInput,
  ): Promise<AdminModelConnectionTestResponse> {
    try {
      return await testConnection.mutateAsync(input);
    } catch {
      return { status: "failed", request_id: "client" };
    }
  }

  async function toggleStatus(model: AdminModelItem): Promise<void> {
    beginAction();
    try {
      const nextStatus = model.status === "active" ? "suspended" : "active";
      await changeStatus.mutateAsync({
        modelId: model.id,
        input: {
          status: nextStatus,
          expected_revision: model.revision,
        },
      });
      setSuccessMessage(
        nextStatus === "active"
          ? labels.success.enabled(model.display_name)
          : labels.success.suspended(model.display_name),
      );
    } catch (error) {
      setActionError(safeActionError(error, labels.actionErrors));
    }
  }

  async function makeDefault(model: AdminModelItem): Promise<void> {
    if (state.status !== "ready") return;
    beginAction();
    try {
      await setDefault.mutateAsync({
        modelId: model.id,
        input: {
          expected_catalog_revision: state.data.catalog_revision,
        },
      });
      setSuccessMessage(labels.success.defaultSet(model.display_name));
    } catch (error) {
      setActionError(safeActionError(error, labels.actionErrors));
    }
  }

  return (
    <>
      <AdminModelCatalogStateView
        state={state}
        pendingAction={pendingAction}
        actionError={editor.open ? null : actionError}
        successMessage={successMessage}
        onCreate={() => {
          beginAction();
          setEditor({ open: true, model: null });
        }}
        onEdit={(model) => {
          beginAction();
          setEditor({ open: true, model });
        }}
        onToggleStatus={(model) => void toggleStatus(model)}
        onSetDefault={(model) => void makeDefault(model)}
        onRetry={() => void catalog.refetch()}
        retrying={catalog.isFetching}
      />
      {editor.open ? (
        <ModelEditorDialog
          key={editor.model?.id ?? "create"}
          open={editor.open}
          model={editor.model}
          providerAdapters={
            state.status === "ready" ? state.data.provider_adapters : []
          }
          credentials={credentials}
          credentialStatus={credentialStatus}
          credentialsRefreshing={credentialsQuery.isFetching}
          pending={createModel.isPending || replaceModel.isPending}
          mutationError={actionError}
          onOpenChange={(open) =>
            setEditor((current) => ({ ...current, open }))
          }
          onRetryCredentials={() => void credentialsQuery.refetch()}
          onSubmit={submitEditor}
          onTestConnection={testEditorConnection}
        />
      ) : null}
    </>
  );
}

/**
 * Client-side defence in depth. The server /admin layout and Gateway still
 * remain authoritative; this gate prevents even constructing admin queries
 * before the authenticated system_admin identity is present.
 */
export function AdminModelSettingsPage() {
  const { user } = useAuth();
  if (user?.system_role !== "system_admin") return null;
  return <AuthorizedAdminModelSettingsPage accountId={user.id} />;
}
