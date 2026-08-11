import { describe, expect, it } from "@rstest/core";

import { workflowRunManualRetryAllowed } from "@/core/project-workflows/retry-policy";

describe("workflowRunManualRetryAllowed", () => {
  it("never offers retry for side-effect-unknown even with stale eligibility", () => {
    expect(workflowRunManualRetryAllowed("side_effect_unknown", true)).toBe(
      false,
    );
  });

  it("does not invent eligibility for any other state", () => {
    expect(workflowRunManualRetryAllowed("failed", false)).toBe(false);
    expect(workflowRunManualRetryAllowed("failed", true)).toBe(true);
  });
});
