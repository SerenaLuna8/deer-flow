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
  type AdminModelItem,
  type AdminModelProviderAdapterDescriptor,
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
    providerSettingsJson: "Provider settings JSON",
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
    invalidSettingsJson: "Provider settings must be a JSON object.",
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
    providerSettingsJson: "Provider 设置 JSON",
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
    invalidSettingsJson: "Provider 设置必须是 JSON 对象。",
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
  provider: string,
  apiKey: string,
  clearApiKey: () => void,
  locale: Locale = "zh-CN",
) {
  // Consume the write-only value before JSON or field validation can return.
  // The returned local is used only by the immediate imperative request.
  const submittedKey = consumeWriteOnlyInput(apiKey, clearApiKey);
  const settingsText = formString(form, "settings", "{}");
  let settings: CreateAdminModelInput["settings"];
  try {
    const parsed = JSON.parse(settingsText) as unknown;
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error();
    }
    settings = parsed as CreateAdminModelInput["settings"];
  } catch {
    throw new Error(adminModelSettingsCopy(locale).invalidSettingsJson);
  }
  return {
    common: {
      display_name: formString(form, "display_name").trim(),
      provider_adapter: provider,
      provider_model: formString(form, "provider_model").trim(),
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
  testPending,
}: {
  apiKey: string;
  creating: boolean;
  pending: boolean;
  providerRequiresApiKey: boolean;
  testPending: boolean;
}): boolean {
  return (
    pending ||
    testPending ||
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
  const [provider, setProvider] = useState(
    target?.provider_adapter ?? descriptors[0]?.id ?? "",
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
        provider,
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
        provider,
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
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
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
                value={provider}
                onChange={(event) => setProvider(event.target.value)}
              >
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
            {copy.providerSettingsJson}
            <textarea
              name="settings"
              className="border-input bg-background min-h-36 rounded-md border p-3 font-mono text-sm"
              defaultValue={JSON.stringify(target?.settings ?? {}, null, 2)}
            />
          </label>
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
              disabled={pending || testPending || apiKey === ""}
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
