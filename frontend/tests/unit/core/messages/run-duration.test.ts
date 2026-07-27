import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, it } from "@rstest/core";

import {
  formatRunDuration,
  getRunDurationDisplaysByGroupIndex,
} from "@/core/messages/run-duration";
import { getMessageGroups } from "@/core/messages/utils";

function message(
  id: string,
  type: Message["type"],
  content: string,
  runId?: string,
  duration?: unknown,
): Message {
  return {
    id,
    type,
    content,
    ...(runId ? { run_id: runId } : {}),
    ...(type === "ai" && duration !== undefined
      ? { additional_kwargs: { turn_duration: duration } }
      : {}),
  } as Message;
}

describe("run duration placement", () => {
  it("renders once after the final visible group belonging to the run", () => {
    const groups = getMessageGroups([
      message("human", "human", "Do it"),
      {
        ...message("ai-tool", "ai", "", "run-1", 114),
        tool_calls: [{ id: "call-1", name: "read_file", args: {} }],
      } as Message,
      {
        ...message("tool", "tool", "done", "run-1"),
        tool_call_id: "call-1",
      } as Message,
      message("ai-final", "ai", "Done", "run-1", 114),
    ]);

    expect(getRunDurationDisplaysByGroupIndex(groups)).toEqual([
      [],
      [],
      [{ runId: "run-1", durationSeconds: 114 }],
    ]);
  });

  it("formats stable compact durations", () => {
    const formatter = {
      lessThanSecond: "<1s",
      hours: (value: number) => `${value}h`,
      minutes: (value: number) => `${value}m`,
      seconds: (value: number) => `${value}s`,
      separator: " ",
    };

    expect(formatRunDuration(0, formatter)).toBe("<1s");
    expect(formatRunDuration(3723.9, formatter)).toBe("1h 2m 3s");
    expect(formatRunDuration(-1, formatter)).toBeNull();
  });
});
