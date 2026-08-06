import { expect, test, rs } from "@rstest/core";
import type { QueryClient } from "@tanstack/react-query";

import { invalidateStoppedThreadCaches } from "@/core/threads/hooks";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";

test("terminal and stopped runs invalidate the scoped context-window reading", () => {
  const invalidateQueries = rs.fn(() => Promise.resolve());
  const queryClient = { invalidateQueries } as unknown as QueryClient;

  invalidateStoppedThreadCaches(queryClient, THREAD_ID, false, {
    accountId: ACCOUNT_ID,
    projectId: PROJECT_ID,
  });

  expect(invalidateQueries).toHaveBeenCalledWith({
    queryKey: [
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      "thread-context-usage",
      THREAD_ID,
    ],
  });
});
