import { describe, expect, it } from "@rstest/core";

import {
  formatKnowledgeSourcePosition,
  formatKnowledgeSourceSpans,
  type KnowledgeSourcePositionLabels,
} from "@/core/knowledge/source-position";

const labels: KnowledgeSourcePositionLabels = {
  page: (page) => `页 ${page}`,
  paragraph: (paragraph) => `段落 ${paragraph}`,
  table: (table) => `表 ${table}`,
  row: (row) => `行 ${row}`,
  slide: (slide) => `幻灯片 ${slide}`,
  chapter: (chapter) => `章节 ${chapter}`,
};

describe("formatKnowledgeSourcePosition", () => {
  it("renders the PDF page marker", () => {
    expect(formatKnowledgeSourcePosition({ page: 3 }, labels)).toBe("页 3");
  });

  it("renders the DOCX paragraph marker", () => {
    expect(formatKnowledgeSourcePosition({ paragraph: 12 }, labels)).toBe(
      "段落 12",
    );
  });

  it("renders the DOCX table and row together", () => {
    expect(formatKnowledgeSourcePosition({ table: 1, row: 2 }, labels)).toBe(
      "表 1 · 行 2",
    );
  });

  it("renders the CSV row marker", () => {
    expect(formatKnowledgeSourcePosition({ row: 8 }, labels)).toBe("行 8");
  });

  it("renders the PPTX slide marker", () => {
    expect(formatKnowledgeSourcePosition({ slide: 4 }, labels)).toBe(
      "幻灯片 4",
    );
  });

  it("renders the EPUB chapter marker", () => {
    expect(formatKnowledgeSourcePosition({ chapter: 2 }, labels)).toBe(
      "章节 2",
    );
  });

  it("renders the XLSX sheet and row pair", () => {
    expect(
      formatKnowledgeSourcePosition({ sheet: "Sheet1", row: 5 }, labels),
    ).toBe("Sheet1 · 行 5");
  });

  it("returns null for plain-text segments without provenance", () => {
    expect(formatKnowledgeSourcePosition({}, labels)).toBeNull();
  });

  it("never leaks unknown payload keys", () => {
    expect(
      formatKnowledgeSourcePosition({ secret: "value", offset: 42 }, labels),
    ).toBeNull();
  });

  it("ignores non-scalar values instead of rendering garbage", () => {
    expect(
      formatKnowledgeSourcePosition({ page: { nested: true } }, labels),
    ).toBeNull();
    expect(
      formatKnowledgeSourcePosition({ page: Number.NaN }, labels),
    ).toBeNull();
  });

  it("keeps every source span and its source or context role", () => {
    expect(
      formatKnowledgeSourceSpans(
        [
          {
            location: { page: 2 },
            role: "source",
          },
          {
            location: { page: 1 },
            role: "context_prefix",
          },
        ],
        labels,
      ),
    ).toEqual([
      { position: "页 2", role: "source" },
      { position: "页 1", role: "context_prefix" },
    ]);
  });

  it("does not reinterpret encoding metadata as a page", () => {
    expect(
      formatKnowledgeSourceSpans(
        [{ location: { encoding: "gb18030" }, role: "source" }],
        labels,
      ),
    ).toEqual([]);
  });
});
