import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageList } from "@/components/workspace/messages/message-list";
import { I18nProvider } from "@/core/i18n/context";
import { ProjectPrivateWorkProvider } from "@/core/private-work/provider";
import type { AgentThreadState } from "@/core/threads";

rs.mock("@/core/workspace-changes/hooks", () => ({
  useWorkspaceChanges: () => ({
    isLoading: false,
    data: {
      available: true,
      version: 2,
      summary: {
        created: 1,
        modified: 0,
        deleted: 0,
        additions: 1,
        deletions: 0,
        truncated: false,
      },
      files: [
        {
          path: "/mnt/user-data/outputs/hidden-change.md",
          root: "outputs",
          status: "created",
          binary: false,
          sensitive: false,
          size_before: null,
          size_after: 12,
          sha256_before: null,
          sha256_after: "a".repeat(64),
          diff: "+hidden\n",
          diff_truncated: false,
          diff_unavailable_reason: null,
          additions: 1,
          deletions: 0,
        },
      ],
      limits: {},
    },
  }),
}));

function renderMessageList(message: Message) {
  const thread = {
    error: undefined,
    getMessagesMetadata: () => undefined,
    isLoading: false,
    isThreadLoading: false,
    messages: [message],
  } as unknown as BaseStream<AgentThreadState>;

  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
      <QueryClientProvider client={new QueryClient()}>
        <ProjectPrivateWorkProvider
          accountId="11111111-1111-4111-8111-111111111111"
          projectId="22222222-2222-4222-8222-222222222222"
        >
          <StandaloneArtifactsProvider enabled={false}>
            <MessageList thread={thread} threadId="thread-workspace-changes" />
          </StandaloneArtifactsProvider>
        </ProjectPrivateWorkProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

describe("MessageList workspace changes", () => {
  test("does not show the file changes area in ordinary conversation history", () => {
    const html = renderMessageList({
      id: "answer-with-workspace-changes",
      type: "ai",
      content: "The requested report is ready.",
      additional_kwargs: {},
      run_id: "run-1",
    } as unknown as Message);

    expect(html).toContain("The requested report is ready.");
    expect(html).not.toContain("Edited 1 file");
    expect(html).not.toContain("View changes");
    expect(html).not.toContain("hidden-change.md");
  });
});
