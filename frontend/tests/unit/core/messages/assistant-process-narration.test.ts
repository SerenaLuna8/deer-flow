import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import { getMessageGroups } from "@/core/messages/utils";

function reasoningToolRound({
  content = "",
  messageId,
  reasoning,
  toolCallId,
}: {
  content?: string;
  messageId: string;
  reasoning: string;
  toolCallId: string;
}): Message {
  return {
    id: messageId,
    type: "ai",
    content,
    additional_kwargs: { reasoning_content: reasoning },
    tool_calls: [
      {
        id: toolCallId,
        name: "read_file",
        args: { path: `/tmp/${messageId}.md` },
      },
    ],
  } as Message;
}

function toolResult(toolCallId: string): Message {
  return {
    id: `${toolCallId}-result`,
    type: "tool",
    name: "read_file",
    tool_call_id: toolCallId,
    content: "ok",
  } as Message;
}

describe("assistant process narration", () => {
  test("keeps a narrated tool round at a stable display position when its tool call arrives", () => {
    const firstRound = reasoningToolRound({
      messageId: "round-1",
      reasoning: "THOUGHT_1",
      toolCallId: "call-1",
    });
    const narrationBeforeToolCall = {
      id: "round-2",
      type: "ai",
      content: "OUTPUT_1",
      additional_kwargs: { reasoning_content: "THOUGHT_2" },
    } as Message;

    const beforeToolCall = getMessageGroups([
      firstRound,
      toolResult("call-1"),
      narrationBeforeToolCall,
    ]);

    expect(beforeToolCall.map((group) => [group.type, group.id])).toEqual([
      ["assistant:processing", "round-1"],
      ["assistant", "round-2"],
    ]);

    const narratedToolRound = reasoningToolRound({
      content: "OUTPUT_1",
      messageId: "round-2",
      reasoning: "THOUGHT_2",
      toolCallId: "call-2",
    });
    const afterToolCall = getMessageGroups([
      firstRound,
      toolResult("call-1"),
      narratedToolRound,
      toolResult("call-2"),
    ]);

    expect(afterToolCall.map((group) => [group.type, group.id])).toEqual([
      ["assistant:processing", "round-1"],
      ["assistant:processing", "round-2"],
    ]);
    expect(afterToolCall[1]?.messages.map((message) => message.id)).toEqual([
      "round-2",
      "call-2-result",
    ]);
  });
});
