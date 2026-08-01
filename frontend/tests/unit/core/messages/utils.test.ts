import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import {
  extractContentFromMessage,
  extractTextFromMessage,
  extractReasoningContentFromMessage,
  getMessageCopyData,
  getAssistantTurnCopyData,
  getAssistantTurnDisplays,
  getAssistantTurnUsageMessages,
  getLatestEditableTurn,
  getMessageGroups,
  getStreamingMessageLookup,
  hasActiveAssistantReasoning,
  hasContent,
  hasReasoning,
  isClarificationOnlyProcessingGroup,
  isAssistantMessageGroupStreaming,
  isHiddenFromUIMessage,
  parseUploadedFiles,
  stripUploadedFilesTag,
} from "@/core/messages/utils";

function aiMessage(content: string): Message {
  return {
    id: "ai-1",
    type: "ai",
    content,
  } as Message;
}

test("aggregates token usage messages once per assistant turn", () => {
  const messages = [
    {
      id: "human-1",
      type: "human",
      content: "Plan a trip",
    },
    {
      id: "ai-1",
      type: "ai",
      content: "",
      tool_calls: [{ id: "tool-1", name: "web_search", args: {} }],
      usage_metadata: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
    },
    {
      id: "tool-1-result",
      type: "tool",
      name: "web_search",
      tool_call_id: "tool-1",
      content: "[]",
    },
    {
      id: "ai-2",
      type: "ai",
      content: "Here is the itinerary",
      usage_metadata: { input_tokens: 2, output_tokens: 8, total_tokens: 10 },
    },
    {
      id: "human-2",
      type: "human",
      content: "Make it shorter",
    },
    {
      id: "ai-3",
      type: "ai",
      content: "Short version",
      usage_metadata: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
    },
  ] as Message[];

  const groups = getMessageGroups(messages);
  const usageMessagesByGroupIndex = getAssistantTurnUsageMessages(groups);

  expect(groups.map((group) => group.type)).toEqual([
    "human",
    "assistant:processing",
    "assistant",
    "human",
    "assistant",
  ]);

  expect(
    usageMessagesByGroupIndex.map(
      (groupMessages) => groupMessages?.map((message) => message.id) ?? null,
    ),
  ).toEqual([null, null, ["ai-1", "ai-2"], null, ["ai-3"]]);
});

describe("assistant turn display projection", () => {
  test("attaches process and delivered files to the terminal answer", () => {
    const messages = [
      { id: "human-1", type: "human", content: "Create ReopenProof.java" },
      {
        id: "ai-write",
        type: "ai",
        content: "",
        additional_kwargs: { reasoning_content: "Write the requested file." },
        tool_calls: [
          {
            id: "write-1",
            name: "write_file",
            args: { path: "/mnt/user-data/outputs/ReopenProof.java" },
          },
        ],
      },
      {
        id: "write-result",
        type: "tool",
        name: "write_file",
        tool_call_id: "write-1",
        content: "ok",
      },
      {
        id: "ai-present",
        type: "ai",
        content: "Written. Here it is:",
        tool_calls: [
          {
            id: "present-1",
            name: "present_files",
            args: {
              filepaths: [
                "/mnt/user-data/outputs/ReopenProof.java",
                "/mnt/user-data/outputs/ReopenProof.java",
              ],
            },
          },
        ],
      },
      {
        id: "present-result",
        type: "tool",
        name: "present_files",
        tool_call_id: "present-1",
        content: "ok",
      },
      {
        id: "ai-final",
        type: "ai",
        content: "ReopenProof.java is ready.",
        additional_kwargs: { reasoning_content: "Give a concise answer." },
      },
    ] as Message[];

    const groups = getMessageGroups(messages);

    expect(groups.map((group) => group.type)).toEqual([
      "human",
      "assistant:processing",
      "assistant:present-files",
      "assistant",
    ]);
    expect(getAssistantTurnDisplays(groups)).toEqual([
      {
        finalGroupIndex: 3,
        hiddenGroupIndexes: [1, 2],
        processStepCount: 4,
        processGroupIndexes: [1, 2],
        presentFilesGroupIndexes: [2],
        presentedFiles: ["/mnt/user-data/outputs/ReopenProof.java"],
      },
    ]);
  });

  test("keeps present-files visible until a terminal answer exists", () => {
    const messages = [
      { id: "human-1", type: "human", content: "Create a file" },
      {
        id: "ai-present",
        type: "ai",
        content: "Here it is:",
        tool_calls: [
          {
            id: "present-1",
            name: "present_files",
            args: { filepaths: ["/mnt/user-data/outputs/result.txt"] },
          },
        ],
      },
    ] as Message[];

    expect(getAssistantTurnDisplays(getMessageGroups(messages))).toEqual([]);
  });

  test("hides completed subagent execution behind the terminal answer", () => {
    const messages = [
      { id: "human-1", type: "human", content: "Compare two products" },
      {
        id: "ai-task",
        type: "ai",
        content: "",
        additional_kwargs: {
          reasoning_content: "Delegate the calculation.",
          reasoning_duration_ms: 2_000,
        },
        tool_calls: [
          {
            id: "task-1",
            name: "task",
            args: {
              description: "Compare products",
              prompt: "Calculate both products.",
              subagent_type: "general-purpose",
            },
          },
        ],
      },
      {
        id: "task-result",
        type: "tool",
        name: "task",
        tool_call_id: "task-1",
        content: "Calculation completed.",
      },
      {
        id: "ai-final",
        type: "ai",
        content: "The second product is larger.",
        additional_kwargs: {
          reasoning_content: "State the result.",
          reasoning_duration_ms: 3_000,
        },
      },
    ] as Message[];

    const groups = getMessageGroups(messages);

    expect(groups.map((group) => group.type)).toEqual([
      "human",
      "assistant:subagent",
      "assistant",
    ]);
    expect(getAssistantTurnDisplays(groups)).toEqual([
      {
        finalGroupIndex: 2,
        hiddenGroupIndexes: [1],
        processStepCount: 2,
        processGroupIndexes: [1],
        presentFilesGroupIndexes: [],
        presentedFiles: [],
      },
    ]);
  });

  test("keeps subagent execution visible while the terminal answer is streaming", () => {
    const messages = [
      { id: "human-1", type: "human", content: "Compare two products" },
      {
        id: "ai-task",
        type: "ai",
        content: "",
        additional_kwargs: { reasoning_content: "Delegate the work." },
        tool_calls: [
          {
            id: "task-1",
            name: "task",
            args: {
              description: "Compare products",
              prompt: "Calculate both products.",
              subagent_type: "general-purpose",
            },
          },
        ],
      },
      {
        id: "task-result",
        type: "tool",
        name: "task",
        tool_call_id: "task-1",
        content: "Calculation completed.",
      },
      {
        id: "ai-final-streaming",
        type: "ai",
        content: "The second product",
        additional_kwargs: { reasoning_content: "State the result." },
      },
    ] as Message[];

    expect(
      getAssistantTurnDisplays(getMessageGroups(messages), {
        isCurrentTurnLoading: true,
      }),
    ).toEqual([]);
  });

  test("never moves process groups across the next visible human turn", () => {
    const messages = [
      { id: "human-1", type: "human", content: "Start" },
      {
        id: "ai-tool",
        type: "ai",
        content: "",
        tool_calls: [{ id: "tool-1", name: "web_search", args: {} }],
      },
      { id: "human-2", type: "human", content: "Stop and answer" },
      { id: "ai-final", type: "ai", content: "Stopped." },
    ] as Message[];

    expect(getAssistantTurnDisplays(getMessageGroups(messages))).toEqual([]);
  });
});

describe("latest editable user turn", () => {
  test("returns the latest completed human turn", () => {
    const messages = [
      { id: "human-1", type: "human", content: "First question" },
      { id: "ai-history", type: "ai", content: "Historical answer" },
      { id: "human-2", type: "human", content: "Use tools" },
      {
        id: "ai-tool",
        type: "ai",
        content: "",
        tool_calls: [{ id: "tool-1", name: "web_search", args: {} }],
      },
      {
        id: "tool-result",
        type: "tool",
        name: "web_search",
        tool_call_id: "tool-1",
        content: "result",
      },
      { id: "ai-final", type: "ai", content: "Final answer" },
    ] as Message[];

    const turn = getLatestEditableTurn(getMessageGroups(messages), false);

    expect(turn?.humanMessage.id).toBe("human-2");
  });

  test("returns null while the current turn is loading", () => {
    const messages = [
      { id: "human-1", type: "human", content: "Question" },
      { id: "ai-1", type: "ai", content: "Answer" },
    ] as Message[];

    expect(getLatestEditableTurn(getMessageGroups(messages), true)).toBeNull();
  });

  test("returns null when the latest turn has no final assistant text", () => {
    const messages = [
      { id: "human-1", type: "human", content: "Question" },
      {
        id: "ai-tool",
        type: "ai",
        content: "",
        tool_calls: [{ id: "tool-1", name: "web_search", args: {} }],
      },
      {
        id: "tool-result",
        type: "tool",
        name: "web_search",
        tool_call_id: "tool-1",
        content: "result",
      },
    ] as Message[];

    expect(getLatestEditableTurn(getMessageGroups(messages), false)).toBeNull();
  });

  test("returns null when a later human turn is incomplete", () => {
    const messages = [
      { id: "human-1", type: "human", content: "First question" },
      { id: "ai-1", type: "ai", content: "First answer" },
      { id: "human-2", type: "human", content: "Follow up" },
    ] as Message[];

    expect(getLatestEditableTurn(getMessageGroups(messages), false)).toBeNull();
  });
});

test("reasoning + content (no tool calls) yields a single assistant bubble, not a duplicate processing group", () => {
  // Regression for #3868: in thinking/pro/ultra modes the final assistant
  // message carries both reasoning_content and answer text. It must surface its
  // reasoning exactly once — inside the assistant bubble's <Reasoning>
  // collapsible. Routing the same message into a processing group as well makes
  // the ChainOfThought panel above the bubble paint the identical reasoning a
  // second time.
  const messages = [
    { id: "human-1", type: "human", content: "Why is the sky blue?" },
    {
      id: "ai-1",
      type: "ai",
      content: "Rayleigh scattering makes the sky blue.",
      additional_kwargs: { reasoning_content: "Recall Rayleigh scattering." },
    },
  ] as Message[];

  const groups = getMessageGroups(messages);

  expect(groups.map((group) => group.type)).toEqual(["human", "assistant"]);

  // The reasoning-bearing message lands in exactly one group, so turn-usage
  // aggregation never double-counts it (see #2770).
  const turnUsage = getAssistantTurnUsageMessages(groups);
  expect(turnUsage.at(-1)?.map((message) => message.id)).toEqual(["ai-1"]);
});

test("detects reasoning after the latest human turn so a duplicate loading row is not rendered", () => {
  const reasoningTurn = getMessageGroups([
    { id: "human-1", type: "human", content: "Explain it" },
    {
      id: "ai-1",
      type: "ai",
      content: "",
      additional_kwargs: { reasoning_content: "Working through it." },
    },
  ] as Message[]);
  const answerOnlyTurn = getMessageGroups([
    { id: "human-1", type: "human", content: "Explain it" },
    { id: "ai-1", type: "ai", content: "Done." },
  ] as Message[]);

  expect(hasActiveAssistantReasoning(reasoningTurn)).toBe(true);
  expect(hasActiveAssistantReasoning(answerOnlyTurn)).toBe(false);
});

test("keeps tool-call reasoning in the processing group while the final answer's reasoning rides its own bubble", () => {
  // Companion to #3868: only the message that also becomes an assistant bubble
  // (content, no tool calls) is pulled out of the processing group. Reasoning
  // attached to an intermediate tool-calling step still belongs above, with its
  // tool steps.
  const messages = [
    { id: "human-1", type: "human", content: "Search and summarize" },
    {
      id: "ai-1",
      type: "ai",
      content: "",
      additional_kwargs: { reasoning_content: "I should search first." },
      tool_calls: [{ id: "tool-1", name: "web_search", args: { query: "x" } }],
    },
    {
      id: "tool-1-result",
      type: "tool",
      name: "web_search",
      tool_call_id: "tool-1",
      content: "[]",
    },
    {
      id: "ai-2",
      type: "ai",
      content: "Here is the summary.",
      additional_kwargs: { reasoning_content: "Synthesize the findings." },
    },
  ] as Message[];

  const groups = getMessageGroups(messages);

  expect(groups.map((group) => group.type)).toEqual([
    "human",
    "assistant:processing",
    "assistant",
  ]);
  expect(groups[1]?.messages.map((message) => message.id)).toEqual([
    "ai-1",
    "tool-1-result",
  ]);
  expect(groups[2]?.messages.map((message) => message.id)).toEqual(["ai-2"]);
});

test("recognizes clarification-only processing as redundant beside the standalone response card", () => {
  const clarificationOnly = [
    {
      id: "ai-clarification",
      type: "ai",
      content: "",
      additional_kwargs: { reasoning_content: "I need one preference." },
      tool_calls: [
        {
          id: "call-clarification",
          name: "ask_clarification",
          args: { question: "Which direction?" },
        },
      ],
    },
    {
      id: "tool-clarification",
      type: "tool",
      name: "ask_clarification",
      tool_call_id: "call-clarification",
      content: "Which direction?",
    },
  ] as Message[];
  const mixedTools = [
    clarificationOnly[0],
    {
      id: "ai-write",
      type: "ai",
      content: "",
      tool_calls: [
        {
          id: "call-write",
          name: "write_file",
          args: { path: "/tmp/result.md" },
        },
      ],
    },
  ] as Message[];

  expect(isClarificationOnlyProcessingGroup(clarificationOnly)).toBe(true);
  expect(isClarificationOnlyProcessingGroup(mixedTools)).toBe(false);
});

test("keeps a clarification card identity stable when stream hydration replaces the tool message id", () => {
  const request = {
    version: 2,
    kind: "human_input_request",
    source: "ask_clarification",
    request_id: "clarification:call-form",
    tool_call_id: "call-form",
    question: "Please complete the acceptance form.",
    input_mode: "form",
    fields: [
      {
        name: "title",
        label: "Title",
        type: "text",
        required: true,
      },
    ],
  };
  const clarificationGroupId = (messageId: string) =>
    getMessageGroups([
      {
        id: messageId,
        type: "tool",
        name: "ask_clarification",
        tool_call_id: "call-form",
        content: "Please complete the acceptance form.",
        artifact: { human_input: request },
      } as unknown as Message,
    ]).find((group) => group.type === "assistant:clarification")?.id;

  expect(clarificationGroupId("stream-tool-message")).toBe(
    clarificationGroupId("persisted-tool-message"),
  );
});

describe("inline <think> tag splitting", () => {
  test("strips a fully closed <think> block from AI content", () => {
    const message = aiMessage("<think>internal reasoning</think>final answer");
    expect(extractContentFromMessage(message)).toBe("final answer");
    expect(extractReasoningContentFromMessage(message)).toBe(
      "internal reasoning",
    );
  });

  test("strips multiple closed <think> blocks and joins their reasoning", () => {
    const message = aiMessage(
      "<think>step one</think>between<think>step two</think>after",
    );
    expect(extractContentFromMessage(message)).toBe("betweenafter");
    expect(extractReasoningContentFromMessage(message)).toBe(
      "step one\n\nstep two",
    );
  });

  test("during streaming, an unclosed <think> tag does not leak its tail into content", () => {
    // Simulates accumulated content mid-stream, before </think> arrives.
    const message = aiMessage(
      "<think>I need to analyze the user's question step by",
    );
    expect(extractContentFromMessage(message)).toBe("");
    expect(extractContentFromMessage(message)).not.toContain("<think>");
    expect(extractReasoningContentFromMessage(message)).toBe(
      "I need to analyze the user's question step by",
    );
  });

  test("preamble before an unclosed <think> stays in content", () => {
    const message = aiMessage(
      "Here is part of the answer.<think>but wait, let me reconsider",
    );
    expect(extractContentFromMessage(message)).toBe(
      "Here is part of the answer.",
    );
    expect(extractReasoningContentFromMessage(message)).toBe(
      "but wait, let me reconsider",
    );
  });

  test("closed <think> followed by a trailing unclosed <think> merges both into reasoning", () => {
    const message = aiMessage(
      "<think>first step</think>partial answer<think>second step still streaming",
    );
    expect(extractContentFromMessage(message)).toBe("partial answer");
    expect(extractReasoningContentFromMessage(message)).toBe(
      "first step\n\nsecond step still streaming",
    );
  });

  test("hasReasoning recognises an unclosed <think> tag mid-stream", () => {
    expect(hasReasoning(aiMessage("<think>thinking in progress"))).toBe(true);
  });

  test("hasContent excludes an unclosed <think> tail when no preamble exists", () => {
    expect(hasContent(aiMessage("<think>thinking in progress"))).toBe(false);
  });

  test("hasContent stays true when preamble precedes an unclosed <think>", () => {
    expect(hasContent(aiMessage("preamble<think>still thinking"))).toBe(true);
  });

  test("a lone <think> open tag with no body yields no reasoning and no content", () => {
    const message = aiMessage("<think>");
    expect(extractContentFromMessage(message)).toBe("");
    expect(extractReasoningContentFromMessage(message)).toBeNull();
    expect(hasReasoning(message)).toBe(false);
  });

  test("a literal <think> inside markdown inline code is not treated as reasoning", () => {
    const message = aiMessage(
      "Use `<think>` markers to delimit reasoning sections.",
    );
    expect(extractContentFromMessage(message)).toBe(
      "Use `<think>` markers to delimit reasoning sections.",
    );
    expect(extractReasoningContentFromMessage(message)).toBeNull();
    expect(hasReasoning(message)).toBe(false);
  });

  test("a backtick-prefixed <think> mid-stream is not split into reasoning", () => {
    // Simulates the moment the model has emitted the opening backtick and
    // `<think>` for a literal documentation reference, before the closing
    // backtick arrives. The pre-fix behaviour would have permanently
    // truncated the content here.
    const message = aiMessage("Documentation: `<think>");
    expect(extractContentFromMessage(message)).toBe("Documentation: `<think>");
    expect(extractReasoningContentFromMessage(message)).toBeNull();
  });
});

describe("human message internal context stripping", () => {
  test("strips uploaded file context from copy data", () => {
    const message = {
      id: "human-with-upload",
      type: "human",
      content:
        "<uploaded_files>\nThe following files were uploaded in this message:\n\n- paper.pdf (1.0 MB)\n  Path: /mnt/user-data/uploads/paper.pdf\n</uploaded_files>\n\nSummarize this paper",
    } as Message;

    expect(getMessageCopyData(message)).toBe("Summarize this paper");
  });

  test("strips slash skill activation context from display content", () => {
    const content =
      "<slash_skill_activation>\n<skill_content># Secret SKILL.md</skill_content>\n</slash_skill_activation>\nreal user task";

    expect(stripUploadedFilesTag(content)).toBe("real user task");
  });

  test("supports the current_uploads compatibility marker and human sizes", () => {
    const content =
      "<current_uploads>\n- report.pdf (1.5 MB)\n  Path: /mnt/data/report.pdf\n</current_uploads>\nReview it";

    expect(stripUploadedFilesTag(content)).toBe("Review it");
    expect(parseUploadedFiles(content)).toEqual([
      {
        filename: "report.pdf",
        size: 1_572_864,
        path: "/mnt/data/report.pdf",
      },
    ]);
  });

  test("does not read message content for already hidden control messages", () => {
    let reads = 0;
    const hidden = {
      id: "hidden",
      type: "human",
      name: "summary",
      get content() {
        reads += 1;
        return "private context";
      },
    } as unknown as Message;

    expect(isHiddenFromUIMessage(hidden)).toBe(true);
    expect(reads).toBe(0);
  });

  test("hides leaked slash skill activation messages with no user text", () => {
    const messages = [
      {
        id: "slash-activation",
        type: "human",
        content:
          "<slash_skill_activation>\n<skill_content># Secret SKILL.md</skill_content>\n</slash_skill_activation>",
      },
      {
        id: "ai-1",
        type: "ai",
        content: "Public answer",
      },
    ] as Message[];

    const groups = getMessageGroups(messages);

    expect(groups.map((group) => group.type)).toEqual(["assistant"]);
    expect(
      groups.flatMap((group) => group.messages).map((message) => message.id),
    ).toEqual(["ai-1"]);
  });
});

test("hides internal todo reminder messages from message groups", () => {
  const messages = [
    {
      id: "human-1",
      type: "human",
      content: "Audit the middleware",
    },
    {
      id: "todo-reminder-1",
      type: "human",
      name: "todo_completion_reminder",
      content: "<system_reminder>finish todos</system_reminder>",
    },
    {
      id: "todo-reminder-2",
      type: "human",
      name: "todo_reminder",
      content: "<system_reminder>remember todos</system_reminder>",
    },
    {
      id: "ai-1",
      type: "ai",
      content: "Done",
    },
  ] as Message[];

  const groups = getMessageGroups(messages);

  expect(groups.map((group) => group.type)).toEqual(["human", "assistant"]);
  expect(
    groups.flatMap((group) => group.messages).map((message) => message.id),
  ).toEqual(["human-1", "ai-1"]);
});

test("hides assistant copy data while that turn is streaming", () => {
  const messages = [
    {
      id: "ai-1",
      type: "ai",
      content: "Partial answer",
    },
  ] as Message[];

  expect(getAssistantTurnCopyData(messages)).toBe("Partial answer");
  expect(getAssistantTurnCopyData(messages, { isStreaming: true })).toBeNull();
});

test("marks the latest assistant message as streaming", () => {
  const messages = [
    {
      id: "human-1",
      type: "human",
      content: "Hello",
    },
    {
      id: "ai-1",
      type: "ai",
      content: "Still generating",
    },
  ] as Message[];
  const groups = getMessageGroups(messages);
  const assistantGroupIndex = groups.findIndex(
    (group) => group.type === "assistant",
  );

  expect(
    isAssistantMessageGroupStreaming(
      groups[assistantGroupIndex]?.messages ?? [],
      getStreamingMessageLookup(messages, true, () => ({
        streamMetadata: { langgraph_node: "agent" },
      })),
    ),
  ).toBe(true);
  expect(
    isAssistantMessageGroupStreaming(
      groups[assistantGroupIndex]?.messages ?? [],
      getStreamingMessageLookup(messages, false, () => ({
        streamMetadata: { langgraph_node: "agent" },
      })),
    ),
  ).toBe(false);
});

test("keeps previous assistant copyable while waiting for a new visible answer", () => {
  const messages = [
    {
      id: "human-1",
      type: "human",
      content: "Hello",
    },
    {
      id: "ai-1",
      type: "ai",
      content: "Completed answer",
    },
    {
      id: "opt-human-1",
      type: "human",
      content: "Continue",
    },
  ] as Message[];
  const groups = getMessageGroups(messages);
  const assistantGroupIndex = groups.findIndex(
    (group) => group.type === "assistant",
  );

  expect(
    isAssistantMessageGroupStreaming(
      groups[assistantGroupIndex]?.messages ?? [],
      getStreamingMessageLookup(messages, true),
    ),
  ).toBe(false);
});

test("keeps previous assistant copyable while a hidden send is starting", () => {
  const messages = [
    {
      id: "human-1",
      type: "human",
      content: "Hello",
    },
    {
      id: "ai-1",
      type: "ai",
      content: "Completed answer",
    },
  ] as Message[];
  const groups = getMessageGroups(messages);
  const assistantGroupIndex = groups.findIndex(
    (group) => group.type === "assistant",
  );

  expect(
    isAssistantMessageGroupStreaming(
      groups[assistantGroupIndex]?.messages ?? [],
      getStreamingMessageLookup(messages, true),
    ),
  ).toBe(false);
});

test("keeps previous assistant copyable after a hidden send is appended", () => {
  const messages = [
    {
      id: "human-1",
      type: "human",
      content: "Hello",
    },
    {
      id: "ai-1",
      type: "ai",
      content: "Completed answer",
    },
    {
      id: "human-hidden",
      type: "human",
      content: "Save this agent",
      additional_kwargs: { hide_from_ui: true },
    },
  ] as Message[];
  const groups = getMessageGroups(messages);
  const assistantGroupIndex = groups.findIndex(
    (group) => group.type === "assistant",
  );

  expect(
    isAssistantMessageGroupStreaming(
      groups[assistantGroupIndex]?.messages ?? [],
      getStreamingMessageLookup(messages, true),
    ),
  ).toBe(false);
});

test("uses stream metadata to identify an assistant before optimistic input", () => {
  const messages = [
    {
      id: "human-1",
      type: "human",
      content: "Hello",
    },
    {
      id: "ai-1",
      type: "ai",
      content: "Completed answer",
    },
    {
      id: "ai-2",
      type: "ai",
      content: "Still generating",
    },
    {
      id: "opt-human-1",
      type: "human",
      content: "Continue",
    },
  ] as Message[];
  const assistantGroups = getMessageGroups(messages).filter(
    (group) => group.type === "assistant",
  );
  const groups = getMessageGroups(messages);
  const assistantGroupIndexes = groups
    .map((group, index) => (group.type === "assistant" ? index : -1))
    .filter((index) => index >= 0);

  expect(
    isAssistantMessageGroupStreaming(
      groups[assistantGroupIndexes[0] ?? -1]?.messages ?? [],
      getStreamingMessageLookup(messages, true, (message) =>
        message.id === "ai-2"
          ? { streamMetadata: { langgraph_node: "agent" } }
          : undefined,
      ),
    ),
  ).toBe(false);
  expect(
    isAssistantMessageGroupStreaming(
      groups[assistantGroupIndexes[1] ?? -1]?.messages ?? [],
      getStreamingMessageLookup(messages, true, (message) =>
        message.id === "ai-2"
          ? { streamMetadata: { langgraph_node: "agent" } }
          : undefined,
      ),
    ),
  ).toBe(true);
  expect(assistantGroups.map((group) => group.id)).toEqual(["ai-1", "ai-2"]);
});

test("does not mark a completed assistant group streaming from a later processing group", () => {
  const messages = [
    {
      id: "human-1",
      type: "human",
      content: "Hello",
    },
    {
      id: "ai-1",
      type: "ai",
      content: "Visible answer",
    },
    {
      id: "ai-2",
      type: "ai",
      content: "",
      tool_calls: [{ id: "tool-1", name: "web_search", args: {} }],
    },
  ] as Message[];
  const groups = getMessageGroups(messages);
  const assistantGroupIndex = groups.findIndex(
    (group) => group.type === "assistant",
  );

  expect(groups.map((group) => group.type)).toEqual([
    "human",
    "assistant",
    "assistant:processing",
  ]);
  expect(
    isAssistantMessageGroupStreaming(
      groups[assistantGroupIndex]?.messages ?? [],
      getStreamingMessageLookup(messages, true, (message) =>
        message.id === "ai-2"
          ? { streamMetadata: { langgraph_node: "agent" } }
          : undefined,
      ),
    ),
  ).toBe(false);
});

test("keeps streaming assistant hidden when a hidden control message follows it", () => {
  const messages = [
    {
      id: "human-1",
      type: "human",
      content: "Hello",
    },
    {
      id: "ai-1",
      type: "ai",
      content: "Still generating",
    },
    {
      id: "human-hidden",
      type: "human",
      content: "Save this agent",
      additional_kwargs: { hide_from_ui: true },
    },
  ] as Message[];
  const groups = getMessageGroups(messages);
  const assistantGroupIndex = groups.findIndex(
    (group) => group.type === "assistant",
  );

  expect(
    isAssistantMessageGroupStreaming(
      groups[assistantGroupIndex]?.messages ?? [],
      getStreamingMessageLookup(messages, true, (message) =>
        message.id === "ai-1"
          ? { streamMetadata: { langgraph_node: "agent" } }
          : undefined,
      ),
    ),
  ).toBe(true);
});

describe("multi-part content with bare-string continuations", () => {
  // Gemini streams the first content block as a {type:"text"} object carrying
  // the thinking signature, then emits continuation deltas as plain strings.
  // LangChain's Python merge_content preserves these as bare-string elements,
  // so the finalized message content is [{type:"text", ...}, "...rest..."].
  const geminiMessage = {
    id: "ai-1",
    type: "ai",
    content: [
      {
        type: "text",
        text: "First block carrying the signature.",
        extras: { signature: "abc123" },
        index: 0,
      },
      "Continuation streamed as a bare string.",
    ],
  } as unknown as Message;

  test("extractContentFromMessage includes the bare-string parts", () => {
    expect(extractContentFromMessage(geminiMessage)).toBe(
      "First block carrying the signature.\nContinuation streamed as a bare string.",
    );
  });

  test("extractTextFromMessage includes the bare-string parts", () => {
    expect(extractTextFromMessage(geminiMessage)).toBe(
      "First block carrying the signature.\nContinuation streamed as a bare string.",
    );
  });
});

describe("tool result association", () => {
  test("scopes repeated tool call ids to their owning run", () => {
    const messages = [
      { id: "h-a", type: "human", content: "first", run_id: "run-a" },
      {
        id: "ai-a",
        type: "ai",
        content: "",
        run_id: "run-a",
        tool_calls: [{ id: "call-1", name: "bash", args: {} }],
      },
      { id: "h-b", type: "human", content: "second", run_id: "run-b" },
      {
        id: "ai-b",
        type: "ai",
        content: "",
        additional_kwargs: { run_id: "run-b" },
        tool_calls: [{ id: "call-1", name: "bash", args: {} }],
      },
      {
        id: "t-b",
        type: "tool",
        name: "bash",
        additional_kwargs: { run_id: "run-b" },
        tool_call_id: "call-1",
        content: "run-b output",
      },
      {
        id: "t-a-late",
        type: "tool",
        name: "bash",
        run_id: "run-a",
        tool_call_id: "call-1",
        content: "run-a output",
      },
    ] as unknown as Message[];

    const groups = getMessageGroups(messages);

    expect(groups.map((group) => group.type)).toEqual([
      "human",
      "assistant:processing",
      "human",
      "assistant:processing",
    ]);
    expect(groups[1]?.messages.map((message) => message.id)).toEqual([
      "ai-a",
      "t-a-late",
    ]);
    expect(groups[3]?.messages.map((message) => message.id)).toEqual([
      "ai-b",
      "t-b",
    ]);
  });

  test("does not guess across an explicit run when a legacy message lacks run_id", () => {
    const messages = [
      { id: "h-a", type: "human", content: "legacy first turn" },
      {
        id: "ai-a",
        type: "ai",
        content: "",
        tool_calls: [{ id: "call-1", name: "bash", args: {} }],
      },
      {
        id: "h-b",
        type: "human",
        content: "scoped second turn",
        run_id: "run-b",
      },
      {
        id: "ai-b",
        type: "ai",
        content: "",
        run_id: "run-b",
        tool_calls: [{ id: "call-1", name: "bash", args: {} }],
      },
      {
        id: "t-ambiguous",
        type: "tool",
        name: "bash",
        tool_call_id: "call-1",
        content: "missing owner",
      },
    ] as unknown as Message[];

    const groups = getMessageGroups(messages);

    expect(groups[1]?.messages.map((message) => message.id)).toEqual(["ai-a"]);
    expect(groups[3]?.messages.map((message) => message.id)).toEqual(["ai-b"]);
    expect(
      groups
        .flatMap((group) => group.messages)
        .some((message) => message.id === "t-ambiguous"),
    ).toBe(false);
  });

  test("attaches a late tool result to the AI group that issued its tool call", () => {
    const messages = [
      { id: "h-1", type: "human", content: "Run something" },
      {
        id: "ai-1",
        type: "ai",
        content: "ok",
        tool_calls: [{ id: "call-1", name: "bash", args: {} }],
      },
      {
        id: "t-1",
        type: "tool",
        name: "bash",
        tool_call_id: "call-1",
        content: "output-1",
      },
      { id: "ai-2", type: "ai", content: "Done." }, // terminal assistant group
      // Replayed result arrives after the terminal answer. It still belongs to
      // call-1's processing group, never to the visible answer bubble.
      {
        id: "t-1-replay",
        type: "tool",
        name: "bash",
        tool_call_id: "call-1",
        content: "output-1 replay",
      },
    ] as Message[];

    const groups = getMessageGroups(messages);

    expect(groups.map((group) => group.type)).toEqual([
      "human",
      "assistant:processing",
      "assistant",
    ]);
    expect(groups[1]?.messages.map((message) => message.id)).toEqual([
      "ai-1",
      "t-1",
      "t-1-replay",
    ]);
    expect(groups[2]?.messages.map((message) => message.id)).toEqual(["ai-2"]);
  });

  test("holds an early tool result until its issuing AI tool call arrives", () => {
    const messages = [
      { id: "h-1", type: "human", content: "q" },
      {
        id: "t-early",
        type: "tool",
        name: "web_search",
        tool_call_id: "call-x",
        content: "early delivery",
      },
      {
        id: "ai-1",
        type: "ai",
        content: "",
        tool_calls: [{ id: "call-x", name: "web_search", args: {} }],
      },
      { id: "ai-2", type: "ai", content: "Done." },
    ] as Message[];

    const groups = getMessageGroups(messages);

    expect(groups.map((group) => group.type)).toEqual([
      "human",
      "assistant:processing",
      "assistant",
    ]);
    expect(groups[1]?.messages.map((message) => message.id)).toEqual([
      "ai-1",
      "t-early",
    ]);
    expect(groups[2]?.messages.map((message) => message.id)).toEqual(["ai-2"]);
  });

  test("does not attach an unknown orphan tool result to a terminal group", () => {
    const messages = [
      { id: "h-1", type: "human", content: "q" },
      { id: "ai-1", type: "ai", content: "Done." },
      {
        id: "t-unknown",
        type: "tool",
        name: "bash",
        tool_call_id: "never-issued",
        content: "must not appear in the answer bubble",
      },
    ] as Message[];

    const groups = getMessageGroups(messages);

    expect(groups.map((group) => group.type)).toEqual(["human", "assistant"]);
    expect(
      groups.flatMap((group) => group.messages.map((message) => message.id)),
    ).toEqual(["h-1", "ai-1"]);
  });

  test("gives groups unique non-empty ids when source messages have null ids", () => {
    const messages = [
      { id: null, type: "human", content: "q" },
      { id: null, type: "ai", content: "first answer" },
      { id: null, type: "human", content: "follow-up" },
      { id: null, type: "ai", content: "second answer" },
    ] as unknown as Message[];

    const groups = getMessageGroups(messages);

    expect(groups.map((group) => group.id)).toEqual([
      "human-0",
      "assistant-1",
      "human-2",
      "assistant-3",
    ]);
    expect(new Set(groups.map((group) => group.id)).size).toBe(groups.length);
  });
});
