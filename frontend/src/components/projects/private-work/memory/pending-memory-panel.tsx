"use client";

import { Badge } from "@/components/ui/badge";
import { useI18n } from "@/core/i18n/hooks";

import { formatMemoryDate, MemoryErrorState } from "./memory-workbench-shared";
import type { MemoryPendingState } from "./memory-workbench-types";

export function PendingMemoryPanel({
  pending,
}: {
  pending: MemoryPendingState;
}) {
  const { locale, t } = useI18n();
  const copy = t.projectMemory;

  if (pending.error) {
    return (
      <div className="shrink-0">
        <MemoryErrorState
          title={copy.pendingFailed}
          retryLabel={copy.retry}
          onRetry={pending.retry}
        />
      </div>
    );
  }
  if (!pending.items.length) return null;

  return (
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
              <span>{formatMemoryDate(entry.createdAt, locale)}</span>
            </div>
            <p className="mt-1.5 text-sm leading-6 whitespace-pre-wrap">
              {entry.taggedText}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}
