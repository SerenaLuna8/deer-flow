import { visit } from "unist-util-visit";

const LOGICAL_IMAGE_RE = /^knowledge-attachment:([0-9a-f]{64})$/u;
const INTERNAL_IMAGE_RE = /^\/__knowledge-image\/([0-9a-f]{64})$/u;

type MarkdownImageNode = {
  type: "image";
  url: string;
  data?: {
    hProperties?: Record<string, unknown>;
    [key: string]: unknown;
  };
};

/** Maps one server-authored logical image identity to a renderer-only path. */
export function knowledgeImagePlaceholderURL(url: string): string | null {
  const match = LOGICAL_IMAGE_RE.exec(url);
  return match ? `/__knowledge-image/${match[1]}` : null;
}

/** Accepts only paths emitted by knowledgeImagePlaceholderURL. */
export function knowledgeImageRefFromPlaceholder(
  url: string | undefined,
  marker: unknown,
): string | null {
  if (url === undefined || typeof marker !== "string") return null;
  const ref = INTERNAL_IMAGE_RE.exec(url)?.[1] ?? null;
  return ref !== null && marker === ref ? ref : null;
}

/**
 * Remark visits parsed image nodes, so lookalike text in code blocks or inline
 * code is never rewritten. All unrecognized image URLs remain untrusted and
 * are handled as blocked placeholders by the custom image renderer.
 */
export function remarkKnowledgeImages() {
  return (tree: unknown) => {
    visit(tree as Parameters<typeof visit>[0], "image", (node) => {
      const image = node as MarkdownImageNode;
      const placeholder = knowledgeImagePlaceholderURL(image.url);
      if (placeholder === null) return;
      const ref = LOGICAL_IMAGE_RE.exec(image.url)?.[1];
      if (ref === undefined) return;
      image.url = placeholder;
      image.data = {
        ...image.data,
        hProperties: {
          ...image.data?.hProperties,
          dataKnowledgeImageRef: ref,
        },
      };
    });
  };
}
