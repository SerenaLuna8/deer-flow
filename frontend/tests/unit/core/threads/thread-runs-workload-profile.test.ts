import { expect, test } from "@rstest/core";

import { fetchAllThreadRuns } from "@/core/threads/thread-runs";

test("retains the server-confirmed effective workload profile from Run history", async () => {
  const runId = "00000000-0000-4000-8000-000000000501";
  const threadId = "00000000-0000-4000-8000-000000000502";
  const apiClient = {
    runs: {
      list: async () => [
        {
          run_id: runId,
          thread_id: threadId,
          assistant_id: null,
          created_at: "2026-08-23T00:00:00Z",
          updated_at: "2026-08-23T00:00:01Z",
          status: "running",
          metadata: {},
          multitask_strategy: "reject",
          error: null,
          model_name: null,
          execution_profile: null,
          workload_profile: "research",
        },
      ],
    },
  };

  const runs = await fetchAllThreadRuns(apiClient, threadId);

  expect(runs).toHaveLength(1);
  expect(Reflect.get(runs[0]!, "workload_profile")).toBe("research");
});
