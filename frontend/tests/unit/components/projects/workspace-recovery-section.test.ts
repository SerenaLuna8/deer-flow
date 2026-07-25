import { describe, expect, test } from "@rstest/core";

import { formatRecoveryDeadline } from "@/components/projects/workspace-recovery-section";

describe("workspace recovery", () => {
  test("localizes the recovery deadline instead of exposing raw UTC ISO text", () => {
    const value = "2026-08-21T07:30:00Z";
    const formatted = formatRecoveryDeadline(value, "zh-CN");

    expect(formatted).not.toBe(value);
    expect(formatted).toContain("2026");
  });

  test("uses a safe fallback for missing or invalid deadlines", () => {
    expect(formatRecoveryDeadline(null, "zh-CN")).toBe("恢复窗口结束");
    expect(formatRecoveryDeadline("invalid", "en-US")).toBe(
      "recovery window end",
    );
  });
});
