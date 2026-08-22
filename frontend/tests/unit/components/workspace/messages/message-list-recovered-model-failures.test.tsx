import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageList } from "@/components/workspace/messages/message-list";
import { I18nProvider } from "@/core/i18n/context";
import { RECOVERED_LLM_FAILURES_KEY } from "@/core/messages/recovered-llm-failures";
import { ProjectPrivateWorkProvider } from "@/core/private-work/provider";
import type { AgentThreadState } from "@/core/threads";

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
    [RECOVERED_LLM_FAILURES_KEY]: {
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
  test("renders every safe recovered failure reason in Chinese", () => {
    const html = renderMessageList(message, "zh-CN");

    expect(html).toContain("最终回答仍然完成。");
    expect(html).toContain('data-testid="recovered-model-failure-notice"');
    expect(html).toContain("本轮有 2 次模型请求失败");
    expect(html).toContain("本轮最终状态以运行提示为准");
    expect(html).toContain("失败 1：第 1/3 次模型请求");
    expect(html).toContain("模型服务或网络连接暂时不可用");
    expect(html).toContain("LLM_PROVIDER_UNAVAILABLE");
    expect(html).toContain("失败 2：第 2/3 次模型请求");
    expect(html).toContain("模型服务繁忙");
    expect(html).toContain("LLM_PROVIDER_BUSY");
  });

  test("renders the safe English diagnostic without raw exception text", () => {
    const html = renderMessageList(message, "en-US");

    expect(html).toContain("2 model requests failed during this Run");
    expect(html).toContain("refer to the Run status for the final outcome");
    expect(html).toContain("model attempt 1/3");
    expect(html).toContain(
      "the model provider or network connection was temporarily unavailable",
    );
    expect(html).not.toContain("api_key");
    expect(html).not.toContain("ConnectionError(");
  });
});
