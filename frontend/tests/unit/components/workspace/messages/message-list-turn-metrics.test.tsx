import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageList } from "@/components/workspace/messages/message-list";
import { I18nProvider } from "@/core/i18n/context";
import type { TokenUsageInlineMode } from "@/core/messages/usage-model";
import { ProjectPrivateWorkProvider } from "@/core/private-work/provider";
import type { AgentThreadState } from "@/core/threads";

function renderMessageList(
  message: Message,
  tokenUsageInlineMode: TokenUsageInlineMode = "per_turn",
) {
  const thread = {
    error: undefined,
    getMessagesMetadata: () => undefined,
    isLoading: false,
    isThreadLoading: false,
    messages: [message],
  } as unknown as BaseStream<AgentThreadState>;

  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <QueryClientProvider client={new QueryClient()}>
        <ProjectPrivateWorkProvider
          accountId="11111111-1111-4111-8111-111111111111"
          projectId="22222222-2222-4222-8222-222222222222"
        >
          <StandaloneArtifactsProvider enabled={false}>
            <MessageList
              enableSidecarActions={false}
              thread={thread}
              threadId="thread-turn-metrics"
              tokenUsageInlineMode={tokenUsageInlineMode}
            />
          </StandaloneArtifactsProvider>
        </ProjectPrivateWorkProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

function assistantMessage({
  duration,
  usage,
}: {
  duration?: number;
  usage?: { input_tokens: number; output_tokens: number; total_tokens: number };
}) {
  return {
    id: "answer-1",
    type: "ai",
    content: "回答内容",
    additional_kwargs: {
      run_id: "run-1",
      ...(duration === undefined ? {} : { turn_duration: duration }),
      ...(usage === undefined ? {} : { usage_metadata: usage }),
    },
  } as unknown as Message;
}

describe("MessageList turn metrics", () => {
  test("places duration before Tokens in one compact metrics row", () => {
    const html = renderMessageList(
      assistantMessage({
        duration: 19,
        usage: {
          input_tokens: 14_500,
          output_tokens: 1_037,
          total_tokens: 15_537,
        },
      }),
    );

    const metricsTag = /<div[^>]*data-testid="turn-metrics"[^>]*>/.exec(
      html,
    )?.[0];
    expect(metricsTag).toContain("text-[11px]");
    expect(html.match(/data-testid="turn-metrics"/g)).toHaveLength(1);
    expect(html.match(/data-testid="run-duration"/g)).toHaveLength(1);
    expect(html.indexOf("本次任务耗时 19 秒")).toBeLessThan(
      html.indexOf("Tokens"),
    );
  });

  test("keeps a compact duration row when token usage is unavailable", () => {
    const html = renderMessageList(assistantMessage({ duration: 3 }));

    expect(html).toContain('data-testid="turn-metrics"');
    expect(html).toContain("本次任务耗时 3 秒");
    expect(html).not.toContain('data-testid="message-token-usage"');
  });

  test("keeps the existing token row when duration is unavailable", () => {
    const html = renderMessageList(
      assistantMessage({
        usage: {
          input_tokens: 1_200,
          output_tokens: 500,
          total_tokens: 1_700,
        },
      }),
    );

    expect(html).toContain('data-testid="turn-metrics"');
    expect(html).toContain('data-testid="message-token-usage"');
    expect(html).not.toContain('data-testid="run-duration"');
  });

  test("does not render an empty metrics row", () => {
    const html = renderMessageList(assistantMessage({}));

    expect(html).not.toContain('data-testid="turn-metrics"');
  });

  test("uses the token-usage visibility condition for duration", () => {
    const html = renderMessageList(
      assistantMessage({
        duration: 3,
        usage: {
          input_tokens: 1_200,
          output_tokens: 500,
          total_tokens: 1_700,
        },
      }),
      "off",
    );

    expect(html).not.toContain('data-testid="turn-metrics"');
    expect(html).not.toContain('data-testid="run-duration"');
    expect(html).not.toContain('data-testid="message-token-usage"');
  });
});
