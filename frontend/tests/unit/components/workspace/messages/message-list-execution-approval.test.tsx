import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { ExecutionApprovalCard } from "@/components/workspace/messages/execution-approval-card";
import { MessageList } from "@/components/workspace/messages/message-list";
import type { ExecutionApprovalProjection } from "@/core/execution-approvals/schemas";
import { I18nProvider } from "@/core/i18n/context";
import type { AgentThreadState } from "@/core/threads";

const approval: ExecutionApprovalProjection = {
  approval_id: "11111111-1111-4111-8111-111111111111",
  source_run_id: "run-1",
  source_tool_call_id: "call-1",
  version: "1",
  execution_domain: {
    label: "Local Provider host",
    effective_user_label: "local OS user",
  },
  command_preview: "python count.py",
  cwd_preview: "/mnt/user-data/workspace",
  timeout_seconds: 60,
  source_agent: {
    kind: "lead",
    label: "Project Assistant",
    path: ["lead"],
  },
  risk_level: "host_execution",
  warning_code: "LOCAL_PROCESS_RUNS_ON_HOST",
  continuation_run: null,
  status: "pending",
  can_decide: true,
  decision_expires_at: "2026-08-14T16:20:00Z",
  remaining_ttl_seconds: 300,
};

function threadState({
  isLoading = false,
  messages = [],
}: {
  isLoading?: boolean;
  messages?: Message[];
} = {}) {
  return {
    error: undefined,
    getMessagesMetadata: () => undefined,
    isLoading,
    isThreadLoading: false,
    messages,
  } as unknown as BaseStream<AgentThreadState>;
}

function renderMessageList(
  thread: BaseStream<AgentThreadState>,
  options: { suspendLoadingIndicators?: boolean } = {},
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
      <StandaloneArtifactsProvider enabled={false}>
        <MessageList
          thread={thread}
          threadId="thread-1"
          suspendLoadingIndicators={options.suspendLoadingIndicators}
          trailingContent={
            <ExecutionApprovalCard
              approval={approval}
              onDecision={() => undefined}
            />
          }
        />
      </StandaloneArtifactsProvider>
    </I18nProvider>,
  );
}

describe("MessageList execution approval integration", () => {
  test("renders the pending approval as trailing timeline content", () => {
    const html = renderMessageList(threadState());

    expect(html).toContain('data-testid="message-list-trailing-content"');
    expect(html).toContain('data-execution-approval-state="pending"');
    expect(html).toContain("Allow once");
  });

  test("suppresses the generic RunActivity while a pending decision card is visible", () => {
    const activeThread = threadState({ isLoading: true });

    expect(renderMessageList(activeThread)).toContain(
      'data-testid="run-activity"',
    );
    expect(
      renderMessageList(activeThread, { suspendLoadingIndicators: true }),
    ).not.toContain('data-testid="run-activity"');
  });

  test("settles the last reasoning message visually while approval is pending", () => {
    const activeThread = threadState({
      isLoading: true,
      messages: [
        {
          id: "ai-1",
          type: "ai",
          content: "",
          additional_kwargs: { reasoning_content: "Preparing the command" },
        } as Message,
      ],
    });

    expect(renderMessageList(activeThread)).toContain("Thinking…");
    const suspended = renderMessageList(activeThread, {
      suspendLoadingIndicators: true,
    });
    expect(suspended).not.toContain("Thinking…");
    expect(suspended).toContain("Reasoning");
  });
});
