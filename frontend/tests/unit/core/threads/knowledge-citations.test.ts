import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import {
  attachKnowledgeCitationsToFinalAiMessages,
  projectThreadMessages,
  readKnowledgeCitations,
  type KnowledgeCitation,
} from "@/core/threads/message-projection";

function citation(segment: string, position = 1): KnowledgeCitation {
  return {
    knowledge_base_id: "kb-1",
    knowledge_base_name: "产品手册",
    document_id: "doc-1",
    document_name: "发布说明.pdf",
    segment_id: segment,
    segment_position: position,
    snippet: `第 ${position} 段内容`,
    score: 0.9,
    source_position: { page: position },
  };
}

function knowledgeToolMessage(
  id: string,
  runId: string,
  citations: unknown,
  extra: Record<string, unknown> = {},
): Message {
  return {
    type: "tool",
    id,
    name: "knowledge_search",
    tool_call_id: `call-${id}`,
    content: '{"items": []}',
    additional_kwargs: { run_id: runId, knowledge_citations: citations },
    ...extra,
  } as Message;
}

function aiMessage(
  id: string,
  runId: string,
  content: string,
  extraKwargs: Record<string, unknown> = {},
): Message {
  return {
    type: "ai",
    id,
    content,
    additional_kwargs: { run_id: runId, ...extraKwargs },
  } as Message;
}

function citationsOf(message: Message | undefined): unknown {
  return message?.additional_kwargs?.knowledge_citations;
}

test("attaches run-merged citations to the run's final visible AI text message only", () => {
  const messages: Message[] = [
    {
      type: "human",
      id: "h-1",
      content: "问题",
      additional_kwargs: { run_id: "run-1" },
    } as Message,
    knowledgeToolMessage("t-1", "run-1", [
      citation("seg-1", 1),
      citation("seg-2", 2),
    ]),
    aiMessage("ai-mid", "run-1", "中间回答"),
    knowledgeToolMessage("t-2", "run-1", [
      citation("seg-2", 2),
      citation("seg-3", 3),
    ]),
    aiMessage("ai-final", "run-1", "最终回答"),
  ];

  const projected = attachKnowledgeCitationsToFinalAiMessages(messages);

  const finalAi = projected.find((message) => message.id === "ai-final");
  expect(
    (citationsOf(finalAi) as KnowledgeCitation[]).map(
      (item) => item.segment_id,
    ),
  ).toEqual(["seg-1", "seg-2", "seg-3"]);
  expect(citationsOf(projected.find((m) => m.id === "ai-mid"))).toBeUndefined();
  expect(citationsOf(projected.find((m) => m.id === "t-1"))).toBeDefined();
  // Inputs are not mutated: a fresh array is returned only where needed.
  expect(
    citationsOf(messages.find((m) => m.id === "ai-final")),
  ).toBeUndefined();
});

test("scopes citations per run and never leaks across runs", () => {
  const messages: Message[] = [
    knowledgeToolMessage("t-1", "run-1", [citation("seg-a")]),
    aiMessage("ai-1", "run-1", "第一轮回答"),
    knowledgeToolMessage("t-2", "run-2", [citation("seg-b")]),
    aiMessage("ai-2", "run-2", "第二轮回答"),
  ];

  const projected = attachKnowledgeCitationsToFinalAiMessages(messages);

  expect(
    (
      citationsOf(projected.find((m) => m.id === "ai-1")) as KnowledgeCitation[]
    ).map((item) => item.segment_id),
  ).toEqual(["seg-a"]);
  expect(
    (
      citationsOf(projected.find((m) => m.id === "ai-2")) as KnowledgeCitation[]
    ).map((item) => item.segment_id),
  ).toEqual(["seg-b"]);
});

test("skips error tool messages, runless messages, and empty citation lists", () => {
  const messages: Message[] = [
    knowledgeToolMessage("t-error", "run-1", [citation("seg-x")], {
      status: "error",
    }),
    knowledgeToolMessage("t-empty", "run-1", []),
    {
      type: "tool",
      id: "t-runless",
      name: "knowledge_search",
      tool_call_id: "call-runless",
      content: '{"items": []}',
      additional_kwargs: { knowledge_citations: [citation("seg-y")] },
    } as Message,
    aiMessage("ai-1", "run-1", "回答"),
  ];

  const projected = attachKnowledgeCitationsToFinalAiMessages(messages);

  expect(projected).toEqual(messages);
});

test("rejects the whole payload when any citation entry is malformed", () => {
  const missingField = { ...citation("seg-1") } as Record<string, unknown>;
  delete missingField.document_id;
  const badCases: unknown[] = [
    "not-an-array",
    [citation("seg-1"), missingField],
    [{ ...citation("seg-1"), score: "high" }],
    [{ ...citation("seg-1"), segment_position: 1.5 }],
    [{ ...citation("seg-1"), segment_id: "" }],
    [{ ...citation("seg-1"), source_position: null }],
    [null],
  ];

  for (const bad of badCases) {
    const messages: Message[] = [
      knowledgeToolMessage("t-1", "run-1", bad),
      aiMessage("ai-1", "run-1", "回答"),
    ];
    expect(attachKnowledgeCitationsToFinalAiMessages(messages)).toEqual(
      messages,
    );
  }
});

test("ignores other tools and leaves runs without a visible AI text message untouched", () => {
  const otherTool = {
    type: "tool",
    id: "t-other",
    name: "web_search",
    tool_call_id: "call-other",
    content: "results",
    additional_kwargs: {
      run_id: "run-1",
      knowledge_citations: [citation("seg-z")],
    },
  } as Message;
  const withoutAi: Message[] = [
    knowledgeToolMessage("t-1", "run-2", [citation("seg-1")]),
    aiMessage("ai-empty", "run-2", ""),
  ];

  expect(attachKnowledgeCitationsToFinalAiMessages([otherTool])).toEqual([
    otherTool,
  ]);
  expect(attachKnowledgeCitationsToFinalAiMessages(withoutAi)).toEqual(
    withoutAi,
  );
});

test("falls back to the last visible AI text message when the final one is hidden", () => {
  const messages: Message[] = [
    knowledgeToolMessage("t-1", "run-1", [citation("seg-1")]),
    aiMessage("ai-visible", "run-1", "可见回答"),
    aiMessage("ai-hidden", "run-1", "隐藏回答", { hide_from_ui: true }),
  ];

  const projected = attachKnowledgeCitationsToFinalAiMessages(messages);

  expect(
    citationsOf(projected.find((m) => m.id === "ai-visible")),
  ).toBeDefined();
  expect(
    citationsOf(projected.find((m) => m.id === "ai-hidden")),
  ).toBeUndefined();
});

test("skips tool-calling AI messages so citations attach to the real final answer", () => {
  const messages: Message[] = [
    knowledgeToolMessage("t-1", "run-1", [citation("seg-1")]),
    aiMessage("ai-final", "run-1", "最终回答"),
    {
      ...aiMessage("ai-caller", "run-1", "我再查一下"),
      tool_calls: [{ id: "call-x", name: "knowledge_search", args: {} }],
    } as Message,
  ];

  const projected = attachKnowledgeCitationsToFinalAiMessages(messages);

  expect(citationsOf(projected.find((m) => m.id === "ai-final"))).toBeDefined();
  expect(
    citationsOf(projected.find((m) => m.id === "ai-caller")),
  ).toBeUndefined();
});

test("strips pre-seeded knowledge_citations keys the projection did not write", () => {
  // A run with no valid citation-carrying tool message: the pre-seeded key on
  // the AI message must not survive to the renderer.
  const preSeeded = aiMessage("ai-seeded", "run-1", "回答", {
    knowledge_citations: [citation("seg-fake")],
  });
  const stripped = attachKnowledgeCitationsToFinalAiMessages([preSeeded]);
  expect(citationsOf(stripped[0])).toBeUndefined();
  expect(stripped[0]?.additional_kwargs?.run_id).toBe("run-1");
  // The input is not mutated.
  expect(citationsOf(preSeeded)).toBeDefined();

  // With a valid tool message, the selected final message gets the validated
  // payload while a pre-seeded key on another AI message is still stripped.
  const messages: Message[] = [
    knowledgeToolMessage("t-1", "run-1", [citation("seg-real")]),
    aiMessage("ai-mid", "run-1", "中间回答", {
      knowledge_citations: [citation("seg-fake")],
    }),
    aiMessage("ai-final", "run-1", "最终回答"),
  ];
  const attached = attachKnowledgeCitationsToFinalAiMessages(messages);
  expect(citationsOf(attached.find((m) => m.id === "ai-mid"))).toBeUndefined();
  expect(
    (
      citationsOf(
        attached.find((m) => m.id === "ai-final"),
      ) as KnowledgeCitation[]
    ).map((item) => item.segment_id),
  ).toEqual(["seg-real"]);
});

test("projection is idempotent so re-renders never duplicate citations", () => {
  const messages: Message[] = [
    knowledgeToolMessage("t-1", "run-1", [citation("seg-1")]),
    aiMessage("ai-1", "run-1", "回答"),
  ];

  const once = attachKnowledgeCitationsToFinalAiMessages(messages);
  const twice = attachKnowledgeCitationsToFinalAiMessages(once);

  expect(twice).toEqual(once);
});

test("readKnowledgeCitations returns projected citations and rejects everything else", () => {
  const projected = attachKnowledgeCitationsToFinalAiMessages([
    knowledgeToolMessage("t-1", "run-1", [citation("seg-1")]),
    aiMessage("ai-1", "run-1", "回答"),
  ]);
  const finalAi = projected.find((m) => m.id === "ai-1");
  expect(finalAi).toBeDefined();
  expect(
    readKnowledgeCitations(finalAi!).map((item) => item.segment_id),
  ).toEqual(["seg-1"]);

  // Renderer-side reads degrade to "no citations" for anything unvalidated.
  expect(
    readKnowledgeCitations(
      aiMessage("ai-2", "run-2", "无引用回答", {
        knowledge_citations: [{ forged: true }],
      }),
    ),
  ).toEqual([]);
  expect(
    readKnowledgeCitations(
      knowledgeToolMessage("t-3", "run-3", [citation("seg-9")]),
    ),
  ).toEqual([]);
  expect(
    readKnowledgeCitations({
      type: "ai",
      id: "ai-3",
      content: "无 kwargs",
    } as Message),
  ).toEqual([]);
});

test("history replay through projectThreadMessages renders the same citations as the live stream", () => {
  const conversation: Message[] = [
    {
      type: "human",
      id: "h-1",
      content: "问题",
      additional_kwargs: { run_id: "run-1" },
    } as Message,
    knowledgeToolMessage("t-1", "run-1", [
      citation("seg-1"),
      citation("seg-2", 2),
    ]),
    aiMessage("ai-1", "run-1", "带引用的回答"),
  ];
  const base = {
    threadId: "thread-1",
    pendingArchivedMessages: [],
    pendingArchiveThreadId: null,
    activeRunId: null,
    runBaselineMessageIds: new Set<string>(),
    pendingSupersededRunIds: new Set<string>(),
    visibleOptimisticMessages: [],
  };

  const fromHistory = projectThreadMessages({
    ...base,
    visibleHistory: conversation,
    renderMessages: [],
  });
  const fromStream = projectThreadMessages({
    ...base,
    visibleHistory: [],
    renderMessages: conversation,
  });

  const historyCitations = citationsOf(
    fromHistory.find((m) => m.id === "ai-1"),
  );
  expect(historyCitations).toEqual(
    citationsOf(fromStream.find((m) => m.id === "ai-1")),
  );
  expect(
    (historyCitations as KnowledgeCitation[]).map((item) => item.segment_id),
  ).toEqual(["seg-1", "seg-2"]);
});
