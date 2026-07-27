import { describe, expect, it } from "@rstest/core";

import {
  decideCoalesce,
  STREAM_RENDER_COALESCE_MS,
} from "@/core/threads/coalesce";

describe("stream render coalescing", () => {
  it("flushes on the leading edge and schedules one bounded trailing edge", () => {
    expect(decideCoalesce(1000, 900, 80, false)).toEqual({
      action: "flush-now",
    });
    expect(decideCoalesce(1000, 950, 80, false)).toEqual({
      action: "schedule",
      delayMs: 30,
    });
    expect(decideCoalesce(1000, 950, 80, true)).toEqual({ action: "wait" });
  });

  it("does not let a late timer suppress an overdue leading-edge flush", () => {
    expect(decideCoalesce(1000, 900, 80, true)).toEqual({
      action: "flush-now",
    });
  });

  it("uses a frame-scale default interval", () => {
    expect(STREAM_RENDER_COALESCE_MS).toBeGreaterThanOrEqual(50);
    expect(STREAM_RENDER_COALESCE_MS).toBeLessThanOrEqual(100);
  });
});
