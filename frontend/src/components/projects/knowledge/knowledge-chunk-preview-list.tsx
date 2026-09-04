"use client";

import { useEffect, useState } from "react";

import { useI18n } from "@/core/i18n/hooks";
import { createPreviewImageURLs } from "@/core/knowledge/attachment-images";
import type { KnowledgeChunkPreviewResponse } from "@/core/knowledge/types";
import { cn } from "@/lib/utils";

import type { KnowledgeImageSource } from "./knowledge-image";
import { KnowledgeMarkdown } from "./knowledge-markdown";

const EMPTY_KNOWLEDGE_IMAGE_SOURCES: ReadonlyMap<string, KnowledgeImageSource> =
  new Map();

/**
 * Renders one stateless chunk preview (wizard upload or document reparse).
 * Preview thumbnails become object URLs scoped to the exact response
 * identity and are disposed when the response is replaced or goes stale.
 */
export function KnowledgeChunkPreviewList({
  data,
  scopeKey,
  stale,
}: {
  data: Pick<
    KnowledgeChunkPreviewResponse,
    "items" | "preview_fingerprint" | "preview_attachments"
  >;
  scopeKey: string;
  stale: boolean;
}) {
  const { t } = useI18n();
  const labels = t.knowledge.wizard;
  const resourceIdentity = `${scopeKey}:${data.preview_fingerprint}:${stale ? "stale" : "current"}`;
  const [imageState, setImageState] = useState<{
    identity: string;
    sources: ReadonlyMap<string, KnowledgeImageSource>;
  } | null>(null);

  useEffect(() => {
    if (stale) return;
    let resources: ReturnType<typeof createPreviewImageURLs> | null = null;
    try {
      resources = createPreviewImageURLs(data.preview_attachments);
      setImageState({
        identity: resourceIdentity,
        sources: new Map(
          [...resources.urls].map(([ref, url]) => [
            ref,
            { kind: "preview", url } as const,
          ]),
        ),
      });
    } catch {
      setImageState({
        identity: resourceIdentity,
        sources: EMPTY_KNOWLEDGE_IMAGE_SOURCES,
      });
    }
    return () => resources?.dispose();
  }, [data, resourceIdentity, stale]);

  const imageSources =
    imageState?.identity === resourceIdentity
      ? imageState.sources
      : EMPTY_KNOWLEDGE_IMAGE_SOURCES;

  return (
    <ul className={cn("grid gap-6 transition-opacity", stale && "opacity-60")}>
      {data.items.map((chunk) => (
        <li key={chunk.position} className="space-y-2">
          <p className="text-muted-foreground flex items-center justify-between gap-2 text-xs tabular-nums">
            <span className="font-medium">
              {labels.previewChunkLabel(chunk.position)}
            </span>
            <span>
              {chunk.child_contents.length > 0
                ? `${labels.previewChildCount(chunk.child_contents.length)} · `
                : null}
              {labels.previewCharacters(chunk.word_count)}
            </span>
          </p>
          {chunk.child_contents.length === 0 ? (
            <KnowledgeMarkdown
              content={chunk.content}
              imageSources={imageSources}
              scopeKey={scopeKey}
              className="text-[13px] leading-6 break-words"
            />
          ) : null}
          {chunk.child_contents.length > 0 ? (
            <ol className="flex flex-wrap items-start gap-x-1 gap-y-1.5">
              {chunk.child_contents.map((childContent, index) => (
                <li
                  key={index}
                  className="bg-muted/60 max-w-full rounded-sm px-1.5 py-0.5 text-[13px] leading-6 break-words"
                >
                  <span className="text-muted-foreground mr-1.5 inline-block text-[10px] font-medium tabular-nums">
                    {labels.previewChildLabel(index + 1)}
                  </span>
                  <KnowledgeMarkdown
                    content={childContent}
                    imageSources={imageSources}
                    scopeKey={scopeKey}
                    className="inline whitespace-normal"
                  />
                </li>
              ))}
            </ol>
          ) : null}
          {chunk.child_contents.length > 0 ? (
            <details className="text-muted-foreground text-xs">
              <summary className="hover:text-foreground cursor-pointer py-1">
                {labels.previewParentText}
              </summary>
              <KnowledgeMarkdown
                content={chunk.content}
                imageSources={imageSources}
                scopeKey={scopeKey}
                className="pt-1 text-[13px] leading-6 break-words"
              />
            </details>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
