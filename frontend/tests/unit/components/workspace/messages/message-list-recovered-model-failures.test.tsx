import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageList } from "@/components/workspace/messages/message-list";
import { I18nProvider } from "@/core/i18n/context";
import { ProjectPrivateWorkProvider } from "@/core/private-work/provider";
import type { AgentThreadState } from "@/core/threads";

const LEGACY_RECOVERED_LLM_FAILURES_KEY = "deerflow_recovered_llm_failures";

function renderMessageList(message: Message, locale: "en-US" | "zh-CN") {
  const thread = {
    error: undefined,
    getMessagesMetadata: () => undefined,
    isLoading: false,
    isThreadLoading: false,
    messages: [message],
  } as unknown as BaseStream<AgentThreadState>;

  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <QueryClientProvider client={new QueryClient()}>
        <ProjectPrivateWorkProvider
          accountId="11111111-1111-4111-8111-111111111111"
          projectId="22222222-2222-4222-8222-222222222222"
        >
          <StandaloneArtifactsProvider enabled={false}>
            <MessageList thread={thread} threadId="thread-recovered-model" />
          </StandaloneArtifactsProvider>
        </ProjectPrivateWorkProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

const message = {
  id: "answer-recovered-model",
  type: "ai",
  content: "最终回答仍然完成。",
  additional_kwargs: {
    [LEGACY_RECOVERED_LLM_FAILURES_KEY]: {
      schema_version: 1,
      failures: [
        {
          attempt: 1,
          max_attempts: 3,
          error_code: "LLM_PROVIDER_UNAVAILABLE",
          reason: "transient",
          disposition: "recovered",
        },
        {
          attempt: 2,
          max_attempts: 3,
          error_code: "LLM_PROVIDER_BUSY",
          reason: "busy",
          disposition: "recovered",
        },
      ],
    },
  },
} as unknown as Message;

describe("MessageList recovered model failures", () => {
  test("does not project recovered retry diagnostics into conversation history", () => {
    for (const { locale, removedNotice } of [
      { locale: "zh-CN", removedNotice: "模型调用失败后已恢复" },
      { locale: "en-US", removedNotice: "Model request failures recovered" },
    ] as const) {
      const html = renderMessageList(message, locale);

      expect(html).toContain("最终回答仍然完成。");
      expect(html).not.toContain(
        'data-testid="recovered-model-failure-notice"',
      );
      expect(html).not.toContain(removedNotice);
      expect(html).not.toContain("LLM_PROVIDER_UNAVAILABLE");
    }
  });
});
