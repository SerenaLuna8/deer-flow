"use client";

import { useRef, useState } from "react";
import { toast } from "sonner";

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
import { AdminModelRegistryApiError } from "@/core/admin-settings/model-registry/api";
import {
  useAdminProviderModels,
  useCreateAdminModelProvider,
  useCreateAdminProviderModel,
  useSetAdminProviderModelStatus,
  useTestAdminModelProviderConnection,
  useTestAdminProviderModel,
  useUpdateAdminModelProvider,
} from "@/core/admin-settings/model-registry/hooks";
import type {
  AdminModelProviderItem,
  AdminProviderModelItem,
  AdminProviderModelTestResult,
  AdminProviderModelType,
} from "@/core/admin-settings/model-registry/types";
import {
  ADMIN_MODEL_MAX_INPUT_TOKENS,
  type AdminModelItem,
  type AdminModelProviderAdapterDescriptor,
} from "@/core/admin-settings/models";
import type { Locale } from "@/core/i18n";

const MODEL_REGISTRY_COPY = {
  "en-US": {
    sectionTitle: "Model providers",
    sectionDescription:
      "Providers own the endpoint and the API Key. Text models bind a provider for chat; embedding/rerank models serve project knowledge bases.",
    refresh: "Refresh",
    addProvider: "Add provider",
    empty: "No model providers yet.",
    loadFailed: "Model providers could not be loaded.",
    keyConfigured: "Key configured",
    timeout: (seconds: number) => `Retrieval timeout ${seconds}s`,
    modelCount: (count: number) => `${count} model${count === 1 ? "" : "s"}`,
    activeModelCount: (count: number) => `${count} active`,
    endpointFrozen:
      "The endpoint is referenced by knowledge bases. To move to a new endpoint, create a new provider and model, then rebuild each base explicitly.",
    editProvider: "Edit",
    deleteProvider: "Delete",
    textModelsTitle: "Text models",
    textModelsEmpty: "No text models bound to this provider yet.",
    addTextModel: "Add text model",
    retrievalModelsTitle: "Retrieval models",
    addModel: "Add retrieval model",
    modelsEmpty: "No retrieval models under this provider yet.",
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
      "The provider owns the only API Key. When editing, leave the Key blank to preserve the saved value; changing the endpoint requires a new Key. A Key or endpoint change re-encrypts the Key for every bound text model.",
    providerName: "Provider name",
    baseUrl: "Base URL",
    baseUrlFrozenHint:
      "Referenced by knowledge bases; the endpoint cannot be changed in place. Create a new provider for a new endpoint.",
    timeoutSeconds: "Retrieval request timeout (seconds)",
    timeoutSecondsHint:
      "Applies to embedding/rerank requests only; text-model requests keep their own timeout settings.",
    apiKey: "API Key",
    apiKeyCreatePlaceholder: "Enter API Key",
    apiKeyEditPlaceholder: "Leave blank to preserve the saved Key",
    baseUrlChangedNeedsKey:
      "Changing the endpoint requires entering a new API Key.",
    fanoutWarning: (count: number) =>
      `Saving a Key or endpoint change re-encrypts credentials for ${count} bound model(s); Runs frozen on the old material become stale.`,
    candidateTestTitle: "Connection test (optional, may incur provider charges)",
    candidateTestDescription:
      "Tests the URL and Key above against one explicit text-model target without saving anything. One passing model does not verify every model of this provider; saving without testing is allowed.",
    candidateAdapter: "Adapter",
    candidateModel: "Model name for the test",
    candidateMaxInputTokens: "Maximum input tokens",
    candidateTest: "Test connection",
    candidateTesting: "Testing…",
    candidateNeedsMaterial:
      "Enter the URL, the Key, and a test model target before testing.",
    candidateSucceeded:
      "Connection test succeeded for this URL/Key/model combination.",
    candidateFailed: "Connection test failed.",
    createModelTitle: "Add retrieval model",
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
      `Delete provider "${name}"? A provider with bound text models requires rebinding them first; retrieval models must be deleted first.`,
    deleteModelTitle: "Delete model",
    deleteModelDescription: (name: string) =>
      `Delete model "${name}"? Models referenced by knowledge bases cannot be deleted.`,
    deleting: "Deleting…",
    invalidNumbers: "Numeric fields must be positive whole numbers.",
    dimensionRequired: "Embedding models require a vector dimension.",
    requestFailed: "The request failed.",
  },
  "zh-CN": {
    sectionTitle: "模型供应商",
    sectionDescription:
      "供应商统一持有服务地址与 API Key。文本模型绑定供应商用于会话；Embedding/Rerank 模型供项目知识库使用。",
    refresh: "刷新",
    addProvider: "添加供应商",
    empty: "还没有模型供应商。",
    loadFailed: "无法加载模型供应商。",
    keyConfigured: "Key 已配置",
    timeout: (seconds: number) => `检索超时 ${seconds} 秒`,
    modelCount: (count: number) => `${count} 个模型`,
    activeModelCount: (count: number) => `${count} 个启用`,
    endpointFrozen:
      "端点正被知识库引用。如需更换端点，请新建供应商和模型，再逐库显式重建。",
    editProvider: "编辑",
    deleteProvider: "删除",
    textModelsTitle: "文本模型",
    textModelsEmpty: "该供应商下还没有绑定的文本模型。",
    addTextModel: "添加文本模型",
    retrievalModelsTitle: "检索模型",
    addModel: "添加检索模型",
    modelsEmpty: "该供应商下还没有检索模型。",
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
      "供应商持有唯一的 API Key。编辑时留空表示保留已保存的 Key；修改端点必须输入新 Key。更换 Key 或端点会同时为所有绑定的文本模型重新加密凭据。",
    providerName: "供应商名称",
    baseUrl: "Base URL",
    baseUrlFrozenHint: "正被知识库引用，端点不可原地修改。更换端点请新建供应商。",
    timeoutSeconds: "检索请求超时（秒）",
    timeoutSecondsHint:
      "仅作用于 Embedding/Rerank 请求；文本模型请求使用各自的超时设置。",
    apiKey: "API Key",
    apiKeyCreatePlaceholder: "输入 API Key",
    apiKeyEditPlaceholder: "留空则保留已保存的 Key",
    baseUrlChangedNeedsKey: "修改端点必须输入新的 API Key。",
    fanoutWarning: (count: number) =>
      `保存新的 Key 或端点会同时为 ${count} 个绑定模型重新加密凭据；冻结旧材料的 Run 会失效。`,
    candidateTestTitle: "连接测试（可选，可能产生供应商计费）",
    candidateTestDescription:
      "使用上方的 URL 和 Key 对一个明确选定的文本模型目标发起测试，不保存任何配置。测试一个模型成功不代表该供应商全部模型可用；未测试也可以保存。",
    candidateAdapter: "适配器",
    candidateModel: "测试用模型名称",
    candidateMaxInputTokens: "最大输入 Token",
    candidateTest: "测试连接",
    candidateTesting: "测试中…",
    candidateNeedsMaterial: "测试前请先填写 URL、Key 和测试模型目标。",
    candidateSucceeded: "本次 URL/Key/模型组合连接测试成功。",
    candidateFailed: "连接测试失败。",
    createModelTitle: "添加检索模型",
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
      `确定删除供应商「${name}」？有绑定文本模型的供应商需要先将模型改绑到其他供应商；检索模型需要先删除。`,
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

export function registryCopy(locale: Locale) {
  return MODEL_REGISTRY_COPY[locale] ?? MODEL_REGISTRY_COPY["zh-CN"];
}

export type ModelRegistryCopy = ReturnType<typeof registryCopy>;

export function registryErrorText(
  error: unknown,
  copy: ModelRegistryCopy,
): string {
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

type CandidateTargetDraft = {
  adapter: string;
  model: string;
  maxInputTokens: string;
};

function providerDraftFrom(target: AdminModelProviderItem | null): ProviderDraft {
  return {
    name: target?.name ?? "",
    baseUrl: target?.base_url ?? "",
    timeoutSeconds: String(target?.request_timeout_seconds ?? 30),
    apiKey: "",
  };
}

function candidateTargetFrom(
  boundTextModels: readonly AdminModelItem[],
  descriptors: readonly AdminModelProviderAdapterDescriptor[],
): CandidateTargetDraft {
  // An existing provider prefills the target from a bound model; a new
  // provider starts from the first authorable adapter.
  const bound = boundTextModels[0];
  if (bound) {
    return {
      adapter: bound.provider_adapter,
      model: bound.provider_model,
      maxInputTokens: String(bound.max_input_tokens),
    };
  }
  return {
    adapter: descriptors[0]?.id ?? "",
    model: "",
    maxInputTokens: "64000",
  };
}

export function ProviderEditorDialog({
  accountId,
  target,
  open,
  onClose,
  copy,
  descriptors,
  boundTextModels,
}: {
  accountId: string;
  target: AdminModelProviderItem | null;
  open: boolean;
  onClose: () => void;
  copy: ModelRegistryCopy;
  descriptors: readonly AdminModelProviderAdapterDescriptor[];
  boundTextModels: readonly AdminModelItem[];
}) {
  const create = useCreateAdminModelProvider(accountId);
  const update = useUpdateAdminModelProvider(accountId);
  const candidateTest = useTestAdminModelProviderConnection(accountId);
  const [draft, setDraft] = useState<ProviderDraft>(() =>
    providerDraftFrom(target),
  );
  const [candidateTarget, setCandidateTarget] = useState<CandidateTargetDraft>(
    () => candidateTargetFrom(boundTextModels, descriptors),
  );
  const [validationError, setValidationError] = useState<string | null>(null);
  const [testState, setTestState] = useState<
    "idle" | "pending" | "succeeded" | "failed"
  >("idle");
  const [testError, setTestError] = useState<string | null>(null);
  // Discard late test responses after a newer test, an edit, or a close.
  const testSequence = useRef(0);
  const pending = create.isPending || update.isPending;
  const testPending = testState === "pending";
  const submitError = create.error ?? update.error;
  const endpointFrozen = target?.endpoint_frozen ?? false;
  const boundModelCount = target?.model_count ?? 0;
  const baseUrlChanged =
    target !== null && draft.baseUrl.trim() !== target.base_url;
  const keyEntered = draft.apiKey.trim().length > 0;

  // Any change to the tested material revokes the local success marker.
  const revokeTestResult = () => {
    testSequence.current += 1;
    setTestState("idle");
    setTestError(null);
  };

  const set = (patch: Partial<ProviderDraft>) => {
    revokeTestResult();
    setDraft((current) => ({ ...current, ...patch }));
  };

  const setTarget = (patch: Partial<CandidateTargetDraft>) => {
    revokeTestResult();
    setCandidateTarget((current) => ({ ...current, ...patch }));
  };

  const close = () => {
    // Write-only secret: never keep the key in state after the dialog closes,
    // and orphan any in-flight test response.
    testSequence.current += 1;
    setDraft(providerDraftFrom(null));
    setValidationError(null);
    setTestState("idle");
    setTestError(null);
    create.reset();
    update.reset();
    onClose();
  };

  const runCandidateTest = async () => {
    const baseUrl = draft.baseUrl.trim();
    const apiKey = draft.apiKey;
    const maxInputTokens = parsePositiveInt(candidateTarget.maxInputTokens);
    if (
      baseUrl.length === 0 ||
      apiKey.trim().length === 0 ||
      candidateTarget.adapter.length === 0 ||
      candidateTarget.model.trim().length === 0 ||
      maxInputTokens === null ||
      maxInputTokens > ADMIN_MODEL_MAX_INPUT_TOKENS
    ) {
      setTestError(copy.candidateNeedsMaterial);
      return;
    }
    const sequence = ++testSequence.current;
    setTestState("pending");
    setTestError(null);
    try {
      const result = await candidateTest.execute({
        base_url: baseUrl,
        api_key: apiKey,
        provider_adapter: candidateTarget.adapter,
        provider_model: candidateTarget.model.trim(),
        max_input_tokens: maxInputTokens,
        settings: {},
        supports_vision: false,
      });
      if (sequence !== testSequence.current) return;
      setTestState(result.status);
    } catch (error) {
      if (sequence !== testSequence.current) return;
      setTestState("failed");
      setTestError(registryErrorText(error, copy));
    }
  };

  const submit = async () => {
    const timeoutSeconds = parsePositiveInt(draft.timeoutSeconds);
    if (timeoutSeconds === null) {
      setValidationError(copy.invalidNumbers);
      return;
    }
    const baseUrl = draft.baseUrl.trim();
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
        if (!nextOpen && !testPending) close();
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
              disabled={endpointFrozen || testPending}
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
            <span className="text-muted-foreground text-xs">
              {copy.timeoutSecondsHint}
            </span>
          </label>
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.apiKey}</span>
            <Input
              type="password"
              autoComplete="off"
              required={target === null}
              disabled={testPending}
              value={draft.apiKey}
              placeholder={
                target === null
                  ? copy.apiKeyCreatePlaceholder
                  : copy.apiKeyEditPlaceholder
              }
              onChange={(event) => set({ apiKey: event.target.value })}
            />
          </label>
          {target !== null && (keyEntered || baseUrlChanged) && boundModelCount > 0 ? (
            <p
              data-testid="admin-provider-fanout-warning"
              className="text-muted-foreground text-xs leading-5"
            >
              {copy.fanoutWarning(boundModelCount)}
            </p>
          ) : null}
          <section className="grid gap-3 rounded-lg border p-3">
            <div>
              <h3 className="text-sm font-medium">{copy.candidateTestTitle}</h3>
              <p className="text-muted-foreground mt-1 text-xs leading-5">
                {copy.candidateTestDescription}
              </p>
            </div>
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="grid gap-1.5 text-sm">
                <span className="font-medium">{copy.candidateAdapter}</span>
                <select
                  className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                  aria-label={copy.candidateAdapter}
                  value={candidateTarget.adapter}
                  disabled={testPending}
                  onChange={(event) => setTarget({ adapter: event.target.value })}
                >
                  {descriptors.map((descriptor) => (
                    <option key={descriptor.id} value={descriptor.id}>
                      {descriptor.id}
                    </option>
                  ))}
                </select>
              </label>
              <label className="grid gap-1.5 text-sm">
                <span className="font-medium">{copy.candidateModel}</span>
                <Input
                  value={candidateTarget.model}
                  disabled={testPending}
                  onChange={(event) => setTarget({ model: event.target.value })}
                />
              </label>
              <label className="grid gap-1.5 text-sm">
                <span className="font-medium">
                  {copy.candidateMaxInputTokens}
                </span>
                <Input
                  type="number"
                  min={1}
                  max={ADMIN_MODEL_MAX_INPUT_TOKENS}
                  value={candidateTarget.maxInputTokens}
                  disabled={testPending}
                  onChange={(event) =>
                    setTarget({ maxInputTokens: event.target.value })
                  }
                />
              </label>
            </div>
            <div className="flex items-center gap-3">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={testPending || pending}
                onClick={() => void runCandidateTest()}
              >
                {testPending ? copy.candidateTesting : copy.candidateTest}
              </Button>
              {testState === "succeeded" ? (
                <p role="status" className="text-success text-xs">
                  {copy.candidateSucceeded}
                </p>
              ) : null}
              {testState === "failed" ? (
                <p role="status" className="text-destructive text-xs">
                  {testError ?? copy.candidateFailed}
                </p>
              ) : null}
              {testState === "idle" && testError ? (
                <p role="alert" className="text-destructive text-xs">
                  {testError}
                </p>
              ) : null}
            </div>
          </section>
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
            <Button
              type="button"
              variant="outline"
              disabled={testPending}
              onClick={close}
            >
              {copy.cancel}
            </Button>
            <Button type="submit" disabled={pending || testPending}>
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

export function ModelCreateDialog({
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
  copy: ModelRegistryCopy;
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

export function ProviderModelList({
  accountId,
  provider,
  copy,
  onDeleteModel,
}: {
  accountId: string;
  provider: AdminModelProviderItem;
  copy: ModelRegistryCopy;
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
