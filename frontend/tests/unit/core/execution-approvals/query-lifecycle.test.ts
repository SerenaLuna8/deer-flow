import { afterEach, describe, expect, rs, test } from "@rstest/core";

afterEach(() => {
  if (rs.isFakeTimers()) rs.useRealTimers();
  rs.unstubAllGlobals();
});

describe("execution approval by-id query lifecycle", () => {
  test("polls a successful null projection until the persisted approval becomes terminal", async () => {
    rs.stubGlobal("window", {});
    rs.useFakeTimers();
    const [{ QueryClient, QueryObserver }, approvalHooks] = await Promise.all([
      import("@tanstack/react-query"),
      import("@/core/execution-approvals/hooks"),
    ]);

    const responses = [
      {
        schema_version: 1 as const,
        server_time: "2026-08-14T16:15:00Z",
        approval: null,
      },
      {
        schema_version: 1 as const,
        server_time: "2026-08-14T16:15:01Z",
        approval: {
          approval_id: "33333333-3333-4333-8333-333333333333",
          source_run_id: "run-1",
          source_tool_call_id: "call-1",
          version: "3",
          execution_domain: {
            label: "Jiangfeng Mac",
            effective_user_label: "jiangfeng",
          },
          command_preview: "python count.py",
          cwd_preview: "/mnt/user-data/workspace",
          timeout_seconds: 60,
          source_agent: {
            kind: "lead" as const,
            label: "Project Assistant",
            path: ["Project Assistant"],
          },
          risk_level: "host_execution" as const,
          warning_code: "LOCAL_PROCESS_RUNS_ON_HOST" as const,
          continuation_run: null,
          status: "finished" as const,
          can_decide: false as const,
          finished_at: "2026-08-14T16:15:01Z",
          exit_code: 0,
          result_summary_code: "PROCESS_EXITED",
        },
      },
    ];
    let calls = 0;
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const observer = new QueryObserver(queryClient, {
      queryKey: ["execution-approval", "persisted-id"],
      queryFn: async () => responses[Math.min(calls++, responses.length - 1)]!,
      refetchInterval: approvalHooks.executionApprovalByIdRefetchInterval,
    });

    let resolveFirst!: () => void;
    const firstProjection = new Promise<void>((resolve) => {
      resolveFirst = resolve;
    });
    const unsubscribe = observer.subscribe((result) => {
      if (result.status === "success" && result.data?.approval === null) {
        resolveFirst();
      }
    });

    try {
      await firstProjection;
      expect(calls).toBe(1);
      await rs.advanceTimersByTimeAsync(
        approvalHooks.EXECUTION_APPROVAL_POLL_INTERVAL_MS,
      );
      expect(calls).toBe(2);
      expect(observer.getCurrentResult().data?.approval?.status).toBe(
        "finished",
      );

      await rs.advanceTimersByTimeAsync(
        approvalHooks.EXECUTION_APPROVAL_POLL_INTERVAL_MS,
      );
      expect(calls).toBe(2);
    } finally {
      unsubscribe();
      queryClient.clear();
    }
  });
});
