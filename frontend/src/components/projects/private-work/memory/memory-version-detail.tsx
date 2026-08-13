"use client";

import { Loader2Icon, RotateCcwIcon } from "lucide-react";
import { useState } from "react";

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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import type { MemoryVersionDetail as MemoryVersionDetailData } from "@/core/private-work/memory/types";
import { SafeStreamdown } from "@/core/streamdown/components";

import {
  formatMemoryDate,
  MemoryErrorState,
  memoryTriggerLabel,
} from "./memory-workbench-shared";
import type {
  MemoryActionsState,
  MemoryDetailState,
} from "./memory-workbench-types";

export function MemoryVersionDiff({
  unifiedDiff,
  diffTruncated,
}: Pick<MemoryVersionDetailData, "unifiedDiff" | "diffTruncated">) {
  const { t } = useI18n();
  const copy = t.projectMemory;
  return (
    <section aria-labelledby="memory-version-diff-title">
      <h3 id="memory-version-diff-title" className="mb-2 text-sm font-semibold">
        {copy.diffTitle}
      </h3>
      {diffTruncated ? (
        <Alert className="mb-3 border-amber-500/40 bg-amber-500/[0.06]">
          <AlertTitle>{copy.diffTruncatedTitle}</AlertTitle>
          <AlertDescription>{copy.diffTruncatedDescription}</AlertDescription>
        </Alert>
      ) : null}
      {unifiedDiff ? (
        <pre className="bg-muted/60 max-h-80 overflow-auto rounded-lg border p-4 font-mono text-xs leading-5 whitespace-pre-wrap">
          {unifiedDiff}
        </pre>
      ) : (
        <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
          {copy.noDiff}
        </p>
      )}
    </section>
  );
}

export function MemoryVersionDetail({
  detail,
  actions,
}: {
  detail: MemoryDetailState;
  actions: MemoryActionsState;
}) {
  const { locale, t } = useI18n();
  const copy = t.projectMemory;
  const [restoreConfirmation, setRestoreConfirmation] = useState<number | null>(
    null,
  );
  const selectedVersion = detail.selectedVersion;

  return (
    <>
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
              <MemoryErrorState
                title={copy.detailFailed}
                retryLabel={copy.retry}
                onRetry={detail.retry}
              />
            ) : detail.data ? (
              <>
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <Badge variant="secondary">
                    {memoryTriggerLabel(copy, detail.data.trigger)}
                  </Badge>
                  <Badge variant="outline">
                    {formatMemoryDate(detail.data.createdAt, locale)}
                  </Badge>
                  {detail.data.historyCount !== null ? (
                    <Badge variant="outline">
                      {copy.handled(detail.data.historyCount)}
                    </Badge>
                  ) : null}
                </div>

                <MemoryVersionDiff
                  unifiedDiff={detail.data.unifiedDiff}
                  diffTruncated={detail.data.diffTruncated}
                />

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
                  // The query model reports the safe API error and keeps this
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
    </>
  );
}
