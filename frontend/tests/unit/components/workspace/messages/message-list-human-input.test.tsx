import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageList } from "@/components/workspace/messages/message-list";
import { I18nProvider } from "@/core/i18n/context";
import type { AgentThreadState } from "@/core/threads";

const requestMessage = {
  id: "clarification:call-form",
  type: "tool",
  name: "ask_clarification",
  tool_call_id: "call-form",
  content: "Provide deployment details",
  artifact: {
    human_input: {
      version: 2,
      kind: "human_input_request",
      source: "ask_clarification",
      request_id: "clarification:call-form",
      tool_call_id: "call-form",
      question: "Provide deployment details",
      input_mode: "form",
      fields: [
        {
          name: "environment",
          label: "Environment",
          type: "text",
          required: true,
        },
      ],
    },
  },
} as unknown as Message;

const requestCallMessage = {
  id: "request-call",
  type: "ai",
  content: "Provide deployment details",
  tool_calls: [
    {
      id: "call-form",
      name: "ask_clarification",
      args: { question: "Provide deployment details" },
    },
  ],
} as unknown as Message;

function renderMessageList(
  isHistoryLoading: boolean,
  messages: Message[] = [requestMessage],
) {
  const thread = {
    error: undefined,
    getMessagesMetadata: () => undefined,
    isLoading: false,
    isThreadLoading: false,
    messages,
  } as unknown as BaseStream<AgentThreadState>;

  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
      <StandaloneArtifactsProvider enabled={false}>
        <MessageList
          isHistoryLoading={isHistoryLoading}
          onSubmitHumanInput={() => true}
          thread={thread}
          threadId="thread-1"
        />
      </StandaloneArtifactsProvider>
    </I18nProvider>,
  );
}

function submitButton(html: string) {
  const match = /<button[^>]*>Submit answer<\/button>/.exec(html);
  expect(match).not.toBeNull();
  expect(match![0]).toContain('type="submit"');
  return match![0];
}

describe("MessageList human input hydration", () => {
  test("keeps an apparently open response card disabled while history loads", () => {
    const html = renderMessageList(true);

    expect(html).toContain('data-human-input-state="open"');
    expect(submitButton(html)).toContain('disabled=""');
  });

  test("enables the latest open response only after history settles", () => {
    const html = renderMessageList(false);

    expect(html).toContain('data-human-input-state="open"');
    expect(submitButton(html)).not.toContain('disabled=""');
  });

  test("renders an ask_clarification question only in its response card", () => {
    const html = renderMessageList(false, [requestCallMessage, requestMessage]);

    expect(html.split("Provide deployment details")).toHaveLength(2);
  });
});
