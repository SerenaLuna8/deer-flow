"use client";

import { PlusIcon, RefreshCwIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import {
  AdminPage,
  AdminPageHeader,
  AdminSection,
} from "@/components/admin/ui/admin-page";
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
import { Skeleton } from "@/components/ui/skeleton";
import { AdminKnowledgeApiError } from "@/core/admin-settings/knowledge/api";
import {
  useAdminKnowledgeModels,
  useCreateAdminKnowledgeModel,
  useDeleteAdminKnowledgeModel,
  useTestAdminKnowledgeModel,
  useUpdateAdminKnowledgeModel,
} from "@/core/admin-settings/knowledge/hooks";
import type {
  AdminKnowledgeModelItem,
  AdminKnowledgeModelTestResult,
} from "@/core/admin-settings/knowledge/types";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Locale } from "@/core/i18n";
import { useI18n } from "@/core/i18n/hooks";

const KNOWLEDGE_SETTINGS_COPY = {
  "en-US": {
    pageTitle: "Knowledge model settings",
    sectionTitle: "Embedding and rerank configurations",
    sectionDescription:
      "SiliconFlow-compatible embedding and reranker configurations used by project knowledge bases.",
    refresh: "Refresh",
    add: "Add configuration",
    empty: "No knowledge model configurations yet.",
    loadFailed: "Knowledge model configurations could not be loaded.",
    active: "Active",
    disabled: "Disabled",
    inUse: "In use",
    enable: "Enable",
    disable: "Disable",
    edit: "Edit",
    delete: "Delete",
    test: "Test connection",
    testing: "Testing…",
    createTitle: "Add knowledge model configuration",
    editTitle: "Edit knowledge model configuration",
    dialogDescription:
      "The API Key is encrypted at rest. When editing, leave the Key blank to preserve the saved value.",
    displayName: "Display name",
    baseUrl: "Base URL",
    embeddingModel: "Embedding model",
    embeddingDimension: "Embedding dimension",
    embeddingMaxBatch: "Embedding max batch",
    rerankerModel: "Reranker model",
    rerankerMaxBatch: "Reranker max batch",
    timeoutSeconds: "Request timeout (seconds)",
    apiKey: "API Key",
    apiKeyCreatePlaceholder: "Enter API Key",
    apiKeyEditPlaceholder: "Leave blank to preserve the saved Key",
    cancel: "Cancel",
    saving: "Saving…",
    save: "Save",
    deleteTitle: "Delete configuration",
    deleteDescription: (name: string) =>
      `Delete "${name}"? This cannot be undone.`,
    deleteBlocked:
      "This configuration is referenced by knowledge bases and cannot be deleted.",
    disableBlocked:
      "This configuration is referenced by knowledge bases and cannot be disabled.",
    deleting: "Deleting…",
    invalidNumbers: "Numeric fields must be positive whole numbers.",
    requestFailed: "The request failed.",
  },
  "zh-CN": {
    pageTitle: "Knowledge 模型设置",
    sectionTitle: "Embedding 与重排配置",
    sectionDescription:
      "供项目知识库使用的 SiliconFlow 兼容 Embedding 与 Reranker 配置。",
    refresh: "刷新",
    add: "新增配置",
    empty: "还没有 Knowledge 模型配置。",
    loadFailed: "无法加载 Knowledge 模型配置。",
    active: "启用",
    disabled: "停用",
    inUse: "使用中",
    enable: "启用",
    disable: "停用",
    edit: "编辑",
    delete: "删除",
    test: "测试连接",
    testing: "测试中…",
    createTitle: "新增 Knowledge 模型配置",
    editTitle: "编辑 Knowledge 模型配置",
    dialogDescription: "API Key 加密存储。编辑时留空表示保留已保存的 Key。",
    displayName: "显示名称",
    baseUrl: "Base URL",
    embeddingModel: "Embedding 模型",
    embeddingDimension: "Embedding 维度",
    embeddingMaxBatch: "Embedding 最大批量",
    rerankerModel: "Reranker 模型",
    rerankerMaxBatch: "Reranker 最大批量",
    timeoutSeconds: "请求超时（秒）",
    apiKey: "API Key",
    apiKeyCreatePlaceholder: "输入 API Key",
    apiKeyEditPlaceholder: "留空则保留已保存的 Key",
    cancel: "取消",
    saving: "保存中…",
    save: "保存",
    deleteTitle: "删除配置",
    deleteDescription: (name: string) =>
      `确定删除「${name}」？此操作不可撤销。`,
    deleteBlocked: "该配置正被知识库引用，无法删除。",
    disableBlocked: "该配置正被知识库引用，无法停用。",
    deleting: "删除中…",
    invalidNumbers: "数字字段必须为正整数。",
    requestFailed: "请求失败。",
  },
} as const;

function knowledgeSettingsCopy(locale: Locale) {
  return KNOWLEDGE_SETTINGS_COPY[locale] ?? KNOWLEDGE_SETTINGS_COPY["zh-CN"];
}

type Copy = ReturnType<typeof knowledgeSettingsCopy>;

function adminErrorText(error: unknown, copy: Copy): string {
  if (error instanceof AdminKnowledgeApiError && error.serverMessage) {
    return error.serverMessage;
  }
  return copy.requestFailed;
}

type EditorTarget = AdminKnowledgeModelItem | null;

type EditorDraft = {
  displayName: string;
  baseUrl: string;
  embeddingModel: string;
  embeddingDimension: string;
  embeddingMaxBatch: string;
  rerankerModel: string;
  rerankerMaxBatch: string;
  timeoutSeconds: string;
  apiKey: string;
};

function draftFrom(target: EditorTarget): EditorDraft {
  return {
    displayName: target?.display_name ?? "",
    baseUrl: target?.base_url ?? "https://api.siliconflow.cn/v1",
    embeddingModel: target?.embedding_model ?? "",
    embeddingDimension: String(target?.embedding_dimension ?? 1024),
    embeddingMaxBatch: String(target?.embedding_max_batch ?? 64),
    rerankerModel: target?.reranker_model ?? "",
    rerankerMaxBatch: String(target?.reranker_max_batch ?? 32),
    timeoutSeconds: String(target?.request_timeout_seconds ?? 30),
    apiKey: "",
  };
}

function parsePositiveInt(value: string): number | null {
  const parsed = Number.parseInt(value, 10);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function KnowledgeModelEditorDialog({
  accountId,
  target,
  open,
  onClose,
  copy,
}: {
  accountId: string;
  target: EditorTarget;
  open: boolean;
  onClose: () => void;
  copy: Copy;
}) {
  const create = useCreateAdminKnowledgeModel(accountId);
  const update = useUpdateAdminKnowledgeModel(accountId);
  const [draft, setDraft] = useState<EditorDraft>(() => draftFrom(target));
  const [validationError, setValidationError] = useState<string | null>(null);
  const pending = create.isPending || update.isPending;
  const submitError = create.error ?? update.error;

  const set = (patch: Partial<EditorDraft>) =>
    setDraft((current) => ({ ...current, ...patch }));

  const close = () => {
    // Write-only secret: never keep the key in state after the dialog closes.
    setDraft(draftFrom(null));
    setValidationError(null);
    create.reset();
    update.reset();
    onClose();
  };

  const submit = async () => {
    const embeddingDimension = parsePositiveInt(draft.embeddingDimension);
    const embeddingMaxBatch = parsePositiveInt(draft.embeddingMaxBatch);
    const rerankerMaxBatch = parsePositiveInt(draft.rerankerMaxBatch);
    const timeoutSeconds = parsePositiveInt(draft.timeoutSeconds);
    if (
      embeddingDimension === null ||
      embeddingMaxBatch === null ||
      rerankerMaxBatch === null ||
      timeoutSeconds === null
    ) {
      setValidationError(copy.invalidNumbers);
      return;
    }
    setValidationError(null);
    const apiKey = draft.apiKey;
    setDraft((current) => ({ ...current, apiKey: "" }));
    try {
      if (target === null) {
        await create.execute({
          display_name: draft.displayName.trim(),
          base_url: draft.baseUrl.trim(),
          embedding_model: draft.embeddingModel.trim(),
          embedding_dimension: embeddingDimension,
          embedding_max_batch: embeddingMaxBatch,
          reranker_model: draft.rerankerModel.trim(),
          reranker_max_batch: rerankerMaxBatch,
          request_timeout_seconds: timeoutSeconds,
          api_key: apiKey,
        });
      } else {
        await update.execute({
          configurationId: target.id,
          input: {
            display_name: draft.displayName.trim(),
            base_url: draft.baseUrl.trim(),
            embedding_model: draft.embeddingModel.trim(),
            embedding_dimension: embeddingDimension,
            embedding_max_batch: embeddingMaxBatch,
            reranker_model: draft.rerankerModel.trim(),
            reranker_max_batch: rerankerMaxBatch,
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

  const numberField = (
    label: string,
    key: keyof Pick<
      EditorDraft,
      | "embeddingDimension"
      | "embeddingMaxBatch"
      | "rerankerMaxBatch"
      | "timeoutSeconds"
    >,
  ) => (
    <label className="grid gap-1.5 text-sm">
      <span className="font-medium">{label}</span>
      <Input
        type="number"
        min={1}
        required
        value={draft[key]}
        onChange={(event) => set({ [key]: event.target.value })}
      />
    </label>
  );

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) close();
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>
            {target === null ? copy.createTitle : copy.editTitle}
          </DialogTitle>
          <DialogDescription>{copy.dialogDescription}</DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.displayName}</span>
            <Input
              required
              // Mirrors the backend display-name bound (120 characters).
              maxLength={120}
              value={draft.displayName}
              onChange={(event) => set({ displayName: event.target.value })}
            />
          </label>
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{copy.baseUrl}</span>
            <Input
              required
              value={draft.baseUrl}
              onChange={(event) => set({ baseUrl: event.target.value })}
            />
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1.5 text-sm">
              <span className="font-medium">{copy.embeddingModel}</span>
              <Input
                required
                value={draft.embeddingModel}
                onChange={(event) =>
                  set({ embeddingModel: event.target.value })
                }
              />
            </label>
            {numberField(copy.embeddingDimension, "embeddingDimension")}
            {numberField(copy.embeddingMaxBatch, "embeddingMaxBatch")}
            <label className="grid gap-1.5 text-sm">
              <span className="font-medium">{copy.rerankerModel}</span>
              <Input
                required
                value={draft.rerankerModel}
                onChange={(event) => set({ rerankerModel: event.target.value })}
              />
            </label>
            {numberField(copy.rerankerMaxBatch, "rerankerMaxBatch")}
            {numberField(copy.timeoutSeconds, "timeoutSeconds")}
          </div>
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
              {adminErrorText(submitError, copy)}
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

export function AdminKnowledgeSettingsPage() {
  const { user } = useAuth();
  const { locale } = useI18n();
  const copy = knowledgeSettingsCopy(locale);
  const accountId = user?.id ?? "default";
  const models = useAdminKnowledgeModels(accountId, user !== null);
  const updateModel = useUpdateAdminKnowledgeModel(accountId);
  const deleteModel = useDeleteAdminKnowledgeModel(accountId);
  const testModel = useTestAdminKnowledgeModel(accountId);
  const [editorOpen, setEditorOpen] = useState(false);
  const [target, setTarget] = useState<EditorTarget>(null);
  const [deleting, setDeleting] = useState<AdminKnowledgeModelItem | null>(
    null,
  );
  const [testResults, setTestResults] = useState<
    Record<string, AdminKnowledgeModelTestResult>
  >({});
  const [testingIds, setTestingIds] = useState<ReadonlySet<string>>(new Set());
  const closeDeleteDialog = () => {
    setDeleting(null);
    // A stale error from a previous failed delete must not greet the next one.
    deleteModel.reset();
  };

  // One promise per configuration: mutate-scoped callbacks only fire for the
  // latest call, so testing another configuration mid-flight would silently
  // drop the earlier verdict and re-enable its button too early.
  const runConnectionTest = (configurationId: string) => {
    setTestingIds((current) => new Set(current).add(configurationId));
    void testModel
      .mutateAsync(configurationId)
      .then(
        (result) =>
          setTestResults((current) => ({
            ...current,
            [configurationId]: result,
          })),
        (error: unknown) =>
          setTestResults((current) => ({
            ...current,
            [configurationId]: {
              ok: false,
              message: adminErrorText(error, copy),
              request_id: "",
            },
          })),
      )
      .finally(() =>
        setTestingIds((current) => {
          const next = new Set(current);
          next.delete(configurationId);
          return next;
        }),
      );
  };

  if (!user) return null;

  return (
    <AdminPage>
      <AdminPageHeader title={copy.pageTitle} />
      <AdminSection
        title={copy.sectionTitle}
        description={copy.sectionDescription}
        actions={
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void models.refetch()}
            >
              <RefreshCwIcon aria-hidden className="size-4" />
              {copy.refresh}
            </Button>
            <Button
              type="button"
              size="sm"
              onClick={() => {
                setTarget(null);
                setEditorOpen(true);
              }}
            >
              <PlusIcon aria-hidden className="size-4" />
              {copy.add}
            </Button>
          </div>
        }
      >
        {models.isLoading ? (
          <Skeleton className="h-40 rounded-xl" />
        ) : models.error ? (
          <p role="alert" className="text-destructive text-sm">
            {copy.loadFailed}
          </p>
        ) : (models.data?.items.length ?? 0) === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-sm">
            {copy.empty}
          </p>
        ) : (
          <ol className="grid gap-3" data-testid="admin-knowledge-model-list">
            {models.data?.items.map((item) => {
              const testResult = testResults[item.id];
              return (
                <li
                  key={item.id}
                  className="border-border rounded-xl border p-4"
                >
                  <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center">
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="text-foreground truncate font-medium">
                          {item.display_name}
                        </span>
                        <Badge
                          variant={
                            item.status === "active" ? "default" : "secondary"
                          }
                        >
                          {item.status === "active"
                            ? copy.active
                            : copy.disabled}
                        </Badge>
                        {item.in_use ? (
                          <Badge variant="outline">{copy.inUse}</Badge>
                        ) : null}
                      </div>
                      <p className="text-muted-foreground mt-1 truncate text-xs">
                        {item.base_url} · {item.embedding_model} (
                        {item.embedding_dimension}) · {item.reranker_model}
                      </p>
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={testingIds.has(item.id)}
                        onClick={() => runConnectionTest(item.id)}
                      >
                        {testingIds.has(item.id) ? copy.testing : copy.test}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={
                          updateModel.isPending ||
                          (item.status === "active" && item.in_use)
                        }
                        title={
                          item.status === "active" && item.in_use
                            ? copy.disableBlocked
                            : undefined
                        }
                        onClick={() =>
                          void updateModel
                            .execute({
                              configurationId: item.id,
                              input: {
                                status:
                                  item.status === "active"
                                    ? "disabled"
                                    : "active",
                              },
                            })
                            .catch((error: unknown) => {
                              // A concurrent in_use conflict (409) must not
                              // fail silently.
                              toast.error(adminErrorText(error, copy));
                            })
                        }
                      >
                        {item.status === "active" ? copy.disable : copy.enable}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setTarget(item);
                          setEditorOpen(true);
                        }}
                      >
                        {copy.edit}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="text-destructive"
                        disabled={item.in_use}
                        title={item.in_use ? copy.deleteBlocked : undefined}
                        onClick={() => setDeleting(item)}
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
                          ? "text-success mt-2 text-xs"
                          : "text-destructive mt-2 text-xs"
                      }
                    >
                      {testResult.message}
                    </p>
                  ) : null}
                </li>
              );
            })}
          </ol>
        )}
      </AdminSection>

      {editorOpen ? (
        <KnowledgeModelEditorDialog
          key={target?.id ?? "create"}
          accountId={accountId}
          target={target}
          open={editorOpen}
          onClose={() => {
            setEditorOpen(false);
            setTarget(null);
          }}
          copy={copy}
        />
      ) : null}

      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) closeDeleteDialog();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{copy.deleteTitle}</DialogTitle>
            <DialogDescription>
              {deleting ? copy.deleteDescription(deleting.display_name) : ""}
            </DialogDescription>
          </DialogHeader>
          {deleteModel.error ? (
            <p role="alert" className="text-destructive text-sm">
              {adminErrorText(deleteModel.error, copy)}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={closeDeleteDialog}>
              {copy.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleteModel.isPending}
              onClick={() => {
                if (!deleting) return;
                deleteModel.mutate(deleting.id, {
                  onSuccess: () => closeDeleteDialog(),
                });
              }}
            >
              {deleteModel.isPending ? copy.deleting : copy.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </AdminPage>
  );
}
