import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import {
  getAssistantTurnDisplays,
  getMessageGroups,
} from "@/core/messages/utils";

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

  test("keeps narrated process output outside the completed turn disclosure", () => {
    const narratedToolRound = reasoningToolRound({
      content: "OUTPUT_1",
      messageId: "round-1",
      reasoning: "THOUGHT_1",
      toolCallId: "call-1",
    });
    const finalAnswer = {
      id: "final-answer",
      type: "ai",
      content: "FINAL_OUTPUT",
      additional_kwargs: { reasoning_content: "FINAL_THOUGHT" },
    } as Message;
    const groups = getMessageGroups([
      narratedToolRound,
      toolResult("call-1"),
      finalAnswer,
    ]);

    expect(groups.map((group) => group.type)).toEqual([
      "assistant:processing",
      "assistant",
    ]);
    expect(getAssistantTurnDisplays(groups)).toEqual([]);
  });

  test("does not treat Anthropic thinking blocks as visible process output", () => {
    const anthropicThinkingRound = (messageId: string, toolCallId: string) =>
      ({
        id: messageId,
        type: "ai",
        content: [
          {
            type: "thinking",
            thinking: `THOUGHT_${messageId}`,
            signature: `signature-${messageId}`,
          },
        ],
        tool_calls: [
          {
            id: toolCallId,
            name: "read_file",
            args: { path: `/tmp/${messageId}.md` },
          },
        ],
      }) as unknown as Message;
    const finalAnswer = {
      id: "final-answer",
      type: "ai",
      content: "FINAL_OUTPUT",
    } as Message;
    const groups = getMessageGroups([
      anthropicThinkingRound("round-1", "call-1"),
      toolResult("call-1"),
      anthropicThinkingRound("round-2", "call-2"),
      toolResult("call-2"),
      finalAnswer,
    ]);

    expect(groups.map((group) => group.type)).toEqual([
      "assistant:processing",
      "assistant",
    ]);
    expect(getAssistantTurnDisplays(groups)).toHaveLength(1);
  });
});
