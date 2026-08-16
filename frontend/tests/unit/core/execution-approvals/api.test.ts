import { afterEach, describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import {
  fetchActiveExecutionApproval,
  fetchExecutionApproval,
} from "@/core/execution-approvals/api";
import {
  commitExecutionApprovalDecisionResponse,
  submitExecutionApprovalDecision,
} from "@/core/execution-approvals/decisions";
import {
  executionApprovalActiveQueryKey,
  executionApprovalQueryKey,
} from "@/core/execution-approvals/query-keys";
import {
  executionApprovalDecisionSurface,
  executionApprovalsActiveResponseSchema,
  type ExecutionApprovalsActiveResponse,
} from "@/core/execution-approvals/schemas";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "thread/with space";
const APPROVAL_ID = "33333333-3333-4333-8333-333333333333";
const access = {
  apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
  scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
};

const approvalCommon = {
  approval_id: APPROVAL_ID,
  source_run_id: "run-1",
  source_tool_call_id: "call-1",
  version: "1",
  execution_domain: {
    label: "Jiangfeng Mac",
    effective_user_label: "jiangfeng",
  },
  command_preview: "python count.py",
  cwd_preview: "/mnt/user-data/workspace",
  timeout_seconds: 60,
  source_agent: {
    kind: "lead",
    label: "Project Assistant",
    path: ["Project Assistant"],
  },
  risk_level: "host_execution",
  warning_code: "LOCAL_PROCESS_RUNS_ON_HOST",
} as const;

const pendingApproval = {
  ...approvalCommon,
  status: "pending",
  can_decide: true,
  continuation_run: null,
  decision_expires_at: "2026-08-14T16:20:00Z",
  remaining_ttl_seconds: 300,
} as const;

function requestURL(input: RequestInfo | URL) {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function jsonBody(init: RequestInit | undefined) {
  if (typeof init?.body !== "string") throw new Error("Expected JSON body");
  return JSON.parse(init.body) as unknown;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("execution approval API", () => {
  test("scopes active and by-id query keys to account, project and thread", () => {
    expect(executionApprovalActiveQueryKey(access.scope, THREAD_ID)).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      "execution-approvals",
      THREAD_ID,
      "active",
    ]);
    expect(
      executionApprovalQueryKey(access.scope, THREAD_ID, APPROVAL_ID),
    ).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      "execution-approvals",
      THREAD_ID,
      APPROVAL_ID,
    ]);
  });

  test("reads singular active and by-id projections with AbortSignal", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({
          schema_version: 1,
          server_time: "2026-08-14T16:15:00Z",
          approval: pendingApproval,
        }),
    );
    rs.stubGlobal("fetch", fetcher);

    await fetchActiveExecutionApproval(access, THREAD_ID, controller.signal);
    await fetchExecutionApproval(
      access,
      THREAD_ID,
      APPROVAL_ID,
      controller.signal,
    );

    expect(requestURL(fetcher.mock.calls[0]![0])).toBe(
      `/api/projects/${PROJECT_ID}/private-work/threads/thread%2Fwith%20space/execution-approvals/active`,
    );
    expect(fetcher.mock.calls[0]![1]?.signal).toBe(controller.signal);
    expect(requestURL(fetcher.mock.calls[1]![0])).toBe(
      `/api/projects/${PROJECT_ID}/private-work/threads/thread%2Fwith%20space/execution-approvals/${APPROVAL_ID}`,
    );
  });

  test("submits a strict one-time decision through the CSRF-aware fetcher", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({
          schema_version: 1,
          server_time: "2026-08-14T16:15:01Z",
          approval: {
            ...approvalCommon,
            status: "approved",
            can_decide: false,
            decision_at: "2026-08-14T16:15:01Z",
            claim_expires_at: "2026-08-14T16:16:01Z",
            continuation_run: { run_id: "run-2", status: "pending" },
          },
        }),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=approval-token" });
    rs.stubGlobal("fetch", fetcher);

    await submitExecutionApprovalDecision(
      access,
      THREAD_ID,
      "run-1",
      APPROVAL_ID,
      {
        schema_version: 1,
        decision: "allow_once",
        expected_version: "1",
        idempotency_key: "44444444-4444-4444-8444-444444444444",
      },
    );

    const [input, init] = fetcher.mock.calls[0]!;
    expect(requestURL(input)).toBe(
      `/api/projects/${PROJECT_ID}/private-work/threads/thread%2Fwith%20space/runs/run-1/execution-approvals/${APPROVAL_ID}/decision`,
    );
    expect(init?.method).toBe("POST");
    expect(jsonBody(init)).toEqual({
      schema_version: 1,
      decision: "allow_once",
      expected_version: "1",
      idempotency_key: "44444444-4444-4444-8444-444444444444",
    });
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe(
      "approval-token",
    );
  });

  test.each(["approved", "denied"] as const)(
    "commits %s so the decision surface closes synchronously",
    (status) => {
      const queryClient = new QueryClient();
      const queryKey = executionApprovalQueryKey(
        access.scope,
        THREAD_ID,
        APPROVAL_ID,
      );
      queryClient.setQueryData<ExecutionApprovalsActiveResponse>(
        queryKey,
        executionApprovalsActiveResponseSchema.parse({
          schema_version: 1,
          server_time: "2026-08-14T16:15:00Z",
          approval: pendingApproval,
        }),
      );
      expect(
        executionApprovalDecisionSurface(
          queryClient.getQueryData<ExecutionApprovalsActiveResponse>(queryKey)
            ?.approval,
        )?.status,
      ).toBe("pending");

      const response = executionApprovalsActiveResponseSchema.parse(
        status === "approved"
          ? {
              schema_version: 1,
              server_time: "2026-08-14T16:15:01Z",
              approval: {
                ...approvalCommon,
                status: "approved",
                version: "2",
                can_decide: false,
                decision_at: "2026-08-14T16:15:01Z",
                claim_expires_at: "2026-08-14T16:16:01Z",
                continuation_run: { run_id: "run-2", status: "pending" },
              },
            }
          : {
              schema_version: 1,
              server_time: "2026-08-14T16:15:01Z",
              approval: {
                ...approvalCommon,
                status: "denied",
                version: "2",
                can_decide: false,
                decision_at: "2026-08-14T16:15:01Z",
                denial_delivery_status: "delivered",
                continuation_run: null,
              },
            },
      );

      commitExecutionApprovalDecisionResponse(
        queryClient,
        access.scope,
        THREAD_ID,
        APPROVAL_ID,
        response,
      );

      const committed =
        queryClient.getQueryData<ExecutionApprovalsActiveResponse>(queryKey);
      expect(committed?.approval?.status).toBe(status);
      expect(executionApprovalDecisionSurface(committed?.approval)).toBeNull();
      expect(
        queryClient.getQueryData<ExecutionApprovalsActiveResponse>(
          executionApprovalActiveQueryKey(access.scope, THREAD_ID),
        )?.approval?.status ?? null,
      ).toBe(status === "approved" ? "approved" : null);
    },
  );
});
