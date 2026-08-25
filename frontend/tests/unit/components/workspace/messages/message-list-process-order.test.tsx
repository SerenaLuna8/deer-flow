import type { Message } from "@langchain/langgraph-sdk";
import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  useParams: () => ({ project_slug: "alpha" }),
}));

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageList } from "@/components/workspace/messages/message-list";
import { I18nProvider } from "@/core/i18n/context";
import { ProjectPrivateWorkProvider } from "@/core/private-work/provider";
import { SubtasksProvider } from "@/core/tasks/context";
import type { AgentThreadState } from "@/core/threads";

function renderCompletedHistory(
  messages: Message[],
  {
    artifactsEnabled = false,
    isLoading = false,
  }: { artifactsEnabled?: boolean; isLoading?: boolean } = {},
) {
  const thread = {
    error: undefined,
    getMessagesMetadata: () => undefined,
    isLoading,
    isThreadLoading: false,
    messages,
  } as unknown as BaseStream<AgentThreadState>;

  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <QueryClientProvider client={new QueryClient()}>
        <ProjectPrivateWorkProvider
          accountId="11111111-1111-4111-8111-111111111111"
          projectId="22222222-2222-4222-8222-222222222222"
        >
          <SubtasksProvider>
            <StandaloneArtifactsProvider enabled={artifactsEnabled}>
              <MessageList
                enableSidecarActions={false}
                thread={thread}
                threadId="thread-completed-process"
              />
            </StandaloneArtifactsProvider>
          </SubtasksProvider>
        </ProjectPrivateWorkProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

describe("MessageList completed process order", () => {
  test("keeps a completed historical process inline without an execution disclosure", () => {
    const html = renderCompletedHistory([
      {
        id: "human-1",
        type: "human",
        content: "USER_REQUEST",
      },
      {
        id: "process-1",
        type: "ai",
        content: "",
        additional_kwargs: { reasoning_content: "PROCESS_THOUGHT" },
        tool_calls: [
          {
            id: "process-call-1",
            name: "web_search",
            args: { query: "PROCESS_TOOL_STEP" },
          },
        ],
      },
      {
        id: "process-result-1",
        type: "tool",
        name: "web_search",
        tool_call_id: "process-call-1",
        content: "[]",
      },
      {
        id: "final-1",
        type: "ai",
        content: "FINAL_ANSWER",
      },
    ] as Message[]);

    expect(html).not.toContain('data-testid="assistant-process-disclosure"');
    expect(html).not.toContain("执行过程");
    expect(html.indexOf("PROCESS_TOOL_STEP")).toBeGreaterThan(-1);
    expect(html.indexOf("PROCESS_TOOL_STEP")).toBeLessThan(
      html.indexOf("FINAL_ANSWER"),
    );
  });

  test("mounts delivered files once after the final answer without moving them at Run end", () => {
    const messages = [
      {
        id: "present-files-round",
        type: "ai",
        content: "PRESENT_PROCESS_OUTPUT",
        additional_kwargs: { reasoning_content: "PRESENT_THOUGHT" },
        tool_calls: [
          {
            id: "present-call",
            name: "present_files",
            args: { filepaths: ["outputs/report.md"] },
          },
          {
            id: "ordinary-call",
            name: "web_search",
            args: { query: "PRESENT_ORDINARY_TOOL" },
          },
        ],
      },
      {
        id: "present-result",
        type: "tool",
        name: "present_files",
        tool_call_id: "present-call",
        content: "ok",
      },
      {
        id: "ordinary-result",
        type: "tool",
        name: "web_search",
        tool_call_id: "ordinary-call",
        content: "[]",
      },
      {
        id: "final-after-files",
        type: "ai",
        content: "FINAL_AFTER_FILES",
      },
    ] as Message[];
    const inFlightHtml = renderCompletedHistory(messages, {
      artifactsEnabled: true,
      isLoading: true,
    });
    const completedHtml = renderCompletedHistory(messages, {
      artifactsEnabled: true,
    });

    expect(inFlightHtml).not.toContain(
      'data-testid="assistant-delivered-files"',
    );
    expect(
      completedHtml.match(/data-testid="assistant-delivered-files"/g),
    ).toHaveLength(1);
    expect(completedHtml.indexOf("PRESENT_PROCESS_OUTPUT")).toBeLessThan(
      completedHtml.indexOf("PRESENT_ORDINARY_TOOL"),
    );
    expect(completedHtml.indexOf("PRESENT_ORDINARY_TOOL")).toBeLessThan(
      completedHtml.indexOf("FINAL_AFTER_FILES"),
    );
    expect(completedHtml.indexOf("FINAL_AFTER_FILES")).toBeLessThan(
      completedHtml.indexOf('data-testid="assistant-delivered-files"'),
    );
    expect(completedHtml.split("FINAL_AFTER_FILES")).toHaveLength(2);
  });

  test("does not insert a subtask-count summary before the model-call sequence", () => {
    const html = renderCompletedHistory([
      {
        id: "subtask-round",
        type: "ai",
        content: "SUBTASK_PROCESS_OUTPUT",
        additional_kwargs: { reasoning_content: "SUBTASK_THOUGHT" },
        tool_calls: [
          {
            id: "task-1",
            name: "task",
            args: { description: "SUBTASK_CARD_MARKER" },
          },
        ],
      },
      {
        id: "subtask-result",
        type: "tool",
        name: "task",
        tool_call_id: "task-1",
        content: '{"status":"completed","summary":"done"}',
      },
      {
        id: "final-after-subtask",
        type: "ai",
        content: "FINAL_AFTER_SUBTASK",
      },
    ] as Message[]);

    expect(html).not.toContain("执行 1 个子任务");
    expect(html.indexOf("SUBTASK_PROCESS_OUTPUT")).toBeLessThan(
      html.indexOf("SUBTASK_CARD_MARKER"),
    );
    expect(html.indexOf("SUBTASK_CARD_MARKER")).toBeLessThan(
      html.indexOf("FINAL_AFTER_SUBTASK"),
    );
  });
});
