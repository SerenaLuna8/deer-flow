import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";

import {
  extractReasoningContentFromMessage,
  hasReasoning,
  reasoningPresentationKind,
} from "@/core/messages/utils";

function aiMessage(content: unknown): Message {
  return {
    type: "ai",
    content,
  } as unknown as Message;
}

test("extracts the Anthropic thinking block shape", () => {
  const message = aiMessage([
    { type: "thinking", thinking: "structured deliberation" },
    { type: "text", text: "visible answer" },
  ]);

  expect(hasReasoning(message)).toBe(true);
  expect(extractReasoningContentFromMessage(message)).toBe(
    "structured deliberation",
  );
});

test("extracts the OpenAI Responses reasoning summary shape", () => {
  // Settled summary entries are complete paragraphs: keep a paragraph break
  // between them instead of gluing the last word of one to the next.
  const message = aiMessage([
    {
      type: "reasoning",
      id: "rs_1",
      summary: [
        { type: "summary_text", text: "planned the answer" },
        { type: "summary_text", text: "then verified it" },
      ],
    },
    { type: "text", text: "visible answer" },
  ]);

  expect(hasReasoning(message)).toBe(true);
  expect(extractReasoningContentFromMessage(message)).toBe(
    "planned the answer\n\nthen verified it",
  );
});

test("merges streamed summary deltas across sibling reasoning blocks", () => {
  const message = aiMessage([
    {
      type: "reasoning",
      summary: [{ type: "summary_text", text: "first part " }],
    },
    {
      type: "reasoning",
      summary: [{ type: "summary_text", text: "second part" }],
    },
    { type: "text", text: "answer" },
  ]);

  expect(extractReasoningContentFromMessage(message)).toBe(
    "first part second part",
  );
});

test("a reasoning block without summary shows nothing and stays hidden", () => {
  const withoutSummary = aiMessage([
    { type: "reasoning", id: "rs_1", encrypted_content: "opaque" },
    { type: "text", text: "visible answer" },
  ]);

  expect(hasReasoning(withoutSummary)).toBe(false);
  expect(extractReasoningContentFromMessage(withoutSummary)).toBeNull();
});

test("ignores summary items that are not summary_text", () => {
  const message = aiMessage([
    {
      type: "reasoning",
      summary: [
        { type: "summary_text", text: "kept" },
        { type: "other_kind", text: "dropped" },
        "not-an-object",
      ],
    },
  ]);

  expect(extractReasoningContentFromMessage(message)).toBe("kept");
});

test("plain text blocks never count as reasoning", () => {
  const message = aiMessage([{ type: "text", text: "just an answer" }]);

  expect(hasReasoning(message)).toBe(false);
  expect(extractReasoningContentFromMessage(message)).toBeNull();
});

test("additional_kwargs reasoning_content still wins over content blocks", () => {
  const message = {
    type: "ai",
    content: [{ type: "text", text: "answer" }],
    additional_kwargs: { reasoning_content: "provider reasoning" },
  } as unknown as Message;

  expect(hasReasoning(message)).toBe(true);
  expect(extractReasoningContentFromMessage(message)).toBe(
    "provider reasoning",
  );
});

test("classifies full chains and summary-only reasoning distinctly", () => {
  // Provider-native full chains: DeepSeek reasoning_content and the
  // Anthropic thinking block.
  const withKwargs = {
    type: "ai",
    content: [],
    additional_kwargs: { reasoning_content: "chain" },
  } as unknown as Message;
  expect(reasoningPresentationKind(withKwargs)).toBe("full");
  expect(
    reasoningPresentationKind(
      aiMessage([{ type: "thinking", thinking: "chain" }]),
    ),
  ).toBe("full");

  // The OpenAI Responses shape carries a model-written summary, not the chain.
  const summaryOnly = aiMessage([
    {
      type: "reasoning",
      summary: [{ type: "summary_text", text: "summary" }],
    },
  ]);
  expect(reasoningPresentationKind(summaryOnly)).toBe("summary");

  // Direct text on a reasoning block outranks an accompanying summary.
  const mixed = aiMessage([
    {
      type: "reasoning",
      reasoning: "chain",
      summary: [{ type: "summary_text", text: "s" }],
    },
  ]);
  expect(reasoningPresentationKind(mixed)).toBe("full");

  expect(
    reasoningPresentationKind(aiMessage([{ type: "text", text: "x" }])),
  ).toBe(null);
  expect(
    reasoningPresentationKind(aiMessage("<think>chain</think>answer")),
  ).toBe("full");
});
