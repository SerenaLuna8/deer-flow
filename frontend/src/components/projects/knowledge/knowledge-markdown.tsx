"use client";

import { useMemo, type ImgHTMLAttributes } from "react";

import type { ClipboardSafeStreamdownProps } from "@/components/ai-elements/streamdown";
import { MarkdownContent } from "@/components/workspace/messages/markdown-content";
import {
  knowledgeImageRefFromPlaceholder,
  remarkKnowledgeImages,
} from "@/core/knowledge/markdown-images";
import { streamdownPluginsWithoutRawHtml } from "@/core/streamdown";

import { KnowledgeImage, type KnowledgeImageSource } from "./knowledge-image";

type KnowledgeMarkdownImageProps = ImgHTMLAttributes<HTMLImageElement> & {
  "data-knowledge-image-ref"?: unknown;
};

export function KnowledgeMarkdown({
  content,
  imageSources,
  scopeKey,
  className,
}: {
  content: string;
  imageSources: ReadonlyMap<string, KnowledgeImageSource>;
  scopeKey: string;
  className?: string;
}) {
  const rendererIdentity = useMemo(
    () =>
      JSON.stringify([
        scopeKey,
        [...imageSources]
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([ref, source]) => [
            ref,
            source.kind === "preview"
              ? [source.kind, source.url]
              : [source.kind, source.request],
          ]),
      ]),
    [imageSources, scopeKey],
  );
  const remarkPlugins = useMemo(
    () =>
      [
        ...(streamdownPluginsWithoutRawHtml.remarkPlugins ?? []),
        remarkKnowledgeImages,
      ] as ClipboardSafeStreamdownProps["remarkPlugins"],
    [],
  );
  const components = useMemo(
    () => ({
      img({
        src,
        alt,
        "data-knowledge-image-ref": marker,
        ...props
      }: KnowledgeMarkdownImageProps) {
        const ref = knowledgeImageRefFromPlaceholder(
          typeof src === "string" ? src : undefined,
          marker,
        );
        return (
          <KnowledgeImage
            {...props}
            alt={alt ?? ""}
            source={ref === null ? null : (imageSources.get(ref) ?? null)}
            scopeKey={scopeKey}
            blockedReason={ref === null ? "external" : "unavailable"}
          />
        );
      },
    }),
    [imageSources, scopeKey],
  );

  return (
    <MarkdownContent
      key={rendererIdentity}
      content={content}
      isLoading={false}
      className={className}
      remarkPlugins={remarkPlugins}
      components={components}
    />
  );
}
