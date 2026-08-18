import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageList } from "@/components/workspace/messages/message-list";
import { I18nProvider } from "@/core/i18n/context";
import { TOKEN_BUDGET_STATUS_KEY } from "@/core/messages/token-budget";
import { ProjectPrivateWorkProvider } from "@/core/private-work/provider";
import type { AgentThreadState } from "@/core/threads";

const internalControlText =
  "[TOKEN BUDGET EXCEEDED] The total token usage (10,197) has exceeded the safety limit (5,000). Producing final answer with results collected so far.";

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
            <MessageList thread={thread} threadId="thread-budget" />
          </StandaloneArtifactsProvider>
        </ProjectPrivateWorkProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

describe("MessageList token budget notice", () => {
  test("renders a localized Chinese notice outside the assistant body", () => {
    const html = renderMessageList(
      {
        id: "answer-budget",
        type: "ai",
        content: "BUDGET_OK",
        response_metadata: {
          [TOKEN_BUDGET_STATUS_KEY]: {
            version: 1,
            status: "exceeded",
            reason: "total",
          },
        },
      } as unknown as Message,
      "zh-CN",
    );

    expect(html).toContain("BUDGET_OK");
    expect(html).toContain('data-testid="token-budget-notice"');
    expect(html).toContain("已达到本次运行的 Token 预算");
    expect(html).toContain("本次运行已提前停止");
    expect(html).not.toContain("TOKEN BUDGET EXCEEDED");
    expect(html).not.toContain("10,197");
  });

  test("cleans a legacy leaked suffix and renders the English notice", () => {
    const html = renderMessageList(
      {
        id: "answer-budget-legacy",
        type: "ai",
        content: `BUDGET_OK\n\n${internalControlText}`,
        response_metadata: {},
      } as unknown as Message,
      "en-US",
    );

    expect(html).toContain("BUDGET_OK");
    expect(html).toContain("Run token budget reached");
    expect(html).not.toContain("TOKEN BUDGET EXCEEDED");
    expect(html).not.toContain("10,197");
  });
});
