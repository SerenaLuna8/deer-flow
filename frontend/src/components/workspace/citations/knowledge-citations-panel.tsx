"use client";

import { LibraryBigIcon } from "lucide-react";

import { useI18n } from "@/core/i18n/hooks";
import { formatKnowledgeSourcePosition } from "@/core/knowledge/source-position";
import type { KnowledgeCitation } from "@/core/threads/message-projection";
import { cn } from "@/lib/utils";

/**
 * Collapsible list of Knowledge Base citations under an assistant answer.
 * Mirrors the web-source CitationSourcesPanel so both citation kinds read as
 * one visual family; knowledge segments have no external URL, so each entry
 * shows its provenance (base, document, segment) and the retrieved snippet.
 */
export function KnowledgeCitationsPanel({
  className,
  citations,
}: {
  className?: string;
  citations: KnowledgeCitation[];
}) {
  const { t } = useI18n();

  if (citations.length === 0) {
    return null;
  }

  return (
    <details
      className={cn(
        "not-prose border-border/60 bg-muted/20 mt-2 rounded-md border text-xs",
        className,
      )}
      data-testid="knowledge-citations-panel"
    >
      <summary className="text-muted-foreground hover:text-foreground flex cursor-pointer list-none items-center gap-2 px-3 py-2 transition-colors [&::-webkit-details-marker]:hidden">
        <LibraryBigIcon className="size-3.5 shrink-0" />
        <span className="font-medium">
          {t.knowledge.citations.summary(citations.length)}
        </span>
      </summary>
      <ol className="border-border/60 divide-border/60 max-h-80 divide-y overflow-y-auto overscroll-contain border-t">
        {citations.map((citation, index) => {
          const position = formatKnowledgeSourcePosition(
            citation.source_position,
            t.knowledge.sourcePosition,
          );
          return (
            <li
              key={citation.segment_id}
              className="flex min-w-0 items-start gap-2 p-2"
            >
              <span className="text-muted-foreground w-5 shrink-0 pt-1 text-right tabular-nums">
                {index + 1}
              </span>
              <div className="min-w-0 flex-1 rounded px-2 py-1">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="text-foreground min-w-0 flex-1 truncate font-medium">
                    {citation.document_name}
                  </span>
                  <span className="text-muted-foreground shrink-0">
                    {t.knowledge.citations.score(citation.score)}
                  </span>
                </div>
                <div className="text-muted-foreground truncate">
                  {citation.knowledge_base_name}
                  {" · "}
                  {t.knowledge.citations.segmentPosition(
                    citation.segment_position,
                  )}
                  {position ? ` · ${position}` : null}
                </div>
                {citation.snippet && (
                  <p className="text-muted-foreground mt-1 line-clamp-3 break-words whitespace-pre-wrap">
                    {citation.snippet}
                  </p>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </details>
  );
}
