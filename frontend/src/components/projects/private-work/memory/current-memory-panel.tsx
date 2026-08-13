"use client";

import {
  Code2Icon,
  EyeIcon,
  GitCompareArrowsIcon,
  Loader2Icon,
  SparklesIcon,
} from "lucide-react";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { SafeStreamdown } from "@/core/streamdown/components";

import {
  formatMemoryDate,
  MemoryErrorState,
  memoryTriggerLabel,
} from "./memory-workbench-shared";
import type {
  MemoryActionsState,
  MemoryDetailState,
  MemoryDocumentState,
  MemoryVersionsState,
} from "./memory-workbench-types";

type MemoryDisplayMode = "preview" | "source";

export function CurrentMemoryPanel({
  document,
  versions,
  detail,
  actions,
}: {
  document: MemoryDocumentState;
  versions: MemoryVersionsState;
  detail: MemoryDetailState;
  actions: MemoryActionsState;
}) {
  const { locale, t } = useI18n();
  const copy = t.projectMemory;
  const [displayMode, setDisplayMode] = useState<MemoryDisplayMode>("preview");
  const latestVersion = versions.latest;
  const overBudget = document.data?.injectionStatus === "skipped_over_budget";
  const inactiveReason =
    document.data?.injectionAdvisory?.status === "inactive"
      ? document.data.injectionAdvisory.reason
      : null;

  return (
    <>
      <p className="text-muted-foreground shrink-0 text-sm">
        {copy.description}
      </p>

      {inactiveReason === "platform_disabled" ||
      inactiveReason === "account_disabled" ? (
        <Alert className="shrink-0">
          <AlertTitle>{copy.injectionInactiveTitle}</AlertTitle>
          <AlertDescription>
            {inactiveReason === "platform_disabled"
              ? copy.injectionPlatformDisabledDescription
              : copy.injectionAccountDisabledDescription}
          </AlertDescription>
        </Alert>
      ) : null}

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
        <MemoryErrorState
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
                  ? `${copy.updated} ${formatMemoryDate(document.data.updatedAt, locale)}`
                  : copy.neverUpdated}
                {latestVersion ? (
                  <>
                    {" · "}
                    {memoryTriggerLabel(copy, latestVersion.trigger)}
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
                    variant={displayMode === "source" ? "secondary" : "ghost"}
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
                    variant={displayMode === "preview" ? "secondary" : "ghost"}
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
    </>
  );
}
