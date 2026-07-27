import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import { extractWriteArtifactSelections } from "@/core/artifacts/preview";
import { filterLeadAgentStreamMessages } from "@/core/messages/lead-stream-visibility";

const subagentMetadata = {
  streamMetadata: {
    tags: ["subagent:general-purpose"],
  },
};

test("removes streamed subagent reasoning and its tool results from the lead conversation", () => {
  const subagentAI = {
    id: "subagent-ai",
    type: "ai",
    content: "I will write a temporary report.",
    tool_calls: [
      {
        id: "subagent-write",
        name: "write_file",
        args: {
          path: "/mnt/user-data/workspace/subagent-draft.md",
          content: "# Draft",
        },
      },
    ],
  } as Message;
  const messages = [
    {
      id: "lead-ai",
      type: "ai",
      content: "",
      tool_calls: [
        {
          id: "lead-task",
          name: "task",
          args: {
            description: "Create a report",
            prompt: "Create a report",
            subagent_type: "general-purpose",
          },
        },
      ],
    },
    subagentAI,
    {
      id: "subagent-tool",
      type: "tool",
      tool_call_id: "subagent-write",
      content: "OK",
    },
  ] as Message[];

  const visibleMessages = filterLeadAgentStreamMessages(
    messages,
    (message) => (message === subagentAI ? subagentMetadata : undefined),
  );

  expect(visibleMessages.map((message) => message.id)).toEqual(["lead-ai"]);
  expect(extractWriteArtifactSelections(visibleMessages)).toEqual([]);
});

test("keeps lead Agent file writes eligible for automatic preview", () => {
  const messages = [
    {
      id: "lead-ai",
      type: "ai",
      content: "",
      tool_calls: [
        {
          id: "lead-write",
          name: "write_file",
          args: {
            path: "/mnt/user-data/outputs/final-report.md",
            content: "# Final report",
          },
        },
      ],
    },
  ] as Message[];

  const visibleMessages = filterLeadAgentStreamMessages(
    messages,
    () => ({
      streamMetadata: {
        tags: ["lead_agent"],
      },
    }),
  );

  expect(visibleMessages).toBe(messages);
  expect(extractWriteArtifactSelections(visibleMessages)).toEqual([
    {
      key: "lead-ai/lead-write",
      url: "write-file:/mnt/user-data/outputs/final-report.md?message_id=lead-ai&tool_call_id=lead-write",
    },
  ]);
});

test("uses the subagent tag only and does not hide ordinary tool namespaces", () => {
  const message = {
    id: "lead-ai",
    type: "ai",
    content: "",
    tool_calls: [
      {
        id: "lead-search",
        name: "web_search",
        args: { query: "LangGraph" },
      },
    ],
  } as Message;

  expect(
    filterLeadAgentStreamMessages([message], () => ({
      streamMetadata: {
        checkpoint_ns: "tools:lead-search",
        tags: ["lead_agent"],
      },
    })),
  ).toEqual([message]);
});
