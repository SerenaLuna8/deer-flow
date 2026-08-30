"use client";

import { PlusIcon, RefreshCwIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { AdminSection } from "@/components/admin/ui/admin-page";
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
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AdminModelRegistryApiError,
  isKnowledgeDisabledError,
} from "@/core/admin-settings/model-registry/api";
import {
  useAdminModelProviders,
  useAdminProviderModels,
  useCreateAdminModelProvider,
  useCreateAdminProviderModel,
  useDeleteAdminModelProvider,
  useDeleteAdminProviderModel,
  useSetAdminProviderModelStatus,
  useTestAdminProviderModel,
  useUpdateAdminModelProvider,
} from "@/core/admin-settings/model-registry/hooks";
import type {
  AdminModelProviderItem,
  AdminProviderModelItem,
  AdminProviderModelTestResult,
  AdminProviderModelType,
} from "@/core/admin-settings/model-registry/types";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Locale } from "@/core/i18n";
import { useI18n } from "@/core/i18n/hooks";

const MODEL_REGISTRY_COPY = {
  "en-US": {
    sectionTitle: "Retrieval model providers",
    sectionDescription:
      "Providers and typed embedding/rerank models used by project knowledge bases. Credentials are configured once per provider.",
    refresh: "Refresh",
    addProvider: "Add provider",
    empty: "No model providers yet.",
    loadFailed: "Model providers could not be loaded.",
    knowledgeDisabled:
      "The Knowledge module is not enabled for this deployment, so retrieval model providers are unavailable.",
    keyConfigured: "Key configured",
    keyConfiguredUnverified: "Key configured, unverified",
    timeout: (seconds: number) => `Timeout ${seconds}s`,
    modelCount: (count: number) => `${count} model${count === 1 ? "" : "s"}`,
    endpointFrozen:
      "The endpoint is referenced by knowledge bases. To move to a new endpoint, create a new provider and model, then rebuild each base explicitly.",
    editProvider: "Edit",
    deleteProvider: "Delete",
    addModel: "Add model",
    modelsEmpty: "No models under this provider yet.",
    modelsLoadFailed: "Models could not be loaded.",
    typeEmbedding: "Embedding",
    typeRerank: "Rerank",
    dimension: (value: number) => `Dimension ${value}`,
    maxBatch: (value: number) => `Batch ${value}`,
    active: "Active",
    disabled: "Disabled",
    inUse: "In use",
    enable: "Enable",
    disable: "Disable",
    delete: "Delete",
    test: "Test",
    testing: "Testing…",
    createProviderTitle: "Add model provider",
    editProviderTitle: "Edit model provider",
    providerDialogDescription:
      "The API Key is encrypted at rest. When editing, leave the Key blank to preserve the saved value; changing the endpoint requires a new Key.",
    providerName: "Provider name",
    baseUrl: "Base URL",
    baseUrlFrozenHint:
      "Referenced by knowledge bases; the endpoint cannot be changed in place. Create a new provider for a new endpoint.",
    timeoutSeconds: "Request timeout (seconds)",
    apiKey: "API Key",
    apiKeyCreatePlaceholder: "Enter API Key",
    apiKeyEditPlaceholder: "Leave blank to preserve the saved Key",
    baseUrlChangedNeedsKey:
      "Changing the endpoint requires entering a new API Key.",
    createModelTitle: "Add model",
    modelDialogDescription:
      "Models are typed: embedding models carry a vector dimension; rerank scores are normalized to [0,1].",
    modelType: "Model type",
    modelName: "Model name",
    embeddingDimension: "Embedding dimension",
    modelMaxBatch: "Max batch",
    modelMaxBatchPlaceholder: (embedding: number, rerank: number) =>
      `Default: embedding ${embedding}, rerank ${rerank}`,
    cancel: "Cancel",
    saving: "Saving…",
    save: "Save",
    deleteProviderTitle: "Delete provider",
    deleteProviderDescription: (name: string) =>
      `Delete provider "${name}"? Providers with models cannot be deleted.`,
    deleteModelTitle: "Delete model",
    deleteModelDescription: (name: string) =>
      `Delete model "${name}"? Models referenced by knowledge bases cannot be deleted.`,
    deleting: "Deleting…",
    invalidNumbers: "Numeric fields must be positive whole numbers.",
    dimensionRequired: "Embedding models require a vector dimension.",
    requestFailed: "The request failed.",
  },
  "zh-CN": {
    sectionTitle: "检索模型供应商",
    sectionDescription:
      "供项目知识库使用的供应商与类型化 Embedding/Rerank 模型。凭据按供应商配置一次。",
    refresh: "刷新",
    addProvider: "添加供应商",
    empty: "还没有模型供应商。",
    loadFailed: "无法加载模型供应商。",
    knowledgeDisabled: "当前部署未启用 Knowledge 模块，检索模型供应商不可用。",
    keyConfigured: "Key 已配置",
    keyConfiguredUnverified: "Key 已配置，未验证",
    timeout: (seconds: number) => `超时 ${seconds} 秒`,
    modelCount: (count: number) => `${count} 个模型`,
    endpointFrozen:
      "端点正被知识库引用。如需更换端点，请新建供应商和模型，再逐库显式重建。",
    editProvider: "编辑",
    deleteProvider: "删除",
    addModel: "添加模型",
    modelsEmpty: "该供应商下还没有模型。",
    modelsLoadFailed: "无法加载模型列表。",
    typeEmbedding: "Embedding",
    typeRerank: "Rerank",
    dimension: (value: number) => `维度 ${value}`,
    maxBatch: (value: number) => `批量 ${value}`,
    active: "启用",
    disabled: "停用",
    inUse: "使用中",
    enable: "启用",
    disable: "停用",
    delete: "删除",
    test: "测试",
    testing: "测试中…",
    createProviderTitle: "添加模型供应商",
    editProviderTitle: "编辑模型供应商",
    providerDialogDescription:
      "API Key 加密存储。编辑时留空表示保留已保存的 Key；修改端点必须输入新 Key。",
    providerName: "供应商名称",
    baseUrl: "Base URL",
    baseUrlFrozenHint: "正被知识库引用，端点不可原地修改。更换端点请新建供应商。",
    timeoutSeconds: "请求超时（秒）",
    apiKey: "API Key",
    apiKeyCreatePlaceholder: "输入 API Key",
    apiKeyEditPlaceholder: "留空则保留已保存的 Key",
    baseUrlChangedNeedsKey: "修改端点必须输入新的 API Key。",
    createModelTitle: "添加模型",
    modelDialogDescription:
      "模型按类型区分：Embedding 模型带向量维度；Rerank 分数归一化到 [0,1]。",
    modelType: "模型类型",
    modelName: "模型名称",
    embeddingDimension: "Embedding 维度",
    modelMaxBatch: "最大批量",
    modelMaxBatchPlaceholder: (embedding: number, rerank: number) =>
      `默认：embedding ${embedding}，rerank ${rerank}`,
    cancel: "取消",
    saving: "保存中…",
    save: "保存",
    deleteProviderTitle: "删除供应商",
    deleteProviderDescription: (name: string) =>
      `确定删除供应商「${name}」？有模型的供应商无法删除。`,
    deleteModelTitle: "删除模型",
    deleteModelDescription: (name: string) =>
      `确定删除模型「${name}」？被知识库引用的模型无法删除。`,
    deleting: "删除中…",
    invalidNumbers: "数字字段必须为正整数。",
    dimensionRequired: "Embedding 模型必须填写向量维度。",
    requestFailed: "请求失败。",
  },
} as const;

const DEFAULT_EMBEDDING_MAX_BATCH = 64;
const DEFAULT_RERANK_MAX_BATCH = 32;

function registryCopy(locale: Locale) {
  return MODEL_REGISTRY_COPY[locale] ?? MODEL_REGISTRY_COPY["zh-CN"];
}

type Copy = ReturnType<typeof registryCopy>;

function registryErrorText(error: unknown, copy: Copy): string {
  if (error instanceof AdminModelRegistryApiError && error.serverMessage) {
    return error.serverMessage;
  }
  return copy.requestFailed;
}

function parsePositiveInt(value: string): number | null {
  const parsed = Number.parseInt(value, 10);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

type ProviderDraft = {
  name: string;
  baseUrl: string;
  timeoutSeconds: string;
  apiKey: string;
};

function providerDraftFrom(target: AdminModelProviderItem | null): ProviderDraft {
  return {
    name: target?.name ?? "",
    baseUrl: target?.base_url ?? "https://api.siliconflow.cn/v1",
    timeoutSeconds: String(target?.request_timeout_seconds ?? 30),
    apiKey: "",
  };
}

function ProviderEditorDialog({
  accountId,
  target,
  open,
  onClose,
  copy,
}: {
  accountId: string;
  target: AdminModelProviderItem | null;
  open: boolean;
  onClose: () => void;
  copy: Copy;
}) {
  const create = useCreateAdminModelProvider(accountId);
  const update = useUpdateAdminModelProvider(accountId);
  const [draft, setDraft] = useState<ProviderDraft>(() =>
    providerDraftFrom(target),
  );
  const [validationError, setValidationError] = useState<string | null>(null);
  const pending = create.isPending || update.isPending;
  const submitError = create.error ?? update.error;
  const endpointFrozen = target?.endpoint_frozen ?? false;

  const set = (patch: Partial<ProviderDraft>) =>
    setDraft((current) => ({ ...current, ...patch }));

  const close = () => {
    // Write-only secret: never keep the key in state after the dialog closes.
    setDraft(providerDraftFrom(null));
    setValidationError(null);
    create.reset();
    update.reset();
    onClose();
  };

  const submit = async () => {
    const timeoutSeconds = parsePositiveInt(draft.timeoutSeconds);
    if (timeoutSeconds === null) {
      setValidationError(copy.invalidNumbers);
      return;
    }
    const baseUrl = draft.baseUrl.trim();
    const baseUrlChanged = target !== null && baseUrl !== target.base_url;
    if (baseUrlChanged && draft.apiKey.trim().length === 0) {
      // The server re-verifies anyway; this only explains the rule up front.
      setValidationError(copy.baseUrlChangedNeedsKey);
      return;
    }
    setValidationError(null);
    const apiKey = draft.apiKey;
    setDraft((current) => ({ ...current, apiKey: "" }));
    try {
      if (target === null) {
        await create.execute({
          name: draft.name.trim(),
          base_url: baseUrl,
          request_timeout_seconds: timeoutSeconds,
          api_key: apiKey,
        });
      } else {
        await update.execute({
          providerId: target.id,
          input: {
            name: draft.name.trim(),
            // The frozen endpoint stays untouched; the server rejects
            // attempts regardless of what the UI sends.
            ...(endpointFrozen ? {} : { base_url: baseUrl }),
            request_timeout_seconds: timeoutSeconds,
            ...(apiKey.trim().length > 0 ? { api_key: apiKey } : {}),
          },
        });
      }
      close();
    } catch {
      // The hook records the error for display; the key was already cleared.
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) close();
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {target === null ? copy.createProviderTitle : copy.editProviderTitle}
          </DialogTitle>
          <DialogDescription>{copy.providerDialogDescription}</DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.providerName}</span>
            <Input
              required
              // Mirrors the backend name bound (120 characters).
              maxLength={120}
              value={draft.name}
              onChange={(event) => set({ name: event.target.value })}
            />
          </label>
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.baseUrl}</span>
            <Input
              required
              disabled={endpointFrozen}
              value={draft.baseUrl}
              onChange={(event) => set({ baseUrl: event.target.value })}
            />
            {endpointFrozen ? (
              <span className="text-muted-foreground text-xs">
                {copy.baseUrlFrozenHint}
              </span>
            ) : null}
          </label>
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.timeoutSeconds}</span>
            <Input
              type="number"
              min={1}
              required
              value={draft.timeoutSeconds}
              onChange={(event) => set({ timeoutSeconds: event.target.value })}
            />
          </label>
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.apiKey}</span>
            <Input
              type="password"
              autoComplete="off"
              required={target === null}
              value={draft.apiKey}
              placeholder={
                target === null
                  ? copy.apiKeyCreatePlaceholder
                  : copy.apiKeyEditPlaceholder
              }
              onChange={(event) => set({ apiKey: event.target.value })}
            />
          </label>
          {validationError ? (
            <p role="alert" className="text-destructive text-sm">
              {validationError}
            </p>
          ) : null}
          {submitError ? (
            <p role="alert" className="text-destructive text-sm">
              {registryErrorText(submitError, copy)}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              {copy.cancel}
            </Button>
            <Button type="submit" disabled={pending}>
              {pending ? copy.saving : copy.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type ModelDraft = {
  modelType: AdminProviderModelType;
  modelName: string;
  embeddingDimension: string;
  maxBatch: string;
};

function ModelCreateDialog({
  accountId,
  provider,
  open,
  onClose,
  copy,
}: {
  accountId: string;
  provider: AdminModelProviderItem;
  open: boolean;
  onClose: () => void;
  copy: Copy;
}) {
  const create = useCreateAdminProviderModel(accountId);
  const [draft, setDraft] = useState<ModelDraft>({
    modelType: "embedding",
    modelName: "",
    embeddingDimension: "1024",
    maxBatch: "",
  });
  const [validationError, setValidationError] = useState<string | null>(null);

  const set = (patch: Partial<ModelDraft>) =>
    setDraft((current) => ({ ...current, ...patch }));

  const close = () => {
    setValidationError(null);
    create.reset();
    onClose();
  };

  const submit = () => {
    const maxBatch =
      draft.maxBatch.trim().length > 0
        ? parsePositiveInt(draft.maxBatch)
        : undefined;
    if (maxBatch === null) {
      setValidationError(copy.invalidNumbers);
      return;
    }
    let embeddingDimension: number | undefined;
    if (draft.modelType === "embedding") {
      const parsed = parsePositiveInt(draft.embeddingDimension);
      if (parsed === null) {
        setValidationError(copy.dimensionRequired);
        return;
      }
      embeddingDimension = parsed;
    }
    setValidationError(null);
    create.mutate(
      {
        providerId: provider.id,
        input: {
          model_type: draft.modelType,
          model_name: draft.modelName.trim(),
          ...(embeddingDimension === undefined
            ? {}
            : { embedding_dimension: embeddingDimension }),
          ...(maxBatch === undefined ? {} : { max_batch: maxBatch }),
        },
      },
      { onSuccess: () => close() },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) close();
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {copy.createModelTitle} · {provider.name}
          </DialogTitle>
          <DialogDescription>{copy.modelDialogDescription}</DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.modelType}</span>
            <Select
              value={draft.modelType}
              onValueChange={(value) =>
                set({ modelType: value as AdminProviderModelType })
              }
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="embedding">{copy.typeEmbedding}</SelectItem>
                <SelectItem value="rerank">{copy.typeRerank}</SelectItem>
              </SelectContent>
            </Select>
          </label>
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.modelName}</span>
            <Input
              required
              // Mirrors the backend model-name bound (200 characters).
              maxLength={200}
              value={draft.modelName}
              onChange={(event) => set({ modelName: event.target.value })}
            />
          </label>
          {draft.modelType === "embedding" ? (
            <label className="grid gap-1.5 text-sm">
              <span className="font-medium">{copy.embeddingDimension}</span>
              <Input
                type="number"
                min={1}
                required
                value={draft.embeddingDimension}
                onChange={(event) =>
                  set({ embeddingDimension: event.target.value })
                }
              />
            </label>
          ) : null}
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.modelMaxBatch}</span>
            <Input
              type="number"
              min={1}
              value={draft.maxBatch}
              placeholder={copy.modelMaxBatchPlaceholder(
                DEFAULT_EMBEDDING_MAX_BATCH,
                DEFAULT_RERANK_MAX_BATCH,
              )}
              onChange={(event) => set({ maxBatch: event.target.value })}
            />
          </label>
          {validationError ? (
            <p role="alert" className="text-destructive text-sm">
              {validationError}
            </p>
          ) : null}
          {create.error ? (
            <p role="alert" className="text-destructive text-sm">
              {registryErrorText(create.error, copy)}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              {copy.cancel}
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? copy.saving : copy.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ProviderModelList({
  accountId,
  provider,
  copy,
  onDeleteModel,
}: {
  accountId: string;
  provider: AdminModelProviderItem;
  copy: Copy;
  onDeleteModel: (model: AdminProviderModelItem) => void;
}) {
  const models = useAdminProviderModels(accountId, provider.id);
  const setStatus = useSetAdminProviderModelStatus(accountId);
  const testModel = useTestAdminProviderModel(accountId);
  const [testResults, setTestResults] = useState<
    Record<string, AdminProviderModelTestResult>
  >({});
  const [testingIds, setTestingIds] = useState<ReadonlySet<string>>(new Set());

  // One promise per model: mutate-scoped callbacks only fire for the latest
  // call, so testing another model mid-flight would silently drop the earlier
  // verdict and re-enable its button too early.
  const runConnectionTest = (modelId: string) => {
    setTestingIds((current) => new Set(current).add(modelId));
    void testModel
      .mutateAsync(modelId)
      .then(
        (result) =>
          setTestResults((current) => ({ ...current, [modelId]: result })),
        (error: unknown) =>
          setTestResults((current) => ({
            ...current,
            [modelId]: {
              ok: false,
              message: registryErrorText(error, copy),
              request_id: "",
            },
          })),
      )
      .finally(() =>
        setTestingIds((current) => {
          const next = new Set(current);
          next.delete(modelId);
          return next;
        }),
      );
  };

  if (models.isLoading) {
    return <Skeleton className="mt-3 h-16 rounded-lg" />;
  }
  if (models.error) {
    return (
      <p role="alert" className="text-destructive mt-3 text-xs">
        {copy.modelsLoadFailed}
      </p>
    );
  }
  const items = models.data ?? [];
  if (items.length === 0) {
    return (
      <p className="text-muted-foreground mt-3 rounded-lg border border-dashed px-3 py-4 text-center text-xs">
        {copy.modelsEmpty}
      </p>
    );
  }
  return (
    <ul className="mt-3 grid gap-2" data-testid="admin-provider-model-list">
      {items.map((model) => {
        const testResult = testResults[model.id];
        return (
          <li
            key={model.id}
            className="border-border/70 rounded-lg border px-3 py-2"
          >
            <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center">
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <Badge variant="outline">
                    {model.model_type === "embedding"
                      ? copy.typeEmbedding
                      : copy.typeRerank}
                  </Badge>
                  <span className="text-foreground truncate text-sm font-medium">
                    {model.model_name}
                  </span>
                  <Badge
                    variant={model.status === "active" ? "default" : "secondary"}
                  >
                    {model.status === "active" ? copy.active : copy.disabled}
                  </Badge>
                  {model.in_use ? (
                    <Badge variant="outline">{copy.inUse}</Badge>
                  ) : null}
                </div>
                <p className="text-muted-foreground mt-0.5 text-xs">
                  {model.embedding_dimension !== null
                    ? `${copy.dimension(model.embedding_dimension)} · `
                    : ""}
                  {copy.maxBatch(model.max_batch)}
                </p>
              </div>
              <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={testingIds.has(model.id)}
                  onClick={() => runConnectionTest(model.id)}
                >
                  {testingIds.has(model.id) ? copy.testing : copy.test}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={
                    setStatus.isPending ||
                    (model.status === "active" && model.in_use)
                  }
                  onClick={() =>
                    setStatus.mutate(
                      {
                        modelId: model.id,
                        status:
                          model.status === "active" ? "disabled" : "active",
                      },
                      {
                        onError: (error: unknown) => {
                          // A concurrent in_use conflict must not fail silently.
                          toast.error(registryErrorText(error, copy));
                        },
                      },
                    )
                  }
                >
                  {model.status === "active" ? copy.disable : copy.enable}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="text-destructive"
                  disabled={model.in_use}
                  onClick={() => onDeleteModel(model)}
                >
                  {copy.delete}
                </Button>
              </div>
            </div>
            {testResult ? (
              <p
                role="status"
                className={
                  testResult.ok
                    ? "text-success mt-1.5 text-xs"
                    : "text-destructive mt-1.5 text-xs"
                }
              >
                {testResult.message}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export function AdminModelRegistrySection() {
  const { user } = useAuth();
  const { locale } = useI18n();
  const copy = registryCopy(locale);
  const accountId = user?.id ?? "default";
  const providers = useAdminModelProviders(accountId, user !== null);
  const deleteProvider = useDeleteAdminModelProvider(accountId);
  const deleteModel = useDeleteAdminProviderModel(accountId);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorTarget, setEditorTarget] =
    useState<AdminModelProviderItem | null>(null);
  const [modelDialogProvider, setModelDialogProvider] =
    useState<AdminModelProviderItem | null>(null);
  const [deletingProvider, setDeletingProvider] =
    useState<AdminModelProviderItem | null>(null);
  const [deletingModel, setDeletingModel] =
    useState<AdminProviderModelItem | null>(null);

  const closeProviderDelete = () => {
    setDeletingProvider(null);
    // A stale error from a previous failed delete must not greet the next one.
    deleteProvider.reset();
  };
  const closeModelDelete = () => {
    setDeletingModel(null);
    deleteModel.reset();
  };

  if (!user) return null;

  return (
    <>
      <AdminSection
        title={copy.sectionTitle}
        description={copy.sectionDescription}
        actions={
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void providers.refetch()}
            >
              <RefreshCwIcon aria-hidden className="size-4" />
              {copy.refresh}
            </Button>
            <Button
              type="button"
              size="sm"
              onClick={() => {
                setEditorTarget(null);
                setEditorOpen(true);
              }}
            >
              <PlusIcon aria-hidden className="size-4" />
              {copy.addProvider}
            </Button>
          </div>
        }
      >
        {providers.isLoading ? (
          <Skeleton className="h-40 rounded-xl" />
        ) : isKnowledgeDisabledError(providers.error) ? (
          <p
            data-testid="admin-model-registry-disabled"
            className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-sm"
          >
            {copy.knowledgeDisabled}
          </p>
        ) : providers.error ? (
          <p role="alert" className="text-destructive text-sm">
            {copy.loadFailed}
          </p>
        ) : (providers.data?.length ?? 0) === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-sm">
            {copy.empty}
          </p>
        ) : (
          <ol className="grid gap-3" data-testid="admin-model-provider-list">
            {providers.data?.map((provider) => (
              <li
                key={provider.id}
                className="border-border rounded-xl border p-4"
              >
                <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center">
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="text-foreground truncate font-medium">
                        {provider.name}
                      </span>
                      {provider.api_key_configured ? (
                        <Badge variant="outline">
                          {provider.active_model_count > 0
                            ? copy.keyConfigured
                            : copy.keyConfiguredUnverified}
                        </Badge>
                      ) : null}
                    </div>
                    <p className="text-muted-foreground mt-1 truncate text-xs">
                      {provider.base_url} ·{" "}
                      {copy.timeout(provider.request_timeout_seconds)} ·{" "}
                      {copy.modelCount(provider.model_count)}
                    </p>
                    {provider.endpoint_frozen ? (
                      <p className="text-muted-foreground mt-1 text-xs">
                        {copy.endpointFrozen}
                      </p>
                    ) : null}
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setModelDialogProvider(provider)}
                    >
                      <PlusIcon aria-hidden className="size-4" />
                      {copy.addModel}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setEditorTarget(provider);
                        setEditorOpen(true);
                      }}
                    >
                      {copy.editProvider}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="text-destructive"
                      disabled={provider.model_count > 0}
                      onClick={() => setDeletingProvider(provider)}
                    >
                      {copy.deleteProvider}
                    </Button>
                  </div>
                </div>
                <ProviderModelList
                  accountId={accountId}
                  provider={provider}
                  copy={copy}
                  onDeleteModel={setDeletingModel}
                />
              </li>
            ))}
          </ol>
        )}
      </AdminSection>

      {editorOpen ? (
        <ProviderEditorDialog
          key={editorTarget?.id ?? "create"}
          accountId={accountId}
          target={editorTarget}
          open={editorOpen}
          onClose={() => {
            setEditorOpen(false);
            setEditorTarget(null);
          }}
          copy={copy}
        />
      ) : null}

      {modelDialogProvider ? (
        <ModelCreateDialog
          key={modelDialogProvider.id}
          accountId={accountId}
          provider={modelDialogProvider}
          open={modelDialogProvider !== null}
          onClose={() => setModelDialogProvider(null)}
          copy={copy}
        />
      ) : null}

      <Dialog
        open={deletingProvider !== null}
        onOpenChange={(open) => {
          if (!open) closeProviderDelete();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{copy.deleteProviderTitle}</DialogTitle>
            <DialogDescription>
              {deletingProvider
                ? copy.deleteProviderDescription(deletingProvider.name)
                : ""}
            </DialogDescription>
          </DialogHeader>
          {deleteProvider.error ? (
            <p role="alert" className="text-destructive text-sm">
              {registryErrorText(deleteProvider.error, copy)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={closeProviderDelete}
            >
              {copy.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleteProvider.isPending}
              onClick={() => {
                if (!deletingProvider) return;
                deleteProvider.mutate(deletingProvider.id, {
                  onSuccess: () => closeProviderDelete(),
                });
              }}
            >
              {deleteProvider.isPending ? copy.deleting : copy.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={deletingModel !== null}
        onOpenChange={(open) => {
          if (!open) closeModelDelete();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{copy.deleteModelTitle}</DialogTitle>
            <DialogDescription>
              {deletingModel
                ? copy.deleteModelDescription(deletingModel.model_name)
                : ""}
            </DialogDescription>
          </DialogHeader>
          {deleteModel.error ? (
            <p role="alert" className="text-destructive text-sm">
              {registryErrorText(deleteModel.error, copy)}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={closeModelDelete}>
              {copy.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleteModel.isPending}
              onClick={() => {
                if (!deletingModel) return;
                deleteModel.mutate(deletingModel.id, {
                  onSuccess: () => closeModelDelete(),
                });
              }}
            >
              {deleteModel.isPending ? copy.deleting : copy.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
