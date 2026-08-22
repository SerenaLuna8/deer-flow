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

type EditorTarget = AdminModelItem | null;

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
    throw new Error("Provider settings 必须是 JSON 对象。");
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

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    let submission: ReturnType<typeof consumeAdminModelEditorSubmission>;
    try {
      submission = consumeAdminModelEditorSubmission(
        new FormData(event.currentTarget),
        provider,
        apiKey,
        () => setApiKey(""),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "模型配置无效。");
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
      setError(caught instanceof Error ? caught.message : "模型保存失败。");
    } finally {
      setPending(false);
    }
  }

  async function testConnection(form: HTMLFormElement) {
    setError(null);
    setTestResult(null);
    if (apiKey === "") {
      setError(
        "连接测试必须临时重新输入 API Key，不能使用数据库中已保存的值。",
      );
      return;
    }
    let submission: ReturnType<typeof consumeAdminModelEditorSubmission>;
    try {
      submission = consumeAdminModelEditorSubmission(
        new FormData(form),
        provider,
        apiKey,
        () => setApiKey(""),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "模型配置无效。");
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
        result.status === "succeeded" ? "连接测试成功。" : "连接测试失败。",
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "连接测试失败。");
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
          <DialogTitle>{target ? "编辑模型" : "新增模型"}</DialogTitle>
          <DialogDescription>
            API Key
            直接由模型域加密保存。编辑时留空表示保留；连接测试必须临时重新输入。
          </DialogDescription>
        </DialogHeader>
        <form className="space-y-4" onSubmit={save}>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="grid gap-2 text-sm">
              显示名称
              <Input
                name="display_name"
                required
                defaultValue={target?.display_name ?? ""}
              />
            </label>
            <label className="grid gap-2 text-sm">
              Provider
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
            Provider Model ID
            <Input
              name="provider_model"
              required
              defaultValue={target?.provider_model ?? ""}
            />
          </label>
          <label className="grid gap-2 text-sm">
            Provider settings JSON
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
              Thinking
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                name="supports_reasoning_effort"
                type="checkbox"
                defaultChecked={target?.supports_reasoning_effort}
              />
              Reasoning effort
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                name="supports_vision"
                type="checkbox"
                defaultChecked={target?.supports_vision}
              />
              Vision
            </label>
          </div>
          <label className="grid gap-2 text-sm">
            API Key
            <Input
              type="password"
              autoComplete="new-password"
              value={apiKey}
              placeholder={
                target?.api_key_configured
                  ? "留空以保留已保存的 Key"
                  : "输入 API Key"
              }
              onChange={(event) => setApiKey(event.target.value)}
            />
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
              {testPending ? "正在测试…" : "测试连接"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={pending || testPending}
              onClick={() => onOpenChange(false)}
            >
              取消
            </Button>
            <Button type="submit" disabled={pending || testPending}>
              {pending ? "正在保存…" : "保存"}
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
            {item.is_default ? <Badge>默认</Badge> : null}
            <Badge variant={item.status === "active" ? "default" : "secondary"}>
              {item.status === "active" ? "启用" : "停用"}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-muted-foreground text-xs">API Key</dt>
            <dd>{item.api_key_configured ? "已配置" : "未配置"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">秘密 revision</dt>
            <dd>{item.secret_revision}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">就绪状态</dt>
            <dd>{item.secret_readiness === "ready" ? "就绪" : "未就绪"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground text-xs">配置 revision</dt>
            <dd>{item.revision}</dd>
          </div>
        </dl>
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="outline" onClick={onEdit}>
            <PencilIcon aria-hidden className="size-4" />
            编辑
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
            {item.status === "active" ? "停用" : "启用"}
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
              设为默认
            </Button>
          ) : null}
          {item.api_key_configured ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setConfirmClear(true)}
            >
              清除 API Key
            </Button>
          ) : null}
        </div>
        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {error instanceof Error ? error.message : "模型操作失败。"}
          </p>
        ) : null}
      </CardContent>
      <Dialog open={confirmClear} onOpenChange={setConfirmClear}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>清除 API Key？</DialogTitle>
            <DialogDescription>
              清除后此模型会变为未就绪，新的 Run
              不能使用它。已经运行或正在准备的 Run 不做额外处理。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setConfirmClear(false)}
            >
              取消
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
              {clear.isPending ? "正在清除…" : "确认清除"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

export function AdminModelSettingsPage() {
  const { user } = useAuth();
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
      <AdminPageHeader title="模型配置" />
      <AdminSection title="系统模型">
        <div className="mb-5 flex flex-col gap-3 sm:flex-row">
          <label className="relative min-w-0 flex-1">
            <SearchIcon
              aria-hidden
              className="text-muted-foreground absolute top-2.5 left-3 size-4"
            />
            <Input
              className="pl-9"
              value={search}
              aria-label="搜索模型"
              placeholder="搜索名称、Provider 或模型 ID"
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <select
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
            value={status}
            onChange={(event) => setStatus(event.target.value as typeof status)}
          >
            <option value="all">全部状态</option>
            <option value="active">启用</option>
            <option value="suspended">停用</option>
          </select>
          <Button
            type="button"
            variant="outline"
            disabled={catalog.isFetching}
            onClick={() => void catalog.refetch()}
          >
            <RefreshCwIcon aria-hidden className="size-4" />
            刷新
          </Button>
          <Button
            type="button"
            onClick={() => {
              setTarget(null);
              setEditorOpen(true);
            }}
          >
            <PlusIcon aria-hidden className="size-4" />
            新增模型
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
                : "模型目录读取失败。"}
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={() => void catalog.refetch()}
            >
              重试
            </Button>
          </div>
        ) : items.length === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed p-8 text-center text-sm">
            没有匹配的模型。
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
