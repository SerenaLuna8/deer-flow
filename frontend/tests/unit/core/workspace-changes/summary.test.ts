import { describe, expect, test } from "@rstest/core";

import { getWorkspaceChangeBadgeLabel } from "@/core/workspace-changes/summary";

describe("workspace change version compatibility", () => {
  test("keeps v1 numeric zero counts visible as known values", () => {
    expect(
      getWorkspaceChangeBadgeLabel({
        created: 1,
        modified: 0,
        deleted: 0,
        additions: 0,
        deletions: 0,
        truncated: false,
      }),
    ).toBe("1 file changed +0 -0");
  });

  test("omits v2 nullable counts instead of fabricating zero", () => {
    expect(
      getWorkspaceChangeBadgeLabel({
        created: 1,
        modified: 0,
        deleted: 0,
        additions: null,
        deletions: null,
        truncated: false,
      }),
    ).toBe("1 file changed");
  });
});
