"use client";

import { HistoryIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

import {
  formatMemoryDate,
  MemoryErrorState,
  memoryTriggerLabel,
} from "./memory-workbench-shared";
import type {
  MemoryDetailState,
  MemoryVersionsState,
} from "./memory-workbench-types";

export function MemoryVersionList({
  versions,
  detail,
}: {
  versions: MemoryVersionsState;
  detail: MemoryDetailState;
}) {
  const { locale, t } = useI18n();
  const copy = t.projectMemory;

  return (
    <details className="group shrink-0 overflow-hidden rounded-xl border">
      <summary className="hover:bg-muted/40 focus-visible:ring-ring flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 outline-none focus-visible:ring-2 focus-visible:ring-inset [&::-webkit-details-marker]:hidden">
        <span className="flex min-w-0 items-center gap-2">
          <HistoryIcon aria-hidden className="text-muted-foreground size-4" />
          <span className="text-sm font-semibold">{copy.versionsTitle}</span>
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
          <MemoryErrorState
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
                      {memoryTriggerLabel(copy, version.trigger)}
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
                    <span>{formatMemoryDate(version.createdAt, locale)}</span>
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
  );
}
