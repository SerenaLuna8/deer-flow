import type { Message } from "@langchain/langgraph-sdk";
import { expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts/context";
import { MessageGroup } from "@/components/workspace/messages/message-group";
import { I18nContext } from "@/core/i18n/context";

test("shows every reasoning message after the last tool in completed process history", () => {
  const messages = [
    {
      id: "ai-search",
      type: "ai",
      content: "",
      additional_kwargs: {
        reasoning_content: "Search for the first clue.",
        reasoning_duration_ms: 1_000,
      },
      tool_calls: [
        {
          id: "search-1",
          name: "web_search",
          args: { query: "first clue" },
        },
      ],
    },
    {
      id: "search-result",
      type: "tool",
      name: "web_search",
      tool_call_id: "search-1",
      content: "[]",
    },
    {
      id: "ai-check-one",
      type: "ai",
      content: "",
      additional_kwargs: {
        reasoning_content: "Inspect the first result.",
        reasoning_duration_ms: 2_000,
      },
    },
    {
      id: "ai-check-two",
      type: "ai",
      content: "",
      additional_kwargs: {
        reasoning_content: "Check a second angle.",
        reasoning_duration_ms: 3_000,
      },
    },
  ] as Message[];

  const html = renderToStaticMarkup(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined }}
    >
      <StandaloneArtifactsProvider enabled={false}>
        <MessageGroup messages={messages} showAllSteps={true} />
      </StandaloneArtifactsProvider>
    </I18nContext.Provider>,
  );

  expect(html).toContain("Search for the first clue.");
  expect(html).toContain("Inspect the first result.");
  expect(html).toContain("Check a second angle.");
  const reasoningDisclosures =
    html.match(/data-testid="thinking-disclosure"/g) ?? [];
  expect(reasoningDisclosures).toHaveLength(3);
  expect(html).toContain("Thought (1 second)");
  expect(html).toContain("Thought (2 seconds)");
  expect(html).toContain("Thought (3 seconds)");
  expect(html.indexOf("Thought (1 second)")).toBeLessThan(
    html.indexOf("Search on the web for"),
  );
  expect(html.indexOf("Search on the web for")).toBeLessThan(
    html.indexOf("Thought (2 seconds)"),
  );
  expect(html.indexOf("Thought (2 seconds)")).toBeLessThan(
    html.indexOf("Thought (3 seconds)"),
  );
  expect(html).not.toContain("more steps");
});

test("keeps the latest live reasoning open without discarding the previous pass", () => {
  const messages = [
    {
      id: "ai-previous-reasoning",
      type: "ai",
      content: "",
      additional_kwargs: { reasoning_content: "Review the first result." },
    },
    {
      id: "ai-current-reasoning",
      type: "ai",
      content: "",
      additional_kwargs: { reasoning_content: "Check the current result." },
    },
  ] as Message[];

  const html = renderToStaticMarkup(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined }}
    >
      <StandaloneArtifactsProvider enabled={false}>
        <MessageGroup messages={messages} isLoading={true} />
      </StandaloneArtifactsProvider>
    </I18nContext.Provider>,
  );
  const disclosureTags =
    html.match(/<div[^>]*data-testid="thinking-disclosure"[^>]*>/g) ?? [];
  const disclosureStates = disclosureTags.map(
    (tag) => /data-state="(closed|open)"/.exec(tag)?.[1],
  );
  const renderedText = html.replace(/<[^>]*>/g, "");

  expect(disclosureTags).toHaveLength(2);
  expect(renderedText).toContain("Review the first result.");
  expect(html).toContain("Thinking… (0s)");
  expect(disclosureStates).toEqual(["open", "open"]);
});

test("keeps subtask cards interleaved with regular tools in tool-call order", () => {
  const messages = [
    {
      id: "ai-mixed-tools",
      type: "ai",
      content: "",
      additional_kwargs: {
        reasoning_content: "Delegate and verify in the requested order.",
        reasoning_duration_ms: 4_000,
      },
      tool_calls: [
        {
          id: "task-1",
          name: "task",
          args: { description: "First delegated task" },
        },
        {
          id: "search-1",
          name: "web_search",
          args: { query: "verification" },
        },
        {
          id: "task-2",
          name: "task",
          args: { description: "Second delegated task" },
        },
      ],
    },
  ] as Message[];

  const html = renderToStaticMarkup(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined }}
    >
      <StandaloneArtifactsProvider enabled={false}>
        <MessageGroup
          messages={messages}
          renderTaskToolCall={(taskId) => <div>{`Subtask ${taskId}`}</div>}
          showAllSteps={true}
        />
      </StandaloneArtifactsProvider>
    </I18nContext.Provider>,
  );

  expect(html.indexOf("Subtask task-1")).toBeLessThan(
    html.indexOf("Search on the web for"),
  );
  expect(html.indexOf("Search on the web for")).toBeLessThan(
    html.indexOf("Subtask task-2"),
  );
});

test("keeps clarification reasoning while omitting the duplicate tool step", () => {
  const messages = [
    {
      id: "ai-clarification",
      type: "ai",
      content: "",
      additional_kwargs: {
        reasoning_content: "I need the deployment preference first.",
        reasoning_duration_ms: 5_000,
      },
      tool_calls: [
        {
          id: "call-clarification",
          name: "ask_clarification",
          args: { question: "Which environment?" },
        },
      ],
    },
    {
      id: "tool-clarification",
      type: "tool",
      name: "ask_clarification",
      tool_call_id: "call-clarification",
      content: "Which environment?",
    },
  ] as Message[];

  const html = renderToStaticMarkup(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined }}
    >
      <StandaloneArtifactsProvider enabled={false}>
        <MessageGroup messages={messages} showAllSteps={true} />
      </StandaloneArtifactsProvider>
    </I18nContext.Provider>,
  );

  expect(html.match(/data-testid="thinking-disclosure"/g)).toHaveLength(1);
  expect(html).toContain("Thought (5 seconds)");
  expect(html).toContain("I need the deployment preference first.");
  expect(html).not.toContain("Which environment?");
});

test("keeps the pure-reasoning token attribution on its own disclosure", () => {
  const messages = [
    {
      id: "ai-token-reasoning",
      type: "ai",
      content: "",
      additional_kwargs: { reasoning_content: "Inspect token attribution." },
    },
  ] as Message[];

  const html = renderToStaticMarkup(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined }}
    >
      <StandaloneArtifactsProvider enabled={false}>
        <MessageGroup
          messages={messages}
          showAllSteps={true}
          showTokenDebugSummaries={true}
          tokenDebugSteps={[
            {
              id: "ai-token-reasoning",
              messageId: "ai-token-reasoning",
              label: "Thinking",
              secondaryLabels: [],
              sharedAttribution: false,
              usage: { inputTokens: 8, outputTokens: 5, totalTokens: 13 },
            },
          ]}
        />
      </StandaloneArtifactsProvider>
    </I18nContext.Provider>,
  );

  expect(html.match(/data-testid="thinking-disclosure"/g)).toHaveLength(1);
  expect(html).toContain("13 Tokens");
  expect(html).toContain("Inspect token attribution.");
});
