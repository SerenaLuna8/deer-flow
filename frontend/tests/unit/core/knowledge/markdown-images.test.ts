import { describe, expect, it } from "@rstest/core";

import {
  knowledgeImageRefFromPlaceholder,
  remarkKnowledgeImages,
} from "@/core/knowledge/markdown-images";

const REF = "a".repeat(64);

describe("Knowledge Markdown image authorization", () => {
  it("rewrites only a canonical logical attachment image node", () => {
    const tree = {
      type: "root",
      children: [
        {
          type: "paragraph",
          children: [
            {
              type: "image",
              url: `knowledge-attachment:${REF}`,
              alt: "chart",
            },
          ],
        },
        {
          type: "code",
          lang: "md",
          value: `![chart](knowledge-attachment:${REF})`,
        },
      ],
    };

    const transform = remarkKnowledgeImages();
    transform(tree);

    expect(tree.children[0]).toMatchObject({
      children: [
        {
          type: "image",
          url: `/__knowledge-image/${REF}`,
          alt: "chart",
          data: {
            hProperties: {
              dataKnowledgeImageRef: REF,
            },
          },
        },
      ],
    });
    expect(tree.children[1]).toMatchObject({
      type: "code",
      value: `![chart](knowledge-attachment:${REF})`,
    });
  });

  it.each([
    "https://images.example/track.png",
    "http://images.example/track.png",
    "javascript:alert(1)",
    "data:image/svg+xml,<svg onload=alert(1) />",
    "knowledge-attachment:../secret",
    `knowledge-attachment:${"A".repeat(64)}`,
    `/__knowledge-image/${REF}`,
    `/__knowledge-image/${REF}/extra`,
  ])("does not authorize %s", (url) => {
    expect(knowledgeImageRefFromPlaceholder(url, undefined)).toBeNull();
  });

  it("requires a matching plugin marker for the exact internal placeholder", () => {
    expect(
      knowledgeImageRefFromPlaceholder(`/__knowledge-image/${REF}`, REF),
    ).toBe(REF);
    expect(
      knowledgeImageRefFromPlaceholder(
        `/__knowledge-image/${REF}`,
        "b".repeat(64),
      ),
    ).toBeNull();
  });

  it("does not resolve a directly authored exact placeholder without a marker", () => {
    expect(
      knowledgeImageRefFromPlaceholder(`/__knowledge-image/${REF}`, undefined),
    ).toBeNull();
  });
});
