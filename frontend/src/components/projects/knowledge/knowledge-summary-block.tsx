"use client";

import { useI18n } from "@/core/i18n/hooks";
import type { KnowledgeSegmentSummary } from "@/core/knowledge/types";

export function KnowledgeSummaryBlock({
  summary,
}: {
  summary: KnowledgeSegmentSummary | null;
}) {
  const { t, locale } = useI18n();
  if (summary === null) return null;

  return (
    <section
      aria-label={t.knowledge.summary.generatedTitle}
      data-testid="knowledge-segment-summary"
      className="border-border bg-muted/40 space-y-2 rounded-lg border p-3"
    >
      <div className="text-muted-foreground flex flex-wrap items-center justify-between gap-2 text-xs">
        <h4 className="font-medium">{t.knowledge.summary.generatedTitle}</h4>
        <time dateTime={summary.created_at}>
          {new Date(summary.created_at).toLocaleString(locale)}
        </time>
      </div>
      <p className="text-[13px] leading-6 [overflow-wrap:anywhere] whitespace-pre-wrap">
        {summary.content}
      </p>
    </section>
  );
}
