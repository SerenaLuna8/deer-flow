import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import { getRunDurationDisplaysByGroupIndex } from "@/core/messages/run-duration";
import type { MessageGroup } from "@/core/messages/utils";

function message(
  id: string,
  type: Message["type"],
  additional_kwargs: Record<string, unknown> = {},
) {
  return {
    id,
    type,
    content: "",
    additional_kwargs,
  } as unknown as Message;
}

describe("getRunDurationDisplaysByGroupIndex", () => {
  test("anchors a Run duration to the end of its visible turn", () => {
    const groups: MessageGroup[] = [
      {
        id: "question",
        type: "human",
        messages: [message("question", "human")],
      },
      {
        id: "answer",
        type: "assistant",
        messages: [
          message("answer", "ai", {
            run_id: "run-1",
            turn_duration: 19,
          }),
        ],
      },
      {
        id: "late-subtask",
        type: "assistant:subagent",
        messages: [message("late-subtask", "ai")],
      },
      {
        id: "next-question",
        type: "human",
        messages: [message("next-question", "human")],
      },
    ];

    const displays = getRunDurationDisplaysByGroupIndex(groups);

    expect(displays[1]).toEqual([]);
    expect(displays[2]).toEqual([{ runId: "run-1", durationSeconds: 19 }]);
    expect(displays[3]).toEqual([]);
  });

  test("does not carry a duration into the next human turn", () => {
    const groups: MessageGroup[] = [
      {
        id: "answer-1",
        type: "assistant",
        messages: [
          message("answer-1", "ai", {
            run_id: "run-1",
            turn_duration: 3,
          }),
        ],
      },
      {
        id: "question-2",
        type: "human",
        messages: [message("question-2", "human")],
      },
      {
        id: "answer-2",
        type: "assistant",
        messages: [
          message("answer-2", "ai", {
            run_id: "run-2",
            turn_duration: 7,
          }),
        ],
      },
    ];

    expect(getRunDurationDisplaysByGroupIndex(groups)).toEqual([
      [{ runId: "run-1", durationSeconds: 3 }],
      [],
      [{ runId: "run-2", durationSeconds: 7 }],
    ]);
  });
});
