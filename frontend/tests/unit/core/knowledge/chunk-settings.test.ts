import { describe, expect, it } from "@rstest/core";

import {
  isChildChunkSizeValid,
  isChunkOverlapValid,
  isChunkSeparatorValid,
  isChunkSizeValid,
  knowledgeFileExtension,
  normalizeKnowledgeExtension,
  normalizeKnowledgeHeaderRules,
} from "@/core/knowledge/chunk-settings";

const serverLimits = {
  unit: "token" as const,
  tokenizer_profile_id: "knowledge-cl100k-v1",
  parent_min: 100,
  parent_max: 1200,
  parent_max_chars: 4000,
  overlap_max: 80,
  child_min: 50,
  child_max: 600,
};

describe("isChunkSizeValid", () => {
  it("accepts the backend range 200-4000", () => {
    expect(isChunkSizeValid(200)).toBe(true);
    expect(isChunkSizeValid(1000)).toBe(true);
    expect(isChunkSizeValid(4000)).toBe(true);
  });

  it("rejects values the backend would 422", () => {
    expect(isChunkSizeValid(199)).toBe(false);
    expect(isChunkSizeValid(4001)).toBe(false);
    expect(isChunkSizeValid(0)).toBe(false);
    expect(isChunkSizeValid(Number.NaN)).toBe(false);
    expect(isChunkSizeValid(1000.5)).toBe(false);
  });

  it("uses the current server parent bounds", () => {
    const validate = isChunkSizeValid as (
      value: number,
      limits: typeof serverLimits,
    ) => boolean;
    expect(validate(100, serverLimits)).toBe(true);
    expect(validate(1200, serverLimits)).toBe(true);
    expect(validate(99, serverLimits)).toBe(false);
    expect(validate(1201, serverLimits)).toBe(false);
  });
});

describe("isChunkOverlapValid", () => {
  it("accepts 0-500 below the chunk size", () => {
    expect(isChunkOverlapValid(0, 200)).toBe(true);
    expect(isChunkOverlapValid(100, 1000)).toBe(true);
    expect(isChunkOverlapValid(500, 501)).toBe(true);
  });

  it("rejects out-of-range and >= chunk size overlaps", () => {
    expect(isChunkOverlapValid(-1, 1000)).toBe(false);
    expect(isChunkOverlapValid(501, 1000)).toBe(false);
    expect(isChunkOverlapValid(200, 200)).toBe(false);
    expect(isChunkOverlapValid(Number.NaN, 1000)).toBe(false);
  });

  it("uses the current server overlap bound", () => {
    const validate = isChunkOverlapValid as (
      overlap: number,
      size: number,
      limits: typeof serverLimits,
    ) => boolean;
    expect(validate(80, 1000, serverLimits)).toBe(true);
    expect(validate(81, 1000, serverLimits)).toBe(false);
  });
});

describe("isChunkSeparatorValid", () => {
  it("accepts the escaped form up to 64 characters", () => {
    expect(isChunkSeparatorValid("\\n\\n")).toBe(true);
    expect(isChunkSeparatorValid("。")).toBe(true);
    expect(isChunkSeparatorValid("a".repeat(64))).toBe(true);
  });

  it("rejects empty and oversized separators", () => {
    expect(isChunkSeparatorValid("")).toBe(false);
    expect(isChunkSeparatorValid("a".repeat(65))).toBe(false);
  });
});

describe("isChildChunkSizeValid", () => {
  it("accepts 100-2000 below the parent chunk size", () => {
    expect(isChildChunkSizeValid(100, 1000)).toBe(true);
    expect(isChildChunkSizeValid(500, 1000)).toBe(true);
    expect(isChildChunkSizeValid(2000, 4000)).toBe(true);
  });

  it("rejects out-of-range and >= parent sizes", () => {
    expect(isChildChunkSizeValid(99, 1000)).toBe(false);
    expect(isChildChunkSizeValid(2001, 4000)).toBe(false);
    expect(isChildChunkSizeValid(1000, 1000)).toBe(false);
    expect(isChildChunkSizeValid(Number.NaN, 1000)).toBe(false);
  });

  it("uses the current server child bounds", () => {
    const validate = isChildChunkSizeValid as (
      child: number,
      parent: number,
      limits: typeof serverLimits,
    ) => boolean;
    expect(validate(50, 1000, serverLimits)).toBe(true);
    expect(validate(600, 1000, serverLimits)).toBe(true);
    expect(validate(49, 1000, serverLimits)).toBe(false);
    expect(validate(601, 1000, serverLimits)).toBe(false);
  });
});

describe("Knowledge parsing parameter normalization", () => {
  it("normalizes a bare or dotted extension without changing the file name", () => {
    expect(normalizeKnowledgeExtension("PDF")).toBe(".pdf");
    expect(normalizeKnowledgeExtension(".XLSX")).toBe(".xlsx");
    expect(knowledgeFileExtension("Quarter.Final.XLSX")).toBe(".xlsx");
    expect(knowledgeFileExtension("README")).toBeNull();
  });

  it("sorts valid header rules by sheet and rejects invalid row semantics", () => {
    expect(
      normalizeKnowledgeHeaderRules([
        { sheet: "Totals", mode: "none", row: null },
        { sheet: "Data", mode: "explicit", row: 3 },
      ]),
    ).toEqual([
      { sheet: "Data", mode: "explicit", row: 3 },
      { sheet: "Totals", mode: "none", row: null },
    ]);
    expect(() =>
      normalizeKnowledgeHeaderRules([
        { sheet: null, mode: "explicit", row: 0 },
      ]),
    ).toThrow();
    expect(() =>
      normalizeKnowledgeHeaderRules([{ sheet: null, mode: "auto", row: 1 }]),
    ).toThrow();
    expect(() =>
      normalizeKnowledgeHeaderRules([
        { sheet: "Data", mode: "auto", row: null },
        { sheet: "Data", mode: "none", row: null },
      ]),
    ).toThrow();
  });
});
