import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import { getReasoningDurationSeconds } from "@/core/messages/utils";

test("reads the persisted server-observed reasoning duration in whole seconds", () => {
  const message = {
    type: "ai",
    content: "answer",
    additional_kwargs: {
      reasoning_content: "analysis",
      reasoning_duration_ms: 19_400,
    },
  } as Message;

  expect(getReasoningDurationSeconds(message)).toBe(19);
});

test("rejects missing, negative, non-finite, and implausibly large durations", () => {
  const message = (value: unknown) =>
    ({
      type: "ai",
      content: "answer",
      additional_kwargs: { reasoning_duration_ms: value },
    }) as Message;

  expect(getReasoningDurationSeconds(message(undefined))).toBeUndefined();
  expect(getReasoningDurationSeconds(message(-1))).toBeUndefined();
  expect(getReasoningDurationSeconds(message(Number.NaN))).toBeUndefined();
  expect(getReasoningDurationSeconds(message(Number.POSITIVE_INFINITY))).toBeUndefined();
  expect(getReasoningDurationSeconds(message(86_400_001))).toBeUndefined();
});

test("preserves a sub-second observed reasoning window for truthful formatting", () => {
  const message = {
    type: "ai",
    content: "answer",
    additional_kwargs: { reasoning_duration_ms: 250 },
  } as Message;

  expect(getReasoningDurationSeconds(message)).toBe(0);
});
