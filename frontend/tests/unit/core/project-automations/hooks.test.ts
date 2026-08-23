import { describe, expect, rs, test } from "@rstest/core";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

import { privateWorkQueryKey } from "@/core/private-work/query-keys";
import type { PrivateWorkAccess } from "@/core/private-work/types";
import {
  automationTriggerMutationOptions,
  createAutomationTriggerIdempotencyRegistry,
} from "@/core/project-automations/hooks";
import { threadContextUsageQueryKey } from "@/core/threads/context-usage";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";

const access = {
  scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
  isActive: () => true,
} as unknown as PrivateWorkAccess;

describe("Automation trigger cache coordination", () => {
  test("refreshes only the admitted reuse Thread Gauge when the trigger returns an active Run", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const queryKey = privateWorkQueryKey(
      access.scope,
      ...threadContextUsageQueryKey(THREAD_ID),
    );
    const otherThreadKey = privateWorkQueryKey(
      access.scope,
      ...threadContextUsageQueryKey("thread-other"),
    );
    queryClient.setQueryData(queryKey, { estimated_tokens: 10 });
    queryClient.setQueryData(otherThreadKey, { estimated_tokens: 30 });
    const refetch = rs.fn(async () => ({ estimated_tokens: 20 }));
    const observer = new QueryObserver(queryClient, {
      queryKey,
      queryFn: refetch,
      staleTime: Number.POSITIVE_INFINITY,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    const options = automationTriggerMutationOptions(
      queryClient,
      access,
      createAutomationTriggerIdempotencyRegistry(),
    );

    await Reflect.apply(options.onSuccess, undefined, [
      {
        id: "automation-run-1",
        automation_id: "automation-1",
        automation_version: 1,
        scheduled_for: "2026-08-23T00:00:00Z",
        trigger: "manual",
        status: "running",
        thread_id: THREAD_ID,
        run_id: "run-1",
        error_code: null,
        started_at: "2026-08-23T00:00:01Z",
        finished_at: null,
        created_at: "2026-08-23T00:00:00Z",
        updated_at: "2026-08-23T00:00:01Z",
      },
    ]);

    expect(refetch).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryState(otherThreadKey)?.isInvalidated).toBe(
      false,
    );

    unsubscribe();
    queryClient.clear();
  });
});
