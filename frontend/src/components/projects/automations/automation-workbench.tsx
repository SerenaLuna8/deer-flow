"use client";

import { CalendarClockIcon, PlusIcon } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

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
import type {
  Automation,
  AutomationRun,
  CreateAutomationInput,
  UpdateAutomationInput,
} from "@/core/project-automations/types";
import { cn } from "@/lib/utils";

import { AutomationForm, type AutomationAgentOption } from "./automation-form";

export type AutomationPermissions = {
  canRead: boolean;
  canManage: boolean;
  canExecute: boolean;
};

export type AutomationAction =
  | "create"
  | "update"
  | "pause"
  | "resume"
  | "trigger"
  | "delete";

export type AutomationActionFeedback = {
  action: AutomationAction;
  kind: "conflict" | "rate_limit" | "unavailable" | "error";
  message: string;
};

export function automationCanTrigger(status: Automation["status"]): boolean {
  return status === "enabled" || status === "paused";
}

export function automationFeedbackForAction(
  feedback: AutomationActionFeedback | null | undefined,
  action: AutomationAction,
): AutomationActionFeedback | null {
  return feedback?.action === action ? feedback : null;
}

export function automationGlobalFeedback(
  feedback: AutomationActionFeedback | null | undefined,
): AutomationActionFeedback | null {
  return feedback && ["pause", "resume", "trigger"].includes(feedback.action)
    ? feedback
    : null;
}

type MaybePromise<T> = T | Promise<T>;

export async function settleAutomationAction(
  action: () => MaybePromise<unknown>,
): Promise<void> {
  try {
    await action();
  } catch {
    return;
  }
}

function timestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function statusLabel(status: Automation["status"]): string {
  return {
    enabled: "启用",
    paused: "已暂停",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[status];
}

function statusVariant(status: Automation["status"]) {
  if (status === "failed" || status === "cancelled")
    return "destructive" as const;
  if (status === "paused" || status === "completed")
    return "secondary" as const;
  return "default" as const;
}

function runStatusLabel(status: AutomationRun["status"]): string {
  return {
    queued: "排队中",
    launching: "启动中",
    running: "运行中",
    success: "成功",
    failed: "失败",
    skipped: "已跳过",
    interrupted: "已中断",
    cancelled: "已取消",
    rejected: "已拒绝",
  }[status];
}

function AutomationDialogFeedback({
  feedback,
  onRefresh,
}: {
  feedback?: AutomationActionFeedback | null;
  onRefresh?: () => MaybePromise<unknown>;
}) {
  if (!feedback) return null;
  return (
    <div
      role="alert"
      className="border-destructive/30 bg-destructive/5 flex items-center justify-between gap-3 rounded-lg border p-3 text-sm"
    >
      <span>{feedback.message}</span>
      {onRefresh && feedback.kind !== "rate_limit" ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => void settleAutomationAction(onRefresh)}
        >
          刷新
        </Button>
      ) : null}
    </div>
  );
}

export function automationThreadHref(projectSlug: string, threadId: string) {
  return `/projects/${encodeURIComponent(projectSlug)}/chats/${encodeURIComponent(threadId)}`;
}

export function AutomationWorkbench({
  projectSlug,
  automations,
  selected,
  runs,
  permissions,
  schedulerEnabled,
  agents,
  agentsLoading = false,
  agentsError = null,
  agentRuntimeNotice = null,
  automationAgentBlockReasons = {},
  runsLoading = false,
  runsError = null,
  initialThreadId,
  actionFeedback,
  isMutating = false,
  onSelect,
  onCreate,
  onUpdate,
  onPause,
  onResume,
  onTrigger,
  onDelete,
  onRefresh,
  onRefreshRuns,
  onDismissFeedback,
}: {
  projectSlug: string;
  automations: Automation[];
  selected: Automation | null;
  runs: AutomationRun[];
  permissions: AutomationPermissions;
  schedulerEnabled: boolean;
  agents: AutomationAgentOption[];
  agentsLoading?: boolean;
  agentsError?: Error | null;
  agentRuntimeNotice?: string | null;
  automationAgentBlockReasons?: Readonly<Record<string, string>>;
  runsLoading?: boolean;
  runsError?: Error | null;
  initialThreadId?: string;
  actionFeedback?: AutomationActionFeedback | null;
  isMutating?: boolean;
  onSelect: (automation: Automation | null) => void;
  onCreate?: (input: CreateAutomationInput) => MaybePromise<unknown>;
  onUpdate?: (
    automation: Automation,
    input: UpdateAutomationInput,
  ) => MaybePromise<unknown>;
  onPause?: (automation: Automation) => MaybePromise<unknown>;
  onResume?: (automation: Automation) => MaybePromise<unknown>;
  onTrigger?: (automation: Automation) => MaybePromise<unknown>;
  onDelete?: (automation: Automation) => MaybePromise<unknown>;
  onRefresh?: () => MaybePromise<unknown>;
  onRefreshRuns?: () => MaybePromise<unknown>;
  onDismissFeedback?: () => void;
}) {
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<
    "all" | Automation["status"]
  >("all");
  const [scheduleFilter, setScheduleFilter] = useState<
    "all" | Automation["schedule_type"]
  >("all");
  const [createOpen, setCreateOpen] = useState(false);
  const [createGeneration, setCreateGeneration] = useState(0);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return automations.filter(
      (automation) =>
        (statusFilter === "all" || automation.status === statusFilter) &&
        (scheduleFilter === "all" ||
          automation.schedule_type === scheduleFilter) &&
        (!normalizedQuery ||
          automation.title.toLocaleLowerCase().includes(normalizedQuery)),
    );
  }, [automations, query, scheduleFilter, statusFilter]);

  useEffect(() => {
    if (selected && filtered.some(({ id }) => id === selected.id)) return;
    onSelect(filtered[0] ?? null);
  }, [filtered, onSelect, selected]);

  const canCreate = permissions.canExecute && Boolean(onCreate);
  const canEdit = permissions.canManage && Boolean(onUpdate);
  const canDelete = permissions.canManage && Boolean(onDelete);
  const selectedAgentBlockReason = selected
    ? (automationAgentBlockReasons[selected.id] ?? null)
    : null;
  const canOfferTrigger =
    permissions.canExecute &&
    Boolean(selected && automationCanTrigger(selected.status)) &&
    Boolean(onTrigger);
  const canPause =
    permissions.canManage && selected?.status === "enabled" && Boolean(onPause);
  const canOfferResume =
    permissions.canExecute &&
    selected?.status === "paused" &&
    Boolean(onResume);
  const globalFeedback = automationGlobalFeedback(actionFeedback);

  const openCreate = () => {
    onDismissFeedback?.();
    setCreateOpen(true);
  };
  const closeCreate = () => {
    setCreateOpen(false);
    onDismissFeedback?.();
  };
  const openEdit = () => {
    onDismissFeedback?.();
    setEditOpen(true);
  };
  const closeEdit = () => {
    setEditOpen(false);
    onDismissFeedback?.();
  };
  const openDelete = () => {
    onDismissFeedback?.();
    setDeleteOpen(true);
  };
  const closeDelete = () => {
    setDeleteOpen(false);
    onDismissFeedback?.();
  };

  return (
    <main className="mx-auto w-full max-w-[1440px] space-y-5 p-4 sm:p-6 lg:p-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Automation</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            在当前项目与当前账号范围内安排可重复运行的任务。
          </p>
          {initialThreadId ? (
            <p className="text-muted-foreground mt-1 text-sm [overflow-wrap:anywhere]">
              已按 Thread {initialThreadId} 筛选
            </p>
          ) : null}
        </div>
        {canCreate ? (
          <Button type="button" onClick={openCreate}>
            <PlusIcon />
            创建 Automation
          </Button>
        ) : null}
      </header>

      {!schedulerEnabled ? (
        <div
          role="status"
          data-testid="automation-scheduler-disabled"
          className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm"
        >
          自动调度当前已关闭；有执行权限的成员仍可手动运行。
        </div>
      ) : null}

      {globalFeedback ? (
        <div
          role="alert"
          data-testid={`automation-action-${globalFeedback.kind}`}
          className="border-destructive/30 bg-destructive/5 flex flex-col gap-3 rounded-xl border p-4 text-sm sm:flex-row sm:items-center sm:justify-between"
        >
          <span>{globalFeedback.message}</span>
          {onRefresh && globalFeedback.kind !== "rate_limit" ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void settleAutomationAction(onRefresh)}
            >
              刷新
            </Button>
          ) : null}
        </div>
      ) : null}

      {automations.length === 0 ? (
        <section
          data-testid="automation-empty"
          className="bg-card flex min-h-80 flex-col items-center justify-center rounded-2xl border p-8 text-center"
        >
          <CalendarClockIcon className="text-muted-foreground size-10" />
          <h2 className="mt-4 text-lg font-semibold">还没有 Automation</h2>
          <p className="text-muted-foreground mt-2 max-w-md text-sm">
            创建后可按 Cron 或单次时间运行项目 Agent。
          </p>
          {canCreate ? (
            <Button className="mt-5" type="button" onClick={openCreate}>
              创建 Automation
            </Button>
          ) : null}
        </section>
      ) : (
        <>
          <section
            aria-label="Automation 筛选"
            className="bg-muted/25 grid gap-3 rounded-xl border p-3 sm:grid-cols-[minmax(0,1fr)_auto_auto]"
          >
            <Input
              aria-label="搜索 Automation"
              placeholder="按 title 搜索"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <select
              aria-label="按状态筛选"
              className="border-input bg-background h-9 rounded-md border px-3 text-sm"
              value={statusFilter}
              onChange={(event) =>
                setStatusFilter(event.target.value as typeof statusFilter)
              }
            >
              <option value="all">全部状态</option>
              <option value="enabled">启用</option>
              <option value="paused">已暂停</option>
              <option value="completed">已完成</option>
              <option value="failed">失败</option>
              <option value="cancelled">已取消</option>
            </select>
            <select
              aria-label="按计划类型筛选"
              className="border-input bg-background h-9 rounded-md border px-3 text-sm"
              value={scheduleFilter}
              onChange={(event) =>
                setScheduleFilter(event.target.value as typeof scheduleFilter)
              }
            >
              <option value="all">全部类型</option>
              <option value="cron">Cron</option>
              <option value="once">单次</option>
            </select>
          </section>

          {filtered.length === 0 ? (
            <section
              data-testid="automation-filter-empty"
              className="rounded-xl border p-8 text-center"
            >
              <h2 className="font-medium">没有符合筛选条件的 Automation</h2>
              <Button
                type="button"
                className="mt-4"
                variant="outline"
                onClick={() => {
                  setQuery("");
                  setStatusFilter("all");
                  setScheduleFilter("all");
                }}
              >
                清除筛选
              </Button>
            </section>
          ) : (
            <div className="grid min-w-0 items-start gap-4 lg:grid-cols-[minmax(17rem,0.36fr)_minmax(0,0.64fr)]">
              <section
                aria-label="Automation 列表"
                className="bg-card space-y-2 rounded-xl border p-3"
              >
                {filtered.map((automation) => {
                  const isSelected = selected?.id === automation.id;
                  return (
                    <button
                      type="button"
                      key={automation.id}
                      aria-current={isSelected ? "true" : undefined}
                      aria-controls="automation-detail"
                      className={cn(
                        "hover:bg-muted/40 w-full min-w-0 rounded-lg border p-4 text-left transition-colors",
                        isSelected
                          ? "border-foreground bg-muted/50"
                          : "border-border",
                      )}
                      onClick={() => onSelect(automation)}
                    >
                      <span className="block font-medium [overflow-wrap:anywhere]">
                        {automation.title}
                      </span>
                      <span className="mt-2 flex flex-wrap gap-2">
                        <Badge variant="outline">
                          {automation.schedule_type === "cron"
                            ? "Cron"
                            : "单次"}
                        </Badge>
                        <Badge variant={statusVariant(automation.status)}>
                          {statusLabel(automation.status)}
                        </Badge>
                      </span>
                      <span className="text-muted-foreground mt-3 block text-xs">
                        下次运行：{timestamp(automation.next_run_at)}
                      </span>
                    </button>
                  );
                })}
              </section>

              <section
                id="automation-detail"
                data-testid="automation-detail"
                className="bg-card min-w-0 rounded-xl border p-5"
              >
                {selected ? (
                  <div className="space-y-5">
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="text-lg font-semibold [overflow-wrap:anywhere]">
                            {selected.title}
                          </h2>
                          <Badge variant={statusVariant(selected.status)}>
                            {statusLabel(selected.status)}
                          </Badge>
                        </div>
                        <p className="text-muted-foreground mt-1 text-xs">
                          版本 {selected.version}
                        </p>
                      </div>
                      {canEdit ? (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={openEdit}
                        >
                          编辑
                        </Button>
                      ) : null}
                    </div>

                    <dl className="grid gap-4 sm:grid-cols-2">
                      <div>
                        <dt className="text-muted-foreground text-xs">
                          上下文
                        </dt>
                        <dd className="mt-1 text-sm">
                          {selected.context_mode === "reuse_thread"
                            ? "复用 Thread"
                            : "每次新建 Thread"}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground text-xs">时区</dt>
                        <dd className="mt-1 text-sm">{selected.timezone}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground text-xs">
                          下次运行
                        </dt>
                        <dd className="mt-1 text-sm">
                          {timestamp(selected.next_run_at)}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground text-xs">
                          运行次数
                        </dt>
                        <dd className="mt-1 text-sm">{selected.run_count}</dd>
                      </div>
                    </dl>

                    {selected.thread_id ? (
                      <Button asChild size="sm" variant="outline">
                        <Link
                          href={automationThreadHref(
                            projectSlug,
                            selected.thread_id,
                          )}
                        >
                          打开复用 Thread
                        </Link>
                      </Button>
                    ) : null}

                    {selectedAgentBlockReason ? (
                      <p role="alert" className="text-destructive text-sm">
                        {selectedAgentBlockReason}
                      </p>
                    ) : null}

                    <div className="bg-muted/30 rounded-lg p-4 text-sm [overflow-wrap:anywhere] whitespace-pre-wrap">
                      {selected.prompt}
                    </div>

                    {permissions.canManage || permissions.canExecute ? (
                      <div
                        className="flex flex-wrap gap-2"
                        aria-label="Automation 操作"
                      >
                        {canPause ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            disabled={isMutating}
                            onClick={() =>
                              void settleAutomationAction(() =>
                                onPause?.(selected),
                              )
                            }
                          >
                            暂停
                          </Button>
                        ) : null}
                        {canOfferResume ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            disabled={
                              isMutating || Boolean(selectedAgentBlockReason)
                            }
                            title={selectedAgentBlockReason ?? undefined}
                            onClick={() =>
                              void settleAutomationAction(() =>
                                onResume?.(selected),
                              )
                            }
                          >
                            恢复
                          </Button>
                        ) : null}
                        {canOfferTrigger ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            disabled={
                              isMutating || Boolean(selectedAgentBlockReason)
                            }
                            title={selectedAgentBlockReason ?? undefined}
                            onClick={() =>
                              void settleAutomationAction(() =>
                                onTrigger?.(selected),
                              )
                            }
                          >
                            立即运行
                          </Button>
                        ) : null}
                        {canDelete ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="destructive"
                            disabled={isMutating}
                            onClick={openDelete}
                          >
                            删除
                          </Button>
                        ) : null}
                      </div>
                    ) : null}

                    <section
                      aria-labelledby="automation-runs-heading"
                      className="space-y-3 border-t pt-5"
                    >
                      <div className="flex items-center justify-between gap-3">
                        <h3
                          id="automation-runs-heading"
                          className="font-medium"
                        >
                          运行历史
                        </h3>
                        {runsError && onRefreshRuns ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              void settleAutomationAction(onRefreshRuns)
                            }
                          >
                            重试
                          </Button>
                        ) : null}
                      </div>
                      <div
                        data-testid="automation-run-list"
                        className="space-y-2"
                      >
                        {runsLoading ? (
                          <p
                            role="status"
                            className="text-muted-foreground text-sm"
                          >
                            正在加载运行历史…
                          </p>
                        ) : runsError ? (
                          <p role="alert" className="text-destructive text-sm">
                            无法加载运行历史，请重试。
                          </p>
                        ) : runs.length === 0 ? (
                          <p className="text-muted-foreground text-sm">
                            暂无运行记录。
                          </p>
                        ) : (
                          runs.map((run) => (
                            <article
                              key={run.id}
                              className="rounded-lg border p-3 text-sm"
                            >
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <p className="font-medium">
                                  {run.trigger === "manual" ? "手动" : "调度"} ·{" "}
                                  {runStatusLabel(run.status)}
                                </p>
                                <time className="text-muted-foreground text-xs">
                                  {timestamp(run.scheduled_for)}
                                </time>
                              </div>
                              {run.thread_id ? (
                                <Link
                                  className="mt-2 inline-block text-sm underline underline-offset-4"
                                  href={automationThreadHref(
                                    projectSlug,
                                    run.thread_id,
                                  )}
                                >
                                  打开 Thread
                                </Link>
                              ) : null}
                              {run.error_code ? (
                                <p className="text-destructive mt-2 text-xs">
                                  错误代码：{run.error_code}
                                </p>
                              ) : null}
                            </article>
                          ))
                        )}
                      </div>
                    </section>
                  </div>
                ) : (
                  <p className="text-muted-foreground text-sm">
                    请选择一个 Automation。
                  </p>
                )}
              </section>
            </div>
          )}
        </>
      )}

      <Dialog
        open={createOpen}
        onOpenChange={(open) => (open ? openCreate() : closeCreate())}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>创建 Automation</DialogTitle>
            <DialogDescription>
              Agent 来自当前项目可执行 catalog，运行时仍由服务端复核权限。
            </DialogDescription>
          </DialogHeader>
          <AutomationDialogFeedback
            feedback={automationFeedbackForAction(actionFeedback, "create")}
            onRefresh={onRefresh}
          />
          {agentRuntimeNotice ? (
            <p role="alert" className="text-destructive text-sm">
              {agentRuntimeNotice}
            </p>
          ) : null}
          {agentsLoading ? (
            <p role="status">正在加载 Agent…</p>
          ) : agentsError ? (
            <p role="alert" className="text-destructive text-sm">
              无法加载 Agent，请关闭后重试。
            </p>
          ) : (
            <AutomationForm
              key={createGeneration}
              mode="create"
              initialThreadId={initialThreadId}
              agents={agents}
              canSubmit={canCreate && !isMutating && agents.length > 0}
              onCancel={closeCreate}
              onSubmit={async (input) => {
                if (!onCreate || !("context_mode" in input)) return;
                try {
                  await onCreate(input);
                  closeCreate();
                  setCreateGeneration((value) => value + 1);
                } catch {
                  return;
                }
              }}
            />
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={editOpen}
        onOpenChange={(open) => (open ? openEdit() : closeEdit())}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>编辑 Automation</DialogTitle>
            <DialogDescription>
              计划类型、上下文模式与 Agent 在编辑时保持不变。
            </DialogDescription>
          </DialogHeader>
          <AutomationDialogFeedback
            feedback={automationFeedbackForAction(actionFeedback, "update")}
            onRefresh={onRefresh}
          />
          {selected ? (
            <AutomationForm
              key={`${selected.id}:${selected.version}`}
              mode="edit"
              initial={selected}
              agents={
                agents.some(({ id }) => id === selected.agent_asset_id)
                  ? agents
                  : [
                      ...agents,
                      {
                        id: selected.agent_asset_id,
                        scope: selected.agent_scope,
                        displayName: "当前 Agent",
                      },
                    ]
              }
              canSubmit={canEdit && !isMutating}
              onCancel={closeEdit}
              onSubmit={async (input) => {
                if (!onUpdate || "context_mode" in input) return;
                try {
                  await onUpdate(selected, input);
                  closeEdit();
                } catch {
                  return;
                }
              }}
            />
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog
        open={deleteOpen}
        onOpenChange={(open) => (open ? openDelete() : closeDelete())}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除 Automation</DialogTitle>
            <DialogDescription>
              这会删除定义；已有运行历史仍按服务端保留策略处理。
            </DialogDescription>
          </DialogHeader>
          <AutomationDialogFeedback
            feedback={automationFeedbackForAction(actionFeedback, "delete")}
            onRefresh={onRefresh}
          />
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={isMutating}
              onClick={closeDelete}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={isMutating}
              onClick={async () => {
                if (!selected || !onDelete) return;
                try {
                  await onDelete(selected);
                  closeDelete();
                } catch {
                  return;
                }
              }}
            >
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}
