import { describe, expect, test } from "@rstest/core";

import { buildSmoothTokenUsagePath } from "@/components/projects/project-token-usage-view-model";

describe("buildSmoothTokenUsagePath", () => {
  test("builds a cubic bezier path instead of straight segments", () => {
    const path = buildSmoothTokenUsagePath([
      { x: 0, y: 100 },
      { x: 20, y: 20 },
      { x: 40, y: 20 },
    ]);

    expect(path.startsWith("M ")).toBe(true);
    expect(path).toContain(" C ");
    expect(path).not.toMatch(/\sL\s/u);
  });

  test("keeps a single point as a move command", () => {
    expect(buildSmoothTokenUsagePath([{ x: 12, y: 34 }])).toBe("M 12.00 34.00");
  });
});
