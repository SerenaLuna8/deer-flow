import { describe, expect, test } from "@rstest/core";

import { getPromptInputEnterAction } from "@/components/ai-elements/prompt-input";

describe("PromptInput Enter keyboard contract", () => {
  test("submits on Enter and inserts a newline on Shift+Enter", () => {
    expect(
      getPromptInputEnterAction({
        key: "Enter",
        shiftKey: false,
        isComposing: false,
      }),
    ).toBe("submit");
    expect(
      getPromptInputEnterAction({
        key: "Enter",
        shiftKey: true,
        isComposing: false,
      }),
    ).toBe("newline");
  });

  test("does not submit during IME composition or for another key", () => {
    expect(
      getPromptInputEnterAction({
        key: "Enter",
        shiftKey: false,
        isComposing: true,
      }),
    ).toBe("ignore");
    expect(
      getPromptInputEnterAction({
        key: "a",
        shiftKey: false,
        isComposing: false,
      }),
    ).toBe("ignore");
  });
});
