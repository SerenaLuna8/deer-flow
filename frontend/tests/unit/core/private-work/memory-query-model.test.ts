import { describe, expect, test } from "@rstest/core";

import {
  FIRST_MEMORY_VERSION_REQUEST,
  memoryEpisodesFilter,
  nextMemoryEpisodePageParam,
  normalizeMemoryEpisodeSearch,
  parseProjectMemorySelectedVersion,
  parseProjectMemoryTab,
  parseProjectMemoryVersionPage,
  projectMemoryVersionRequest,
} from "@/core/private-work/memory/query-model";

describe("project Memory query model", () => {
  test("normalizes URL state without accepting unsafe version coordinates", () => {
    expect(parseProjectMemoryTab("archive")).toBe("archive");
    expect(parseProjectMemoryTab("other")).toBe("current");
    expect(parseProjectMemorySelectedVersion("42")).toBe(42);
    expect(parseProjectMemorySelectedVersion("0")).toBeNull();
    expect(
      parseProjectMemorySelectedVersion(String(Number.MAX_SAFE_INTEGER + 1)),
    ).toBeNull();
    expect(parseProjectMemoryVersionPage(null)).toBe(0);
    expect(parseProjectMemoryVersionPage("2")).toBe(2);
    expect(parseProjectMemoryVersionPage("201")).toBe(0);
  });

  test("preserves first-page request identity and bounded offsets", () => {
    expect(projectMemoryVersionRequest(0)).toBe(FIRST_MEMORY_VERSION_REQUEST);
    expect(projectMemoryVersionRequest(2)).toEqual({ limit: 51, offset: 100 });
  });

  test("normalizes Unicode search text and omits inactive filters", () => {
    expect(normalizeMemoryEpisodeSearch("   ")).toBeNull();
    expect(normalizeMemoryEpisodeSearch(`  ${"😀".repeat(201)}  `)).toBe(
      "😀".repeat(200),
    );
    expect(memoryEpisodesFilter(null, [])).toEqual({});
    expect(memoryEpisodesFilter("region", ["durable"])).toEqual({
      q: "region",
      tags: ["durable"],
    });
    expect(
      memoryEpisodesFilter(normalizeMemoryEpisodeSearch("😀".repeat(201)), []),
    ).toEqual({ q: "😀".repeat(200) });
  });

  test("continues cursor pages and preserves the legacy before fallback", () => {
    expect(
      nextMemoryEpisodePageParam(
        { items: [{ occurredAt: "2026-08-05T00:00:00Z" }], nextCursor: "c1" },
        false,
      ),
    ).toEqual({ kind: "cursor", value: "c1" });
    expect(
      nextMemoryEpisodePageParam(
        {
          items: [{ occurredAt: "2026-08-05T00:00:00Z" }],
          nextCursor: null,
        },
        false,
      ),
    ).toBeUndefined();
    expect(
      nextMemoryEpisodePageParam(
        {
          items: Array.from({ length: 20 }, (_, index) => ({
            occurredAt: `2026-08-05T00:00:${String(index).padStart(2, "0")}Z`,
          })),
        },
        false,
      ),
    ).toEqual({ kind: "before", value: "2026-08-05T00:00:19Z" });
    expect(
      nextMemoryEpisodePageParam(
        { items: [], nextCursor: "ignored-during-search" },
        true,
      ),
    ).toBeUndefined();
  });
});
