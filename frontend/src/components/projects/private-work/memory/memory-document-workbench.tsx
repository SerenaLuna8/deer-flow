"use client";

import {
  ArchiveIcon,
  BrainIcon,
  FileTextIcon,
  Loader2Icon,
  SparklesIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useI18n } from "@/core/i18n/hooks";

import { CurrentMemoryPanel } from "./current-memory-panel";
import { MemoryArchivePanel } from "./memory-archive-panel";
import { MemoryVersionDetail } from "./memory-version-detail";
import { MemoryVersionList } from "./memory-version-list";
import type {
  MemoryDocumentWorkbenchProps,
  MemoryWorkbenchTab,
} from "./memory-workbench-types";
import { PendingMemoryPanel } from "./pending-memory-panel";

export { MEMORY_EPISODE_TAGS } from "./memory-archive-panel";
export type {
  MemoryDocumentWorkbenchProps,
  MemoryWorkbenchTab,
} from "./memory-workbench-types";
export { MemoryVersionDiff } from "./memory-version-detail";

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
  const { t } = useI18n();
  const copy = t.projectMemory;
  const overBudget = document.data?.injectionStatus === "skipped_over_budget";
  const injectionInactive =
    document.data?.injectionAdvisory?.status === "inactive" &&
    document.data.injectionAdvisory.reason !== "no_document";

  return (
    <main className="mx-auto flex h-[calc(100svh-3.5rem)] w-full max-w-5xl flex-col px-4 py-4 sm:px-6 md:h-screen lg:px-8 lg:py-6">
      <Tabs
        value={activeTab}
        onValueChange={(value) => {
          if (value === "current" || value === "archive") {
            onTabChange?.(value as MemoryWorkbenchTab);
          }
        }}
        className="flex min-h-0 flex-1 flex-col gap-4"
      >
        <header className="flex shrink-0 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 flex-wrap items-center gap-3">
            <div className="flex items-center gap-2 text-sm font-medium text-violet-600 dark:text-violet-300">
              <BrainIcon className="size-4" />
              {copy.title}
            </div>
            <TabsList aria-label={copy.title}>
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
                  injectionInactive ||
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
          <CurrentMemoryPanel
            document={document}
            versions={versions}
            detail={detail}
            actions={actions}
          />
          <MemoryVersionList versions={versions} detail={detail} />
          <PendingMemoryPanel pending={pending} />
        </TabsContent>

        <TabsContent
          value="archive"
          className="flex min-h-0 flex-1 flex-col gap-3 outline-none"
        >
          <MemoryArchivePanel episodes={episodes} />
        </TabsContent>
      </Tabs>

      <MemoryVersionDetail detail={detail} actions={actions} />
    </main>
  );
}
