"use client";

import { ImageOffIcon } from "lucide-react";
import {
  useEffect,
  useState,
  type ImgHTMLAttributes,
  type ReactNode,
} from "react";

import { Button } from "@/components/ui/button";
import { fetchKnowledgeAttachment } from "@/core/knowledge/api";
import type { KnowledgeAttachmentRead } from "@/core/knowledge/types";
import { cn } from "@/lib/utils";

export type KnowledgeImageSource =
  | { kind: "preview"; url: string }
  | { kind: "protected"; request: KnowledgeAttachmentRead };

export type KnowledgeImageState = {
  url: string | null;
  status: "idle" | "loading" | "ready" | "error";
};

type OwnedKnowledgeImageState = KnowledgeImageState & { identity: string };

function attachmentIdentity(input: KnowledgeAttachmentRead): string {
  return JSON.stringify(input);
}

/**
 * Imperatively reads one authorized published image and owns its Object URL.
 * The returned URL is hidden immediately when request or scope identity
 * changes, before the prior effect cleanup runs.
 */
export function useKnowledgeImage(
  input: KnowledgeAttachmentRead | null,
  scopeKey: string,
): KnowledgeImageState {
  const requestIdentity = input === null ? null : attachmentIdentity(input);
  const desiredIdentity = `${scopeKey}:${requestIdentity ?? "idle"}`;
  const [state, setState] = useState<OwnedKnowledgeImageState>({
    identity: desiredIdentity,
    url: null,
    status: input === null ? "idle" : "loading",
  });

  useEffect(() => {
    if (requestIdentity === null) {
      setState({ identity: desiredIdentity, url: null, status: "idle" });
      return;
    }

    const request = JSON.parse(requestIdentity) as KnowledgeAttachmentRead;
    let active = true;
    let ownedURL: string | null = null;
    const controller = new AbortController();
    setState({ identity: desiredIdentity, url: null, status: "loading" });

    void (async () => {
      try {
        const blob = await fetchKnowledgeAttachment(request, controller.signal);
        if (!active) return;
        const url = URL.createObjectURL(blob);
        if (!active) {
          URL.revokeObjectURL(url);
          return;
        }
        ownedURL = url;
        setState({ identity: desiredIdentity, url, status: "ready" });
      } catch {
        if (!active || controller.signal.aborted) return;
        setState({ identity: desiredIdentity, url: null, status: "error" });
      }
    })();

    return () => {
      active = false;
      controller.abort();
      if (ownedURL !== null) URL.revokeObjectURL(ownedURL);
    };
  }, [desiredIdentity, requestIdentity]);

  if (state.identity !== desiredIdentity) {
    return { url: null, status: input === null ? "idle" : "loading" };
  }
  return { url: state.url, status: state.status };
}

function ImagePlaceholder({
  alt,
  children,
  onRetry,
}: {
  alt: string;
  children: ReactNode;
  onRetry?: () => void;
}) {
  return (
    <span
      data-testid="knowledge-image-placeholder"
      className="border-border/70 bg-muted/30 text-muted-foreground my-2 inline-flex min-h-16 w-full items-center justify-center gap-2 rounded-md border border-dashed px-3 py-4 text-xs"
    >
      <span className="sr-only">{alt}</span>
      <ImageOffIcon aria-hidden className="size-4 shrink-0" />
      <span>{children}</span>
      {onRetry ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-7 rounded-md text-xs"
          onClick={onRetry}
        >
          Refresh image
        </Button>
      ) : null}
    </span>
  );
}

export function KnowledgeImage({
  source,
  scopeKey,
  blockedReason = "external",
  alt = "",
  className,
  ...props
}: Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> & {
  source: KnowledgeImageSource | null;
  scopeKey: string;
  blockedReason?: "external" | "unavailable";
}) {
  const [retryGeneration, setRetryGeneration] = useState(0);
  const protectedImage = useKnowledgeImage(
    source?.kind === "protected" ? source.request : null,
    `${scopeKey}:image-retry-${retryGeneration}`,
  );
  const url = source?.kind === "preview" ? source.url : protectedImage.url;
  const status =
    source?.kind === "preview"
      ? "ready"
      : source === null
        ? "error"
        : protectedImage.status;
  const [failedURL, setFailedURL] = useState<string | null>(null);

  if (status !== "ready" || url === null || failedURL === url) {
    return (
      <ImagePlaceholder
        alt={alt || "Image unavailable"}
        onRetry={
          source?.kind === "protected" && status !== "loading"
            ? () => setRetryGeneration((generation) => generation + 1)
            : undefined
        }
      >
        {source === null
          ? blockedReason === "external"
            ? "External image not loaded"
            : "Image unavailable"
          : status === "loading"
            ? "Loading image"
            : "Image unavailable"}
      </ImagePlaceholder>
    );
  }

  return (
    // Blob URLs are generated from strict safe-raster DTOs and cannot be
    // served by next/image's optimizer without turning them back into URLs.
    <img
      {...props}
      src={url}
      alt={alt}
      className={cn("my-2 h-auto max-w-full rounded-md", className)}
      data-testid="knowledge-image"
      onError={() => setFailedURL(url)}
    />
  );
}
