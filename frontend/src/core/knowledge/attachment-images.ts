import { knowledgePreviewAttachmentsSchema } from "./types";

export type PreviewImageURLs = {
  urls: Map<string, string>;
  dispose(): void;
};

/**
 * Turns the already-bounded preview payload into browser-local URLs. The
 * existing strict DTO is deliberately re-applied at this resource boundary:
 * callers cannot bypass the per-image or aggregate byte budgets with a cast.
 */
export function createPreviewImageURLs(attachments: unknown): PreviewImageURLs {
  const parsed = knowledgePreviewAttachmentsSchema.parse(attachments);
  const urls = new Map<string, string>();
  let disposed = false;

  try {
    for (const item of parsed) {
      const binary = atob(item.data_base64);
      const bytes = Uint8Array.from(binary, (character) =>
        character.charCodeAt(0),
      );
      const previous = urls.get(item.ref);
      if (previous !== undefined) URL.revokeObjectURL(previous);
      urls.set(
        item.ref,
        URL.createObjectURL(
          new Blob([bytes.buffer], { type: item.media_type }),
        ),
      );
    }
  } catch (error) {
    for (const url of urls.values()) URL.revokeObjectURL(url);
    urls.clear();
    disposed = true;
    throw error;
  }

  return {
    urls,
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const url of urls.values()) URL.revokeObjectURL(url);
      urls.clear();
    },
  };
}

/** Exact UTF-8 digest used by the attachment endpoint's content fence. */
export async function knowledgeContentDigest(content: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(content),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
