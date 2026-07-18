import { describe, expect, it } from "@rstest/core";

import {
  buildMemorySectionGroups,
  confidenceToLevelKey,
  countPopulatedSummaries,
  getMemoryCategoryVisual,
  isMemorySummaryEmpty,
  truncateFactPreview,
  upperFirst,
} from "@/components/workspace/settings/memory/memory-view-model";
import { enUS } from "@/core/i18n/locales/en-US";
import type { UserMemory } from "@/core/private-work/memory";

const memory: UserMemory = {
  version: "1",
  lastUpdated: "2026-07-10T00:00:00Z",
  user: {
    workContext: {
      summary: "Prefers source-backed work.",
      updatedAt: "2026-07-10T00:00:00Z",
    },
    personalContext: { summary: "", updatedAt: "" },
    topOfMind: {
      summary: "Redesigning DeerFlow.",
      updatedAt: "2026-07-10T00:00:00Z",
    },
  },
  history: {
    recentMonths: {
      summary: "Forked DeerFlow.",
      updatedAt: "2026-07-10T00:00:00Z",
    },
    earlierContext: { summary: "", updatedAt: "" },
    longTermBackground: { summary: "", updatedAt: "" },
  },
  facts: [],
};

describe("memory view model", () => {
  it("builds the two existing summary groups and counts non-empty sections", () => {
    const groups = buildMemorySectionGroups(memory, enUS);

    expect(groups).toHaveLength(2);
    expect(groups.flatMap((group) => group.sections)).toHaveLength(6);
    expect(countPopulatedSummaries(groups)).toBe(3);
    expect(isMemorySummaryEmpty(memory)).toBe(false);
  });

  it("maps confidence values to the existing localized levels", () => {
    expect(confidenceToLevelKey(0.95)).toEqual({
      key: "veryHigh",
      value: 0.95,
    });
    expect(confidenceToLevelKey(0.7)).toEqual({ key: "high", value: 0.7 });
    expect(confidenceToLevelKey(0.2)).toEqual({ key: "normal", value: 0.2 });
    expect(confidenceToLevelKey(Number.NaN)).toEqual({ key: "unknown" });
  });

  it("normalizes arbitrary category names to stable visual keys", () => {
    expect(getMemoryCategoryVisual("preference")).toBe("preference");
    expect(getMemoryCategoryVisual("PERSONAL")).toBe("preference");
    expect(getMemoryCategoryVisual("work")).toBe("work");
    expect(getMemoryCategoryVisual("project-context")).toBe("project");
    expect(getMemoryCategoryVisual("context")).toBe("context");
    expect(getMemoryCategoryVisual("anything-else")).toBe("default");
  });

  it("keeps preview and label formatting deterministic", () => {
    expect(truncateFactPreview("  a   b  ", 20)).toBe("a b");
    expect(truncateFactPreview("abcdefgh", 6)).toBe("abc...");
    expect(upperFirst("context")).toBe("Context");
  });
});
