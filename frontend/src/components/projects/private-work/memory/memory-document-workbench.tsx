"use client";

import {
  BrainIcon,
  Clock3Icon,
  HistoryIcon,
  Loader2Icon,
  RefreshCwIcon,
  RotateCcwIcon,
  SparklesIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import type {
  MemoryDocument,
  MemoryVersionDetail,
  MemoryVersionSummary,
} from "@/core/private-work/memory";
import { cn } from "@/lib/utils";

type QueryState<T> = {
  data: T | undefined;
  error: Error | null;
  isLoading: boolean;
  retry: () => void;
};

export type MemoryDocumentWorkbenchProps = {
  document: QueryState<MemoryDocument>;
  versions: QueryState<readonly MemoryVersionSummary[]> & {
    page: number;
    hasNext: boolean;
    previous: () => void;
    next: () => void;
  };
  detail: QueryState<MemoryVersionDetail> & {
    selectedVersion: number | null;
    select: (version: number | null) => void;
  };
  actions: {
    canDream: boolean;
    canRestore: boolean;
    dreaming: boolean;
    restoringVersion: number | null;
    dream: () => Promise<void>;
    restore: (version: number) => Promise<void>;
  };
};

function localizedCopy(locale: string) {
  if (locale === "zh-CN") {
    return {
      description: "一份由对话逐步整理而成、仅属于你的长期记忆文档。",
      currentTitle: "当前记忆文档",
      version: (value: number) => `版本 ${value}`,
      updated: "最近更新",
      neverUpdated: "尚未整理",
      emptyTitle: "还没有长期记忆",
      emptyDescription:
        "继续对话后，ActWeave 会先记录待整理内容，再通过 Dream 汇入这份文档。",
      pending: "待整理",
      pendingUnit: (value: number) => `${value} 条`,
      dream: "立即整理",
      dreaming: "正在整理",
      dreamRunning: "已有整理任务运行中",
      dreamUnavailable: "你当前没有运行整理任务的权限。",
      recordsTitle: "整理记录",
      recordsDescription: "查看每次 Dream 或恢复操作对文档产生的真实变化。",
      noRecords: "还没有整理记录。",
      autoDream: "自动整理",
      manualDream: "手动整理",
      restoreTrigger: "版本恢复",
      handled: (value: number) => `处理 ${value} 条`,
      changed: "有内容变化",
      unchanged: "内容未变化",
      detailsTitle: (value: number) => `版本 ${value}`,
      detailsDescription: "查看该版本保存的完整文档和相对上一版本的真实 diff。",
      diffTitle: "文档变化",
      documentTitle: "该版本文档",
      noDiff: "这次整理没有改变文档内容。",
      restore: "恢复此版本",
      restoring: "正在恢复",
      restoreTitle: (value: number) => `恢复版本 ${value}？`,
      restoreDescription:
        "恢复会把这个历史内容写成一个新的当前版本，不会删除之后的整理记录。",
      cancel: "取消",
      confirmRestore: "确认恢复",
      loadFailed: "无法加载记忆",
      versionsFailed: "无法加载整理记录",
      detailFailed: "无法加载版本详情",
      retry: "重试",
      previous: "上一页",
      next: "下一页",
    };
  }
  return {
    description:
      "A private long-term document gradually organized from your conversations.",
    currentTitle: "Current memory document",
    version: (value: number) => `Version ${value}`,
    updated: "Last updated",
    neverUpdated: "Not organized yet",
    emptyTitle: "No long-term memory yet",
    emptyDescription:
      "As you keep talking, ActWeave records pending notes and Dream organizes them into this document.",
    pending: "Pending",
    pendingUnit: (value: number) =>
      `${value} ${value === 1 ? "item" : "items"}`,
    dream: "Organize now",
    dreaming: "Organizing",
    dreamRunning: "An organization job is already running",
    dreamUnavailable: "You do not have permission to run an organization job.",
    recordsTitle: "Organization history",
    recordsDescription:
      "Inspect the real document change produced by each Dream or restore.",
    noRecords: "No organization history yet.",
    autoDream: "Automatic Dream",
    manualDream: "Manual Dream",
    restoreTrigger: "Version restore",
    handled: (value: number) =>
      `Processed ${value} ${value === 1 ? "item" : "items"}`,
    changed: "Document changed",
    unchanged: "No content change",
    detailsTitle: (value: number) => `Version ${value}`,
    detailsDescription:
      "View the saved document and its real diff from the preceding version.",
    diffTitle: "Document change",
    documentTitle: "Document at this version",
    noDiff: "This organization run did not change the document.",
    restore: "Restore this version",
    restoring: "Restoring",
    restoreTitle: (value: number) => `Restore version ${value}?`,
    restoreDescription:
      "Restore writes this historical content as a new current version. Later history remains available.",
    cancel: "Cancel",
    confirmRestore: "Restore",
    loadFailed: "Memory could not be loaded",
    versionsFailed: "Organization history could not be loaded",
    detailFailed: "Version details could not be loaded",
    retry: "Retry",
    previous: "Previous",
    next: "Next",
  };
}

function ErrorState({
  title,
  retryLabel,
  onRetry,
}: {
  title: string;
  retryLabel: string;
  onRetry: () => void;
}) {
  return (
    <Alert variant="destructive">
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-3"
          onClick={onRetry}
        >
          <RefreshCwIcon className="size-4" />
          {retryLabel}
        </Button>
      </AlertDescription>
    </Alert>
  );
}

function formatDate(value: string, locale: string) {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function MemoryDocumentWorkbench({
  document,
  versions,
  detail,
  actions,
}: MemoryDocumentWorkbenchProps) {
  const { locale, t } = useI18n();
  const copy = useMemo(() => localizedCopy(locale), [locale]);
  const [restoreConfirmation, setRestoreConfirmation] = useState<number | null>(
    null,
  );
  const selectedVersion = detail.selectedVersion;

  const triggerLabel = (trigger: MemoryVersionSummary["trigger"]) => {
    if (trigger === "auto_dream") return copy.autoDream;
    if (trigger === "manual_dream") return copy.manualDream;
    return copy.restoreTrigger;
  };

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 px-4 py-6 sm:px-6 lg:py-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-violet-600 dark:text-violet-300">
            <BrainIcon className="size-4" />
            {t.settings.memory.title}
          </div>
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
            {copy.currentTitle}
          </h1>
          <p className="text-muted-foreground mt-2 max-w-2xl text-sm">
            {copy.description}
          </p>
        </div>
        {document.data ? (
          <div className="flex shrink-0 items-center gap-2">
            <div className="border-border/70 bg-muted/30 rounded-xl border px-3 py-2 text-right">
              <p className="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
                {copy.pending}
              </p>
              <p className="text-sm font-semibold tabular-nums">
                {copy.pendingUnit(document.data.pendingCount)}
              </p>
            </div>
            <Button
              type="button"
              disabled={
                !actions.canDream ||
                actions.dreaming ||
                document.data.dreamRunning ||
                document.data.pendingCount === 0
              }
              onClick={() => void actions.dream().catch(() => undefined)}
            >
              {actions.dreaming ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                <SparklesIcon className="size-4" />
              )}
              {actions.dreaming ? copy.dreaming : copy.dream}
            </Button>
          </div>
        ) : null}
      </header>

      {document.isLoading ? (
        <Card>
          <CardHeader>
            <Skeleton className="h-6 w-40" />
          </CardHeader>
          <CardContent className="space-y-3">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
            <Skeleton className="h-4 w-2/3" />
          </CardContent>
        </Card>
      ) : document.error ? (
        <ErrorState
          title={copy.loadFailed}
          retryLabel={copy.retry}
          onRetry={document.retry}
        />
      ) : document.data ? (
        <Card className="overflow-hidden border-violet-500/15 shadow-sm">
          <CardHeader className="border-b bg-gradient-to-r from-violet-500/[0.07] via-transparent to-transparent">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle className="text-base">
                {copy.version(document.data.version)}
              </CardTitle>
              <div className="text-muted-foreground flex items-center gap-1.5 text-xs">
                <Clock3Icon className="size-3.5" />
                <span>{copy.updated}</span>
                <span>·</span>
                <span>
                  {document.data.updatedAt
                    ? formatDate(document.data.updatedAt, locale)
                    : copy.neverUpdated}
                </span>
              </div>
            </div>
            {document.data.dreamRunning ? (
              <p className="text-muted-foreground mt-2 flex items-center gap-2 text-xs">
                <Loader2Icon className="size-3.5 animate-spin" />
                {copy.dreamRunning}
              </p>
            ) : !actions.canDream ? (
              <p className="text-muted-foreground mt-2 text-xs">
                {copy.dreamUnavailable}
              </p>
            ) : null}
          </CardHeader>
          <CardContent className="p-0">
            {document.data.content ? (
              <article className="prose prose-sm dark:prose-invert max-w-none px-5 py-6 whitespace-pre-wrap sm:px-7">
                {document.data.content}
              </article>
            ) : (
              <div className="flex min-h-56 flex-col items-center justify-center px-6 py-12 text-center">
                <div className="mb-4 rounded-2xl bg-violet-500/10 p-3 text-violet-600 dark:text-violet-300">
                  <SparklesIcon className="size-6" />
                </div>
                <h2 className="font-medium">{copy.emptyTitle}</h2>
                <p className="text-muted-foreground mt-2 max-w-md text-sm">
                  {copy.emptyDescription}
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      ) : null}

      <section aria-labelledby="memory-history-title" className="space-y-3">
        <div>
          <h2
            id="memory-history-title"
            className="flex items-center gap-2 text-lg font-semibold"
          >
            <HistoryIcon className="size-4.5" />
            {copy.recordsTitle}
          </h2>
          <p className="text-muted-foreground mt-1 text-sm">
            {copy.recordsDescription}
          </p>
        </div>

        {versions.isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : versions.error ? (
          <ErrorState
            title={copy.versionsFailed}
            retryLabel={copy.retry}
            onRetry={versions.retry}
          />
        ) : versions.data?.length ? (
          <div className="overflow-hidden rounded-xl border">
            {versions.data.map((version) => (
              <button
                key={version.version}
                type="button"
                className="hover:bg-muted/50 focus-visible:ring-ring flex w-full items-center justify-between gap-4 border-b px-4 py-3 text-left outline-none last:border-b-0 focus-visible:ring-2 focus-visible:ring-inset"
                onClick={() => detail.select(version.version)}
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">
                      {copy.version(version.version)}
                    </span>
                    <Badge variant="secondary">
                      {triggerLabel(version.trigger)}
                    </Badge>
                    <Badge
                      variant="outline"
                      className={cn(
                        version.changed &&
                          "border-emerald-500/30 text-emerald-700 dark:text-emerald-300",
                      )}
                    >
                      {version.changed ? copy.changed : copy.unchanged}
                    </Badge>
                  </div>
                  <div className="text-muted-foreground mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs">
                    <span>{formatDate(version.createdAt, locale)}</span>
                    {version.historyCount !== null ? (
                      <span>{copy.handled(version.historyCount)}</span>
                    ) : null}
                  </div>
                </div>
                <span aria-hidden="true" className="text-muted-foreground">
                  ›
                </span>
              </button>
            ))}
          </div>
        ) : (
          <div className="text-muted-foreground rounded-xl border border-dashed px-5 py-10 text-center text-sm">
            {copy.noRecords}
          </div>
        )}

        {versions.page > 0 || versions.hasNext ? (
          <div className="flex items-center justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={versions.page === 0}
              onClick={versions.previous}
            >
              {copy.previous}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!versions.hasNext}
              onClick={versions.next}
            >
              {copy.next}
            </Button>
          </div>
        ) : null}
      </section>

      <Sheet
        open={selectedVersion !== null}
        onOpenChange={(open) => !open && detail.select(null)}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
          <SheetHeader>
            <SheetTitle>
              {copy.detailsTitle(selectedVersion ?? detail.data?.version ?? 0)}
            </SheetTitle>
            <SheetDescription>{copy.detailsDescription}</SheetDescription>
          </SheetHeader>

          <div className="space-y-6 px-4 pb-6">
            {detail.isLoading ? (
              <div className="space-y-3">
                <Skeleton className="h-28 w-full" />
                <Skeleton className="h-52 w-full" />
              </div>
            ) : detail.error ? (
              <ErrorState
                title={copy.detailFailed}
                retryLabel={copy.retry}
                onRetry={detail.retry}
              />
            ) : detail.data ? (
              <>
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <Badge variant="secondary">
                    {triggerLabel(detail.data.trigger)}
                  </Badge>
                  <Badge variant="outline">
                    {formatDate(detail.data.createdAt, locale)}
                  </Badge>
                  {detail.data.historyCount !== null ? (
                    <Badge variant="outline">
                      {copy.handled(detail.data.historyCount)}
                    </Badge>
                  ) : null}
                </div>

                <section aria-labelledby="memory-version-diff-title">
                  <h3
                    id="memory-version-diff-title"
                    className="mb-2 text-sm font-semibold"
                  >
                    {copy.diffTitle}
                  </h3>
                  {detail.data.unifiedDiff ? (
                    <pre className="bg-muted/60 max-h-80 overflow-auto rounded-lg border p-4 font-mono text-xs leading-5 whitespace-pre-wrap">
                      {detail.data.unifiedDiff}
                    </pre>
                  ) : (
                    <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
                      {copy.noDiff}
                    </p>
                  )}
                </section>

                <section aria-labelledby="memory-version-document-title">
                  <h3
                    id="memory-version-document-title"
                    className="mb-2 text-sm font-semibold"
                  >
                    {copy.documentTitle}
                  </h3>
                  <div className="bg-background max-h-[40vh] overflow-auto rounded-lg border p-4 text-sm leading-6 whitespace-pre-wrap">
                    {detail.data.content || "—"}
                  </div>
                </section>

                {actions.canRestore ? (
                  <Button
                    type="button"
                    variant="outline"
                    disabled={actions.restoringVersion !== null}
                    onClick={() => setRestoreConfirmation(detail.data!.version)}
                  >
                    {actions.restoringVersion === detail.data.version ? (
                      <Loader2Icon className="size-4 animate-spin" />
                    ) : (
                      <RotateCcwIcon className="size-4" />
                    )}
                    {actions.restoringVersion === detail.data.version
                      ? copy.restoring
                      : copy.restore}
                  </Button>
                ) : null}
              </>
            ) : null}
          </div>
        </SheetContent>
      </Sheet>

      <Dialog
        open={restoreConfirmation !== null}
        onOpenChange={(open) => !open && setRestoreConfirmation(null)}
      >
        <DialogContent closeLabel={copy.cancel}>
          <DialogHeader>
            <DialogTitle>
              {copy.restoreTitle(restoreConfirmation ?? 0)}
            </DialogTitle>
            <DialogDescription>{copy.restoreDescription}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setRestoreConfirmation(null)}
            >
              {copy.cancel}
            </Button>
            <Button
              type="button"
              disabled={actions.restoringVersion !== null}
              onClick={async () => {
                if (restoreConfirmation === null) return;
                try {
                  await actions.restore(restoreConfirmation);
                  setRestoreConfirmation(null);
                } catch {
                  // The owning page reports the safe API error and keeps the
                  // confirmation open so the user can retry or cancel.
                }
              }}
            >
              {actions.restoringVersion !== null ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                <RotateCcwIcon className="size-4" />
              )}
              {copy.confirmRestore}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
