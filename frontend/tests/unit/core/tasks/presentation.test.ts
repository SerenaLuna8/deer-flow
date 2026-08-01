import { describe, expect, it } from "@rstest/core";

import {
  formatSubtaskTokenUsage,
  resolveSubtaskModelLabel,
} from "@/core/tasks/presentation";

describe("subtask runtime presentation", () => {
  it("prefers the configured display name and falls back to the model id", () => {
    expect(
      resolveSubtaskModelLabel("claude-3-7-sonnet", [
        {
          name: "claude-3-7-sonnet",
          model: "claude-3-7-sonnet",
          display_name: "Claude 3.7 Sonnet",
          description: "",
          supports_thinking: true,
          supports_reasoning_effort: false,
          supports_vision: false,
          is_default: true,
        },
      ]),
    ).toBe("Claude 3.7 Sonnet");
    expect(resolveSubtaskModelLabel("unlisted-model", [])).toBe(
      "unlisted-model",
    );
  });

  it("formats only reported cumulative token usage", () => {
    expect(formatSubtaskTokenUsage(undefined)).toBeUndefined();
    expect(
      formatSubtaskTokenUsage({
        inputTokens: 10_000,
        outputTokens: 2_345,
        totalTokens: 12_345,
      }),
    ).toBe("12.3K");
  });
});
