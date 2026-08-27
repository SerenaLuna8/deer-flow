import type { BaseStream } from "@langchain/langgraph-sdk/react";
import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageList } from "@/components/workspace/messages/message-list";
import { I18nProvider } from "@/core/i18n/context";
import type { AgentThreadState } from "@/core/threads";
import { runExecutionStateSchema } from "@/core/threads/run-execution-state";

const executionState = runExecutionStateSchema.parse({
  phase: "executing",
  observed_at: "2026-08-24T10:02:05Z",
  phase_started_at: "2026-08-24T10:02:00Z",
  execution_started_at: "2026-08-24T10:00:00Z",
  retry_at: null,
  run_status: "running",
});

const thread = {
  error: undefined,
  getMessagesMetadata: () => undefined,
  isLoading: true,
  isThreadLoading: false,
  messages: [],
} as unknown as BaseStream<AgentThreadState>;

function render(
  state: typeof executionState | "unavailable" | null,
  historyError?: Error,
  isRunAdmissionPending = false,
): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
      <StandaloneArtifactsProvider enabled={false}>
        <MessageList
          thread={thread}
          threadId="33333333-3333-4333-8333-333333333333"
          runExecutionState={state}
          isRunAdmissionPending={isRunAdmissionPending}
          historyError={historyError}
        />
      </StandaloneArtifactsProvider>
    </I18nProvider>,
  );
}

describe("MessageList Run execution state", () => {
  test("renders the strict server projection instead of local RunActivity", () => {
    const html = render(executionState);

    expect(html).toContain('data-testid="run-execution-activity"');
    expect(html).not.toContain('data-testid="run-activity"');
    expect(html).toContain("Executing");
    expect(html).toContain("Total execution time 2m 5s");
    expect(html).toContain("Current phase 5s");
  });

  test("renders unavailable without falling back to a page-residence timer", () => {
    const html = render("unavailable");

    expect(html).toContain("Execution status temporarily unavailable");
    expect(html).not.toContain('data-testid="run-activity"');
  });

  test("renders no active indicator when the owner supplies no active Run", () => {
    const html = render(null);

    expect(html).not.toContain('data-testid="run-execution-activity"');
    expect(html).not.toContain('data-testid="run-activity"');
  });

  test("renders local activity only while Run admission is pending", () => {
    const html = render(null, undefined, true);

    expect(html).toContain('data-testid="run-activity"');
    expect(html).not.toContain('data-testid="run-execution-activity"');
  });

  test("keeps a terminal reconciliation history failure visible", () => {
    const html = render(
      "unavailable",
      new Error("canonical history unavailable"),
    );

    expect(html).toContain("Conversation history could not be loaded safely");
    expect(html).toContain("Execution status temporarily unavailable");
  });
});
