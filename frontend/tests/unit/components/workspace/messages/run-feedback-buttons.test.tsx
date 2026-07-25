import { expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { RunFeedbackButtons } from "@/components/workspace/messages/run-feedback-buttons";
import { I18nProvider } from "@/core/i18n/context";
import { PrivateWorkProvider } from "@/core/private-work/provider";
import { privateWorkQueryKey } from "@/core/private-work/query-keys";
import type { ProjectPrivateWorkScope } from "@/core/private-work/types";

const scope = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "22222222-2222-4222-8222-222222222222",
};
const threadId = "33333333-3333-4333-8333-333333333333";
const runId = "44444444-4444-4444-8444-444444444444";

test("feedback controls render persisted selection after history refresh", () => {
  const queryClient = new QueryClient();
  queryClient.setQueryData(
    privateWorkQueryKey(scope, "feedback", threadId, runId),
    {
      feedback_id: "feedback-1",
      thread_id: threadId,
      run_id: runId,
      message_id: "message-1",
      rating: 1,
      comment: null,
      created_at: "2026-07-22T10:00:00+00:00",
    },
  );
  const access = {
    scope,
    client: {} as ProjectPrivateWorkScope["client"],
    apiBaseURL: `/api/projects/${scope.projectId}/private-work`,
    queryKeyPrefix: [],
    reconnectOnMount: false,
  } satisfies ProjectPrivateWorkScope;

  const html = renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <I18nProvider initialLocale="en-US">
        <PrivateWorkProvider access={access}>
          <RunFeedbackButtons
            threadId={threadId}
            runId={runId}
            messageId="message-1"
          />
        </PrivateWorkProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );

  expect(html).toContain('data-testid="run-feedback-actions"');
  expect(html).toMatch(
    /aria-label="Helpful response"[^>]*aria-pressed="true"/u,
  );
  expect(html).toMatch(
    /aria-label="Not helpful response"[^>]*aria-pressed="false"/u,
  );
});
