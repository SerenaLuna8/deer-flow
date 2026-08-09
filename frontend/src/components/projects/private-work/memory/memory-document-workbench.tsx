"use client";

import {
  ArchiveIcon,
  BrainIcon,
  Code2Icon,
  EyeIcon,
  FileTextIcon,
  GitCompareArrowsIcon,
  HistoryIcon,
  Loader2Icon,
  RefreshCwIcon,
  RotateCcwIcon,
  SearchIcon,
  SparklesIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useI18n } from "@/core/i18n/hooks";
import type {
  MemoryDocument,
  MemoryEpisode,
  MemoryEpisodeTag,
  MemoryPendingEntry,
  MemoryVersionDetail,
  MemoryVersionSummary,
} from "@/core/private-work/memory";
import { SafeStreamdown } from "@/core/streamdown/components";
import { cn } from "@/lib/utils";

type MemoryDisplayMode = "preview" | "source";
export type MemoryWorkbenchTab = "current" | "archive";

type QueryState<T> = {
  data: T | undefined;
  error: Error | null;
  isLoading: boolean;
  retry: () => void;
};

export const MEMORY_EPISODE_TAGS: readonly MemoryEpisodeTag[] = [
  "permanent",
  "durable",
  "ephemeral",
  "correction",
];

export type MemoryDocumentWorkbenchProps = {
  document: QueryState<MemoryDocument>;
  versions: QueryState<readonly MemoryVersionSummary[]> & {
    latest: MemoryVersionSummary | null;
    page: number;
    hasNext: boolean;
    previous: () => void;
    next: () => void;
  };
  detail: QueryState<MemoryVersionDetail> & {
    selectedVersion: number | null;
    select: (version: number | null) => void;
  };
  episodes: {
    items: readonly MemoryEpisode[];
    error: Error | null;
    isLoading: boolean;
    retry: () => void;
    searchInput: string;
    setSearchInput: (value: string) => void;
    submitSearch: () => void;
    activeQuery: string | null;
    tags: readonly MemoryEpisodeTag[];
    toggleTag: (tag: MemoryEpisodeTag) => void;
    hasMore: boolean;
    loadMore: () => void;
    loadingMore: boolean;
  };
  pending: {
    items: readonly MemoryPendingEntry[];
    error: Error | null;
    isLoading: boolean;
    retry: () => void;
  };
  actions: {
    canDream: boolean;
    canRestore: boolean;
    dreaming: boolean;
    restoringVersion: number | null;
    dream: () => Promise<void>;
    restore: (version: number) => Promise<void>;
  };
  activeTab?: MemoryWorkbenchTab;
  onTabChange?: (tab: MemoryWorkbenchTab) => void;
};

function localizedCopy(locale: string) {
  if (locale === "zh-CN") {
    return {
      description: "一份由对话逐步整理而成、仅属于你的长期记忆文档。",
      currentTab: "当前记忆",
      archiveTab: "历史归档",
      documentFileName: "MEMORY.md",
      mediaType: "text/markdown",
      version: (value: number) => `版本 ${value}`,
      updated: "最近更新",
      neverUpdated: "尚未整理",
      viewMode: "文件显示方式",
      source: "源码",
      preview: "预览",
      viewChanges: "查看变化",
      versionsTitle: "版本历史",
      versionsDescription: "查看每次整理或恢复生成的真实文档版本。",
      versionsFailed: "无法加载版本历史",
      noVersions: "还没有历史版本。",
      previous: "上一页",
      next: "下一页",
      reviewTitle: "最新版本建议复核",
      reviewDescription:
        "这次整理删除了较多既有内容，请查看真实 diff，确认没有遗漏重要记忆。",
      reviewAction: "查看需复核版本",
      emptyTitle: "还没有长期记忆",
      emptyDescription:
        "继续对话后，ActWeave 会先记录待整理内容，再通过 Dream 汇入这份文档。",
      pending: "待整理",
      pendingUnit: (value: number) => `${value} 条`,
      dream: "立即整理",
      dreaming: "正在整理",
      dreamRunning: "已有整理任务运行中",
      dreamUnavailable: "你当前没有运行整理任务的权限。",
      autoDream: "自动整理",
      manualDream: "手动整理",
      restoreTrigger: "版本恢复",
      budgetRewrite: "预算压缩",
      handled: (value: number) => `处理 ${value} 条`,
      changed: "有内容变化",
      unchanged: "内容未变化",
      needsReview: "建议复核",
      overBudgetTitle: "记忆文档超出注入预算",
      overBudgetDescription:
        "在压缩进预算之前，新对话将暂时不注入这份记忆文档。",
      compressNow: "立即压缩文档",
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
      detailFailed: "无法加载版本详情",
      retry: "重试",
      archiveDescription:
        "已汇入文档的原始记忆条目会归档在这里，可以按内容和标签检索。",
      searchPlaceholder: "搜索归档记忆…",
      search: "搜索",
      archiveFailed: "无法加载历史归档",
      archiveEmpty: "还没有归档的记忆条目。",
      archiveNoMatch: "没有找到匹配的归档记忆。",
      loadMore: "加载更多",
      loadingMore: "正在加载",
      originSnip: "自动摘要",
      originRemember: "主动记忆",
      pendingTitle: "待整理内容",
      pendingDescription:
        "这些条目已被记录，将在下次整理（Dream）时汇入记忆文档。",
      pendingFailed: "无法加载待整理内容",
      tagLabel: (tag: string) =>
        tag === "permanent"
          ? "永久"
          : tag === "durable"
            ? "持久"
            : tag === "ephemeral"
              ? "短期"
              : "更正",
    };
  }
  return {
    description:
      "A private long-term document gradually organized from your conversations.",
    currentTab: "Current memory",
    archiveTab: "Archive",
    documentFileName: "MEMORY.md",
    mediaType: "text/markdown",
    version: (value: number) => `Version ${value}`,
    updated: "Last updated",
    neverUpdated: "Not organized yet",
    viewMode: "Document display mode",
    source: "Source",
    preview: "Preview",
    viewChanges: "View changes",
    versionsTitle: "Version history",
    versionsDescription:
      "Inspect the real document version created by every organization or restore.",
    versionsFailed: "Version history could not be loaded",
    noVersions: "No historical versions yet.",
    previous: "Previous",
    next: "Next",
    reviewTitle: "Latest version needs review",
    reviewDescription:
      "This organization removed a large share of existing content. Inspect the real diff to confirm that no important memory was lost.",
    reviewAction: "Review this version",
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
    autoDream: "Automatic Dream",
    manualDream: "Manual Dream",
    restoreTrigger: "Version restore",
    budgetRewrite: "Budget compression",
    handled: (value: number) =>
      `Processed ${value} ${value === 1 ? "item" : "items"}`,
    changed: "Document changed",
    unchanged: "No content change",
    needsReview: "Review suggested",
    overBudgetTitle: "Memory document exceeds the injection budget",
    overBudgetDescription:
      "New conversations temporarily run without this memory document until it is compressed into budget.",
    compressNow: "Compress document now",
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
    detailFailed: "Version details could not be loaded",
    retry: "Retry",
    archiveDescription:
      "Original memory items already organized into the document are archived here and searchable by text and tag.",
    searchPlaceholder: "Search archived memory…",
    search: "Search",
    archiveFailed: "Archive could not be loaded",
    archiveEmpty: "No archived memory items yet.",
    archiveNoMatch: "No archived memory matched this search.",
    loadMore: "Load more",
    loadingMore: "Loading",
    originSnip: "Auto summary",
    originRemember: "Remembered",
    pendingTitle: "Pending items",
    pendingDescription:
      "These items are recorded and will be organized into the memory document by the next Dream.",
    pendingFailed: "Pending items could not be loaded",
    tagLabel: (tag: string) =>
      tag === "permanent"
        ? "Permanent"
        : tag === "durable"
          ? "Durable"
          : tag === "ephemeral"
            ? "Ephemeral"
            : "Correction",
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
  episodes,
  pending,
  actions,
  activeTab = "current",
  onTabChange,
}: MemoryDocumentWorkbenchProps) {
  const { locale, t } = useI18n();
  const copy = useMemo(() => localizedCopy(locale), [locale]);
  const [restoreConfirmation, setRestoreConfirmation] = useState<number | null>(
    null,
  );
  const [displayMode, setDisplayMode] = useState<MemoryDisplayMode>("preview");
  const selectedVersion = detail.selectedVersion;
  const latestVersion = versions.latest;

  const triggerLabel = (trigger: MemoryVersionSummary["trigger"]) => {
    if (trigger === "auto_dream") return copy.autoDream;
    if (trigger === "manual_dream") return copy.manualDream;
    if (trigger === "budget_rewrite") return copy.budgetRewrite;
    return copy.restoreTrigger;
  };
  const overBudget = document.data?.injectionStatus === "skipped_over_budget";

  return (
    <div className="mx-auto flex h-[calc(100svh)] w-full max-w-5xl flex-col px-4 py-4 sm:px-6 lg:px-8 lg:py-6">
      <Tabs
        value={activeTab}
        onValueChange={(value) => {
          if (value === "current" || value === "archive") {
            onTabChange?.(value);
          }
        }}
        className="flex min-h-0 flex-1 flex-col gap-4"
      >
        <header className="flex shrink-0 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 flex-wrap items-center gap-3">
            <div className="flex items-center gap-2 text-sm font-medium text-violet-600 dark:text-violet-300">
              <BrainIcon className="size-4" />
              {t.settings.memory.title}
            </div>
            <TabsList aria-label={t.settings.memory.title}>
              <TabsTrigger value="current">
                <FileTextIcon className="size-4" />
                {copy.currentTab}
              </TabsTrigger>
              <TabsTrigger value="archive">
                <ArchiveIcon className="size-4" />
                {copy.archiveTab}
              </TabsTrigger>
            </TabsList>
          </div>
          {activeTab === "current" && document.data ? (
            <div className="flex shrink-0 items-center gap-2">
              <div className="border-border/70 bg-muted/30 rounded-xl border px-3 py-1.5 text-right">
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
                  (document.data.pendingCount === 0 && !overBudget)
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

        <TabsContent
          value="current"
          className="flex min-h-0 flex-1 flex-col gap-4 outline-none"
        >
          <p className="text-muted-foreground shrink-0 text-sm">
            {copy.description}
          </p>

          {overBudget && document.data ? (
            <Alert className="shrink-0">
              <AlertTitle>{copy.overBudgetTitle}</AlertTitle>
              <AlertDescription>
                <p>{copy.overBudgetDescription}</p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  disabled={
                    !actions.canDream ||
                    actions.dreaming ||
                    document.data.dreamRunning
                  }
                  onClick={() => void actions.dream().catch(() => undefined)}
                >
                  {actions.dreaming ? (
                    <Loader2Icon className="size-4 animate-spin" />
                  ) : (
                    <SparklesIcon className="size-4" />
                  )}
                  {actions.dreaming ? copy.dreaming : copy.compressNow}
                </Button>
              </AlertDescription>
            </Alert>
          ) : null}

          {latestVersion?.needsReview ? (
            <Alert className="shrink-0 border-amber-500/40 bg-amber-500/[0.06]">
              <AlertTitle>{copy.reviewTitle}</AlertTitle>
              <AlertDescription>
                <p>{copy.reviewDescription}</p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={() => detail.select(latestVersion.version)}
                >
                  <GitCompareArrowsIcon aria-hidden className="size-4" />
                  {copy.reviewAction}
                </Button>
              </AlertDescription>
            </Alert>
          ) : null}

          {document.isLoading ? (
            <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border">
              <div className="shrink-0 border-b px-4 py-3">
                <Skeleton className="h-4 w-28" />
                <Skeleton className="mt-2 h-3 w-56" />
              </div>
              <div className="min-h-0 flex-1 space-y-3 overflow-auto p-5">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-5/6" />
                <Skeleton className="h-4 w-2/3" />
              </div>
            </div>
          ) : document.error ? (
            <ErrorState
              title={copy.loadFailed}
              retryLabel={copy.retry}
              onRetry={document.retry}
            />
          ) : document.data ? (
            <section
              data-slot="memory-document-frame"
              className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border"
            >
              <div className="border-border/70 flex shrink-0 flex-col gap-3 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <p className="truncate font-mono text-sm font-semibold">
                    {copy.documentFileName}
                  </p>
                  <p className="text-muted-foreground mt-1 truncate text-[11px]">
                    {copy.version(document.data.version)}
                    {" · "}
                    {copy.mediaType}
                    {" · "}
                    {document.data.updatedAt
                      ? `${copy.updated} ${formatDate(document.data.updatedAt, locale)}`
                      : copy.neverUpdated}
                    {latestVersion ? (
                      <>
                        {" · "}
                        {triggerLabel(latestVersion.trigger)}
                        {" · "}
                        {latestVersion.changed ? copy.changed : copy.unchanged}
                        {latestVersion.needsReview ? (
                          <>
                            {" · "}
                            {copy.needsReview}
                          </>
                        ) : null}
                      </>
                    ) : null}
                  </p>
                  {document.data.dreamRunning ? (
                    <p className="text-muted-foreground mt-1.5 flex items-center gap-1.5 text-[11px]">
                      <Loader2Icon className="size-3 animate-spin" />
                      {copy.dreamRunning}
                    </p>
                  ) : !actions.canDream ? (
                    <p className="text-muted-foreground mt-1.5 text-[11px]">
                      {copy.dreamUnavailable}
                    </p>
                  ) : null}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {latestVersion ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      className="h-7 px-2"
                      onClick={() => detail.select(latestVersion.version)}
                    >
                      <GitCompareArrowsIcon aria-hidden className="size-3.5" />
                      {copy.viewChanges}
                    </Button>
                  ) : null}
                  {document.data.content ? (
                    <div
                      className="bg-muted flex rounded-lg p-1"
                      role="group"
                      aria-label={copy.viewMode}
                    >
                      <Button
                        type="button"
                        size="sm"
                        variant={
                          displayMode === "source" ? "secondary" : "ghost"
                        }
                        className="h-7 px-2"
                        aria-pressed={displayMode === "source"}
                        onClick={() => setDisplayMode("source")}
                      >
                        <Code2Icon aria-hidden className="size-3.5" />
                        {copy.source}
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant={
                          displayMode === "preview" ? "secondary" : "ghost"
                        }
                        className="h-7 px-2"
                        aria-pressed={displayMode === "preview"}
                        onClick={() => setDisplayMode("preview")}
                      >
                        <EyeIcon aria-hidden className="size-3.5" />
                        {copy.preview}
                      </Button>
                    </div>
                  ) : null}
                </div>
              </div>
              {document.data.content ? (
                displayMode === "preview" ? (
                  <div className="prose prose-sm prose-neutral dark:prose-invert min-h-0 max-w-none flex-1 overflow-auto p-5 [&_h1]:text-base [&_h1]:font-semibold [&_h2]:text-sm [&_h2]:font-semibold [&_h3]:text-sm">
                    <SafeStreamdown>{document.data.content}</SafeStreamdown>
                  </div>
                ) : (
                  <pre className="bg-muted/15 min-h-0 flex-1 overflow-auto p-5 font-mono text-sm leading-6 whitespace-pre-wrap">
                    <code>{document.data.content}</code>
                  </pre>
                )
              ) : (
                <div className="flex min-h-0 flex-1 flex-col items-center justify-center px-6 py-10 text-center">
                  <div className="mb-3 rounded-2xl bg-violet-500/10 p-3 text-violet-600 dark:text-violet-300">
                    <SparklesIcon className="size-6" />
                  </div>
                  <h2 className="font-medium">{copy.emptyTitle}</h2>
                  <p className="text-muted-foreground mt-2 max-w-md text-sm">
                    {copy.emptyDescription}
                  </p>
                </div>
              )}
            </section>
          ) : null}

          <details className="group shrink-0 overflow-hidden rounded-xl border">
            <summary className="hover:bg-muted/40 focus-visible:ring-ring flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 outline-none focus-visible:ring-2 focus-visible:ring-inset [&::-webkit-details-marker]:hidden">
              <span className="flex min-w-0 items-center gap-2">
                <HistoryIcon
                  aria-hidden
                  className="text-muted-foreground size-4"
                />
                <span className="text-sm font-semibold">
                  {copy.versionsTitle}
                </span>
                <span className="text-muted-foreground hidden truncate text-xs sm:inline">
                  {copy.versionsDescription}
                </span>
              </span>
              <span
                aria-hidden="true"
                className="text-muted-foreground transition-transform group-open:rotate-90"
              >
                ›
              </span>
            </summary>
            <div className="space-y-3 border-t p-3">
              {versions.isLoading ? (
                <div className="space-y-2">
                  <Skeleton className="h-14 w-full" />
                  <Skeleton className="h-14 w-full" />
                </div>
              ) : versions.error ? (
                <ErrorState
                  title={copy.versionsFailed}
                  retryLabel={copy.retry}
                  onRetry={versions.retry}
                />
              ) : versions.data?.length ? (
                <div className="max-h-52 overflow-y-auto rounded-lg border">
                  {versions.data.map((version) => (
                    <button
                      key={version.version}
                      type="button"
                      className="hover:bg-muted/50 focus-visible:ring-ring flex w-full items-center justify-between gap-4 border-b px-3 py-2.5 text-left outline-none last:border-b-0 focus-visible:ring-2 focus-visible:ring-inset"
                      onClick={() => detail.select(version.version)}
                    >
                      <span className="min-w-0">
                        <span className="flex flex-wrap items-center gap-2">
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
                          {version.needsReview ? (
                            <Badge
                              variant="outline"
                              className="border-amber-500/40 text-amber-700 dark:text-amber-300"
                            >
                              {copy.needsReview}
                            </Badge>
                          ) : null}
                        </span>
                        <span className="text-muted-foreground mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs">
                          <span>{formatDate(version.createdAt, locale)}</span>
                          {version.historyCount !== null ? (
                            <span>{copy.handled(version.historyCount)}</span>
                          ) : null}
                        </span>
                      </span>
                      <span aria-hidden className="text-muted-foreground">
                        ›
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-muted-foreground rounded-lg border border-dashed px-4 py-6 text-center text-sm">
                  {copy.noVersions}
                </p>
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
            </div>
          </details>

          {pending.error ? (
            <div className="shrink-0">
              <ErrorState
                title={copy.pendingFailed}
                retryLabel={copy.retry}
                onRetry={pending.retry}
              />
            </div>
          ) : pending.items.length ? (
            <section
              id="memory-pending"
              tabIndex={-1}
              aria-labelledby="memory-pending-title"
              className="max-h-48 shrink-0 space-y-3 overflow-y-auto"
            >
              <div>
                <h2 id="memory-pending-title" className="text-sm font-semibold">
                  {copy.pendingTitle}
                </h2>
                <p className="text-muted-foreground mt-1 text-sm">
                  {copy.pendingDescription}
                </p>
              </div>
              <ul className="overflow-hidden rounded-xl border border-dashed">
                {pending.items.map((entry) => (
                  <li
                    key={entry.sequence}
                    className="border-b border-dashed px-4 py-3 last:border-b-0"
                  >
                    <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                      <Badge variant="secondary">
                        {entry.origin === "snip"
                          ? copy.originSnip
                          : copy.originRemember}
                      </Badge>
                      <span>{formatDate(entry.createdAt, locale)}</span>
                    </div>
                    <p className="mt-1.5 text-sm leading-6 whitespace-pre-wrap">
                      {entry.taggedText}
                    </p>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </TabsContent>

        <TabsContent
          value="archive"
          className="flex min-h-0 flex-1 flex-col gap-3 outline-none"
        >
          <section
            aria-labelledby="memory-archive-title"
            className="flex min-h-0 flex-1 flex-col gap-3"
          >
            <div className="shrink-0">
              <h2 id="memory-archive-title" className="sr-only">
                {copy.archiveTab}
              </h2>
              <p className="text-muted-foreground text-sm">
                {copy.archiveDescription}
              </p>
            </div>

            <form
              className="flex shrink-0 items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                episodes.submitSearch();
              }}
            >
              <div className="relative flex-1">
                <SearchIcon className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
                <Input
                  type="search"
                  value={episodes.searchInput}
                  maxLength={200}
                  placeholder={copy.searchPlaceholder}
                  className="pl-9"
                  onChange={(event) =>
                    episodes.setSearchInput(event.target.value)
                  }
                />
              </div>
              <Button type="submit" variant="secondary">
                {copy.search}
              </Button>
            </form>

            <div className="flex shrink-0 flex-wrap items-center gap-2">
              {MEMORY_EPISODE_TAGS.map((tag) => {
                const active = episodes.tags.includes(tag);
                return (
                  <button
                    key={tag}
                    type="button"
                    aria-pressed={active}
                    className={cn(
                      "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                      active
                        ? "border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300"
                        : "text-muted-foreground hover:bg-muted/60",
                    )}
                    onClick={() => episodes.toggleTag(tag)}
                  >
                    {copy.tagLabel(tag)}
                  </button>
                );
              })}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto">
              {episodes.isLoading ? (
                <div className="space-y-2">
                  <Skeleton className="h-14 w-full" />
                  <Skeleton className="h-14 w-full" />
                </div>
              ) : episodes.error ? (
                <ErrorState
                  title={copy.archiveFailed}
                  retryLabel={copy.retry}
                  onRetry={episodes.retry}
                />
              ) : episodes.items.length ? (
                <>
                  <ul className="overflow-hidden rounded-xl border">
                    {episodes.items.map((episode) => (
                      <li
                        key={episode.id}
                        className="border-b px-4 py-3 last:border-b-0"
                      >
                        <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                          <Badge variant="secondary">
                            {episode.origin === "snip"
                              ? copy.originSnip
                              : copy.originRemember}
                          </Badge>
                          <span>{formatDate(episode.occurredAt, locale)}</span>
                        </div>
                        <p className="mt-1.5 text-sm leading-6 whitespace-pre-wrap">
                          {episode.taggedText}
                        </p>
                      </li>
                    ))}
                  </ul>
                  {episodes.hasMore ? (
                    <div className="flex justify-center py-3">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={episodes.loadingMore}
                        onClick={episodes.loadMore}
                      >
                        {episodes.loadingMore ? (
                          <Loader2Icon className="size-4 animate-spin" />
                        ) : null}
                        {episodes.loadingMore
                          ? copy.loadingMore
                          : copy.loadMore}
                      </Button>
                    </div>
                  ) : null}
                </>
              ) : (
                <div className="text-muted-foreground rounded-xl border border-dashed px-5 py-8 text-center text-sm">
                  {episodes.activeQuery || episodes.tags.length
                    ? copy.archiveNoMatch
                    : copy.archiveEmpty}
                </div>
              )}
            </div>
          </section>
        </TabsContent>
      </Tabs>

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
                  <div className="bg-background prose prose-sm prose-neutral dark:prose-invert max-h-[40vh] max-w-none overflow-auto rounded-lg border p-4 [&_h1]:text-base [&_h1]:font-semibold [&_h2]:text-sm [&_h2]:font-semibold [&_h3]:text-sm">
                    {detail.data.content ? (
                      <SafeStreamdown>{detail.data.content}</SafeStreamdown>
                    ) : (
                      "—"
                    )}
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
