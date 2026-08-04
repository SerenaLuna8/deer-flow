import { expect, test } from "@rstest/core";

import {
  buildMessageSidecarContext,
  buildParentConversationContext,
  buildSidecarContextPrompt,
} from "@/core/sidecar/context";

test("builds message sidecar context with a readable label", () => {
  const context = buildMessageSidecarContext(
    { type: "ai", id: "msg-1", content: "A focused answer." },
    3,
  );

  expect(context).toMatchObject({
    type: "referenced_message",
    label: "Assistant message #3",
    messageId: "msg-1",
    role: "assistant",
    content: "A focused answer.",
  });
});

test("builds sidecar context from selected text", () => {
  const context = buildMessageSidecarContext(
    {
      type: "ai",
      id: "msg-1",
      content: "The full answer includes more detail.",
    },
    2,
    { selectedText: "includes more detail" },
  );

  expect(context).toMatchObject({
    type: "referenced_message",
    label: "Selected assistant text #2",
    messageId: "msg-1",
    role: "assistant",
    content: "includes more detail",
  });
});

test("renders hidden sidecar context prompt around quoted material", () => {
  const prompt = buildSidecarContextPrompt({
    type: "referenced_message",
    label: "Selected assistant text #2",
    messageId: "msg-1",
    role: "assistant",
    content: "A side conversation panel.",
  });

  expect(prompt).toContain("You are answering in a side conversation");
  expect(prompt).toContain(
    '<referenced_message index="1" label="Selected assistant text #2">',
  );
  expect(prompt).toContain("Message ID: msg-1");
  expect(prompt).toContain("A side conversation panel.");
});

test("renders hidden sidecar context prompt around multiple quoted materials", () => {
  const prompt = buildSidecarContextPrompt([
    {
      type: "referenced_message",
      label: "Selected assistant text #2",
      messageId: "msg-1",
      role: "assistant",
      content: "First quoted fragment.",
    },
    {
      type: "referenced_message",
      label: "Selected user text #3",
      messageId: "msg-2",
      role: "user",
      content: "Second quoted fragment.",
    },
  ] as never);

  expect(prompt).toContain('referenced_message index="1"');
  expect(prompt).toContain('referenced_message index="2"');
  expect(prompt).toContain("First quoted fragment.");
  expect(prompt).toContain("Second quoted fragment.");
});

test("builds compact parent conversation context from visible messages", () => {
  const parentContext = buildParentConversationContext([
    {
      type: "human",
      id: "parent-human-1",
      content: "Plan the feature.",
    },
    {
      type: "human",
      id: "hidden-context",
      content: "Hidden implementation note.",
      additional_kwargs: { hide_from_ui: true },
    },
    {
      type: "ai",
      id: "parent-ai-1",
      content: "Use a side conversation with cited snippets.",
    },
  ] as never);

  expect(parentContext).toEqual([
    {
      role: "user",
      messageId: "parent-human-1",
      content: "Plan the feature.",
    },
    {
      role: "assistant",
      messageId: "parent-ai-1",
      content: "Use a side conversation with cited snippets.",
    },
  ]);
});

test("omits a verified legacy clarification control reply from parent context", () => {
  const parentContext = buildParentConversationContext([
    {
      type: "tool",
      id: "clarification-request",
      name: "ask_clarification",
      tool_call_id: "call-legacy",
      content: "fallback",
      artifact: {
        human_input: {
          version: 1,
          kind: "human_input_request",
          source: "ask_clarification",
          request_id: "clarification:call-legacy",
          tool_call_id: "call-legacy",
          question: "Which service should be used?",
          input_mode: "free_text",
        },
      },
    },
    {
      type: "human",
      id: "legacy-response",
      content:
        'For your clarification "Which service should be used?", my answer is: Use MCP',
      additional_kwargs: {
        human_input_response: {
          version: 1,
          kind: "human_input_response",
          source: "ask_clarification",
          request_id: "clarification:call-legacy",
          response_kind: "text",
          value: "Use MCP",
        },
      },
    },
    {
      type: "ai",
      id: "public-answer",
      content: "I will query it through MCP.",
    },
  ] as never);

  expect(parentContext).toEqual([
    {
      role: "assistant",
      messageId: "public-answer",
      content: "I will query it through MCP.",
    },
  ]);
});

test("renders parent conversation as read-only background in sidecar prompt", () => {
  const prompt = buildSidecarContextPrompt(
    [
      {
        type: "referenced_message",
        label: "Selected assistant text #2",
        messageId: "msg-1",
        role: "assistant",
        content: "Quoted fragment.",
      },
    ] as never,
    {
      parentConversation: [
        {
          role: "user",
          messageId: "parent-human-1",
          content: "Plan the feature.",
        },
        {
          role: "assistant",
          messageId: "parent-ai-1",
          content: "Use a side conversation.",
        },
      ],
    },
  );

  expect(prompt).toContain("<parent_conversation_context");
  expect(prompt).toContain(
    '<parent_message index="1" role="User" message_id="parent-human-1">',
  );
  expect(prompt).toContain("Plan the feature.");
  expect(prompt).toContain("Use a side conversation.");
  expect(prompt).toContain('referenced_message index="1"');
});

test("renders parent conversation for sidecar follow-ups without new references", () => {
  const prompt = buildSidecarContextPrompt([], {
    parentConversation: [
      {
        role: "assistant",
        messageId: "parent-ai-1",
        content: "Use a side conversation.",
      },
    ],
  });

  expect(prompt).toContain(
    "The user did not attach new referenced messages for this side question.",
  );
  expect(prompt).toContain(
    "Use parent_conversation_context only as continuity background",
  );
  expect(prompt).toContain("<parent_conversation_context");
  expect(prompt).toContain("Use a side conversation.");
  expect(prompt).not.toContain("<referenced_message");
});
