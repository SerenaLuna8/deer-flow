import { afterEach, describe, expect, test, rs } from "@rstest/core";

import { GatewayApiError } from "@/core/api/errors";
import {
  RUN_EXECUTION_PHASES,
  RUN_EXECUTION_STATE_MAX_RETRIES,
  RUN_EXECUTION_STATE_POLL_INTERVAL_MS,
  fetchRunExecutionState,
  isTerminalRunExecutionState,
  runExecutionPhaseSemantic,
  runExecutionStateObserverQueryKey,
  runExecutionStatePollInterval,
  selectObservedRunExecutionState,
  runExecutionStateQueryEnabled,
  runExecutionStateQueryKey,
  runExecutionStateRetryDelay,
  runExecutionStateSchema,
  shouldRetryRunExecutionState,
} from "@/core/threads/run-execution-state";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const RUN_ID = "44444444-4444-4444-8444-444444444444";
const ACCESS = {
  apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
  scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
} as const;

function state(
  phase: (typeof RUN_EXECUTION_PHASES)[number],
  runStatus:
    | "pending"
    | "running"
    | "success"
    | "error"
    | "timeout"
    | "interrupted" = "pending",
) {
  return {
    phase,
    observed_at: "2026-08-24T10:00:00Z",
    phase_started_at: "2026-08-24T09:59:55Z",
    execution_started_at: null,
    retry_at: null,
    run_status: runStatus,
  };
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("Run execution state", () => {
  test("accepts exactly the eleven authority phases and six response fields", () => {
    expect(RUN_EXECUTION_PHASES).toEqual([
      "queued",
      "waiting_for_worker",
      "starting",
      "executing",
      "retry_wait",
      "waiting_for_lease_expiry",
      "waiting_for_terminalization",
      "waiting_for_recovery",
      "recovering",
      "cancelling",
      "terminal",
    ]);

    for (const phase of RUN_EXECUTION_PHASES) {
      expect(
        runExecutionStateSchema.safeParse(
          state(phase, phase === "terminal" ? "success" : "pending"),
        ).success,
      ).toBe(true);
    }

    for (const invalid of [
      { ...state("queued"), phase: "translated-label" },
      { ...state("queued"), run_status: "unknown" },
      { ...state("queued"), observed_at: "yesterday" },
      { ...state("queued"), worker_id: "must-not-leak" },
      {
        phase: "queued",
        observed_at: "2026-08-24T10:00:00Z",
        phase_started_at: null,
        execution_started_at: null,
        run_status: "pending",
      },
    ]) {
      expect(runExecutionStateSchema.safeParse(invalid).success).toBe(false);
    }
  });

  test("fails closed on phase/status mismatches and identifies only legal terminal states", () => {
    for (const runStatus of [
      "success",
      "error",
      "timeout",
      "interrupted",
    ] as const) {
      const parsed = runExecutionStateSchema.parse(
        state("terminal", runStatus),
      );
      expect(isTerminalRunExecutionState(parsed)).toBe(true);
    }

    expect(
      runExecutionStateSchema.safeParse(state("terminal", "pending")).success,
    ).toBe(false);
    expect(
      runExecutionStateSchema.safeParse(state("executing", "success")).success,
    ).toBe(false);
    expect(isTerminalRunExecutionState(state("executing", "running"))).toBe(
      false,
    );
  });

  test("maps authority phases to locale-independent UI semantics", () => {
    expect(runExecutionPhaseSemantic("queued")).toBe("waiting");
    expect(runExecutionPhaseSemantic("waiting_for_worker")).toBe("waiting");
    expect(runExecutionPhaseSemantic("retry_wait")).toBe("waiting");
    expect(runExecutionPhaseSemantic("waiting_for_lease_expiry")).toBe(
      "waiting",
    );
    expect(runExecutionPhaseSemantic("waiting_for_terminalization")).toBe(
      "waiting",
    );
    expect(runExecutionPhaseSemantic("waiting_for_recovery")).toBe("waiting");
    expect(runExecutionPhaseSemantic("starting")).toBe("starting");
    expect(runExecutionPhaseSemantic("executing")).toBe("executing");
    expect(runExecutionPhaseSemantic("recovering")).toBe("recovering");
    expect(runExecutionPhaseSemantic("cancelling")).toBe("cancelling");
    expect(runExecutionPhaseSemantic("terminal")).toBe("terminal");
  });

  test("owns an account/project/thread/run scoped query key", () => {
    expect(runExecutionStateQueryKey(ACCESS.scope, THREAD_ID, RUN_ID)).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      "thread",
      THREAD_ID,
      "run",
      RUN_ID,
      "execution-state",
    ]);
  });

  test("reads through the matching project scope and forwards AbortSignal", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(state("executing", "running")),
    );
    rs.stubGlobal("fetch", fetcher);

    await expect(
      fetchRunExecutionState(ACCESS, THREAD_ID, RUN_ID, controller.signal),
    ).resolves.toEqual(state("executing", "running"));

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0]![0]).toBe(
      `/api/projects/${PROJECT_ID}/private-work/threads/${THREAD_ID}/runs/${RUN_ID}/execution-state`,
    );
    expect(fetcher.mock.calls[0]![1]?.signal).toBe(controller.signal);
  });

  test("rejects a mismatched project URL before issuing a request", async () => {
    const fetcher = rs.fn();
    rs.stubGlobal("fetch", fetcher);

    await expect(
      fetchRunExecutionState(
        {
          ...ACCESS,
          apiBaseURL:
            "/api/projects/55555555-5555-4555-8555-555555555555/private-work",
        },
        THREAD_ID,
        RUN_ID,
        new AbortController().signal,
      ),
    ).rejects.toThrow("project-scoped private-work URL");
    expect(fetcher).not.toHaveBeenCalled();
  });

  test("polls only a visible exact active generation and isolates late A from B", () => {
    expect(runExecutionStateQueryEnabled(RUN_ID, 7, "visible")).toBe(true);
    expect(runExecutionStateQueryEnabled(RUN_ID, 7, "hidden")).toBe(false);
    expect(runExecutionStateQueryEnabled(null, 7, "visible")).toBe(false);
    expect(runExecutionStateQueryEnabled(RUN_ID, null, "visible")).toBe(false);
    expect(
      runExecutionStatePollInterval("visible", state("executing", "running")),
    ).toBe(RUN_EXECUTION_STATE_POLL_INTERVAL_MS);
    expect(
      runExecutionStatePollInterval("hidden", state("executing", "running")),
    ).toBe(false);
    expect(
      runExecutionStatePollInterval("visible", state("terminal", "success")),
    ).toBe(false);
    expect(runExecutionStatePollInterval("visible")).toBe(
      RUN_EXECUTION_STATE_POLL_INTERVAL_MS,
    );

    const previouslyExecuting = runExecutionStateSchema.parse(
      state("executing", "running"),
    );
    expect(
      selectObservedRunExecutionState(previouslyExecuting, true),
    ).toBe("unavailable");
    expect(
      selectObservedRunExecutionState(previouslyExecuting, false),
    ).toBe(previouslyExecuting);

    const runA = runExecutionStateObserverQueryKey(
      ACCESS.scope,
      THREAD_ID,
      RUN_ID,
      7,
    );
    const runB = runExecutionStateObserverQueryKey(
      ACCESS.scope,
      THREAD_ID,
      "55555555-5555-4555-8555-555555555555",
      8,
    );
    expect(runA).not.toEqual(runB);
    expect(runA.slice(0, -2)).toEqual(
      runExecutionStateQueryKey(ACCESS.scope, THREAD_ID, RUN_ID),
    );
    expect(runA.slice(-2)).toEqual(["generation", 7]);
  });

  test("uses bounded retry only for 429, 503, or network failures", () => {
    for (const error of [
      new GatewayApiError(429, null, "busy"),
      new GatewayApiError(503, "PRIVATE_WORK_UNAVAILABLE", "unavailable"),
      new TypeError("Failed to fetch"),
    ]) {
      expect(shouldRetryRunExecutionState(0, error)).toBe(true);
      expect(
        shouldRetryRunExecutionState(RUN_EXECUTION_STATE_MAX_RETRIES, error),
      ).toBe(false);
    }
    expect(
      shouldRetryRunExecutionState(
        0,
        new GatewayApiError(404, null, "missing"),
      ),
    ).toBe(false);
    expect(shouldRetryRunExecutionState(0, new Error("invalid"))).toBe(false);
    expect(runExecutionStateRetryDelay(0)).toBe(1_000);
    expect(runExecutionStateRetryDelay(9)).toBe(8_000);
  });
});
