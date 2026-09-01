import { describe, expect, it } from "@rstest/core";

import {
  parseWarningSummary,
  processingUnitLabel,
} from "@/core/knowledge/processing-profile";

describe("processingUnitLabel", () => {
  it("does not reinterpret a legacy character profile", () => {
    expect(processingUnitLabel(null)).toBe("characters");
    expect(processingUnitLabel({ chunk: { unit: "character" } })).toBe(
      "characters",
    );
    expect(processingUnitLabel({ chunk: { unit: "token" } })).toBe(
      "knowledgeTokens",
    );
  });
});

describe("parseWarningSummary", () => {
  it("counts image failures separately from header and non-failure notices", () => {
    expect(
      parseWarningSummary([
        { code: "HEADER_INFERRED" },
        { code: "IMAGE_CORRUPT" },
        { code: "IMAGE_FIRST_FRAME_ONLY" },
        { code: "IMAGE_LIMIT_EXCEEDED" },
      ]),
    ).toEqual({ total: 4, imageFailures: 2, headerInferred: true });
  });
});
