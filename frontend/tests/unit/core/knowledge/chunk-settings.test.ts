import { describe, expect, it } from "@rstest/core";

import {
  isChildChunkSizeValid,
  isChunkOverlapValid,
  isChunkSeparatorValid,
  isChunkSizeValid,
} from "@/core/knowledge/chunk-settings";

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
});
