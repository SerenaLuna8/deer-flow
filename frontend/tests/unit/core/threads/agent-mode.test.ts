import { describe, expect, test } from "@rstest/core";

import {
  getAgentModeRuntimeContext,
  reasoningEffortForMode,
  resolveAgentMode,
} from "@/core/threads/agent-mode";

describe("agent mode presets", () => {
  test.each([
    ["flash", "minimal"],
    ["thinking", "low"],
    ["pro", "medium"],
    ["ultra", "high"],
  ] as const)("%s fixes reasoning effort to %s", (mode, effort) => {
    expect(reasoningEffortForMode(mode)).toBe(effort);
    expect(getAgentModeRuntimeContext(mode).reasoning_effort).toBe(effort);
  });

  test("resolves the default and unsupported-thinking fallbacks", () => {
    expect(resolveAgentMode(undefined, true)).toBe("pro");
    expect(resolveAgentMode(undefined, false)).toBe("flash");
    expect(resolveAgentMode("ultra", false)).toBe("flash");

    expect(
      getAgentModeRuntimeContext(resolveAgentMode(undefined, true)),
    ).toMatchObject({
      thinking_enabled: true,
      reasoning_effort: "medium",
    });
    expect(
      getAgentModeRuntimeContext(resolveAgentMode("ultra", false)),
    ).toMatchObject({
      thinking_enabled: false,
      reasoning_effort: "minimal",
    });
  });

  test("rejects an invalid persisted mode at runtime", () => {
    const invalidMode = "legacy-custom" as never;

    expect(resolveAgentMode(invalidMode, true)).toBe("pro");
    expect(resolveAgentMode(invalidMode, false)).toBe("flash");
    expect(getAgentModeRuntimeContext(invalidMode)).toMatchObject({
      thinking_enabled: true,
      reasoning_effort: "medium",
    });
  });

  test("does not accept a hidden reasoning-effort override", () => {
    const legacyContext = {
      mode: "pro" as const,
      reasoning_effort: "high" as const,
    };

    expect({
      ...legacyContext,
      ...getAgentModeRuntimeContext(legacyContext.mode),
    }).toMatchObject({
      mode: "pro",
      reasoning_effort: "medium",
    });
  });
});
