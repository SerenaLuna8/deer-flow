import { describe, expect, test } from "@rstest/core";

import {
  executionApprovalBlocksSending,
  executionApprovalContinuationRunId,
  executionApprovalNeedsAdmissionRecovery,
  executionApprovalDecisionInputSchema,
  executionApprovalProjectionSchema,
  executionApprovalsActiveResponseSchema,
  selectNewerExecutionApprovalProjection,
} from "@/core/execution-approvals/schemas";

const APPROVAL_ID = "11111111-1111-4111-8111-111111111111";

const common = {
  approval_id: APPROVAL_ID,
  source_run_id: "run-1",
  source_tool_call_id: "call-1",
  status: "pending",
  version: "1",
  execution_domain: {
    label: "Jiangfeng Mac",
    effective_user_label: "jiangfeng",
  },
  command_preview: "python /mnt/user-data/workspace/count.py",
  cwd_preview: "/mnt/user-data/workspace",
  timeout_seconds: 60,
  source_agent: {
    kind: "lead",
    label: "Project Assistant",
    path: ["Project Assistant"],
  },
  risk_level: "host_execution",
  warning_code: "LOCAL_PROCESS_RUNS_ON_HOST",
  can_decide: true,
  continuation_run: null,
} as const;

const statuses = [
  {
    ...common,
    status: "pending",
    decision_expires_at: "2026-08-14T16:20:00Z",
    remaining_ttl_seconds: 300,
  },
  {
    ...common,
    status: "approved",
    can_decide: false,
    decision_at: "2026-08-14T16:16:00Z",
    claim_expires_at: "2026-08-14T16:17:00Z",
    continuation_run: { run_id: "run-2", status: "pending" },
  },
  {
    ...common,
    status: "claimed",
    can_decide: false,
    claimed_at: "2026-08-14T16:16:05Z",
    continuation_run: { run_id: "run-2", status: "running" },
  },
  {
    ...common,
    status: "finished",
    can_decide: false,
    finished_at: "2026-08-14T16:16:10Z",
    exit_code: 0,
    result_summary_code: "PROCESS_EXITED",
  },
  {
    ...common,
    status: "launch_failed",
    can_decide: false,
    finished_at: "2026-08-14T16:16:10Z",
    reason_code: "PROCESS_NOT_CREATED",
  },
  {
    ...common,
    status: "unknown",
    can_decide: false,
    finished_at: "2026-08-14T16:16:10Z",
    warning_code: "HOST_EXECUTION_STATE_UNKNOWN",
  },
  {
    ...common,
    status: "denied",
    can_decide: false,
    decision_at: "2026-08-14T16:16:00Z",
    denial_delivery_status: "delivered",
  },
  {
    ...common,
    status: "expired",
    can_decide: false,
    finished_at: "2026-08-14T16:20:00Z",
    reason_code: "DECISION_TTL_EXPIRED",
  },
  {
    ...common,
    status: "cancelled",
    can_decide: false,
    finished_at: "2026-08-14T16:18:00Z",
    reason_code: "THREAD_CANCELLED",
  },
] as const;

const parsedStatuses = statuses.map((approval) =>
  executionApprovalProjectionSchema.parse(approval),
);

describe("execution approval schemas", () => {
  test.each(statuses)("accepts strict $status projections", (approval) => {
    expect(executionApprovalProjectionSchema.parse(approval).status).toBe(
      approval.status,
    );
  });

  test("rejects extra authority and invalid decimal versions", () => {
    expect(() =>
      executionApprovalProjectionSchema.parse({
        ...statuses[0],
        browser_grant: "allow",
      }),
    ).toThrow();
    expect(() =>
      executionApprovalProjectionSchema.parse({
        ...statuses[0],
        version: 1,
      }),
    ).toThrow();
    expect(() =>
      executionApprovalProjectionSchema.parse({
        ...statuses[0],
        source_agent: { ...statuses[0].source_agent, secret: "value" },
      }),
    ).toThrow();
  });

  test("requires status-specific fields", () => {
    const withoutDeadline = {
      ...statuses[0],
      decision_expires_at: undefined,
    };
    expect(() =>
      executionApprovalProjectionSchema.parse(withoutDeadline),
    ).toThrow();
    expect(() =>
      executionApprovalProjectionSchema.parse({
        ...common,
        status: "unknown",
        can_decide: false,
        finished_at: "2026-08-14T16:16:10Z",
      }),
    ).toThrow();
  });

  test("allows pending requests to become read-only after capability changes", () => {
    expect(
      executionApprovalProjectionSchema.parse({
        ...statuses[0],
        can_decide: false,
      }).can_decide,
    ).toBe(false);
  });

  test("blocks new sends only while an approval is active", () => {
    expect(
      parsedStatuses.map((approval) =>
        executionApprovalBlocksSending(approval),
      ),
    ).toEqual([true, true, true, false, false, false, false, false, false]);
    expect(executionApprovalBlocksSending(null)).toBe(false);
  });

  test("selects only pending or running continuation Runs for attachment", () => {
    expect(executionApprovalContinuationRunId(parsedStatuses[1])).toBe("run-2");
    expect(executionApprovalContinuationRunId(parsedStatuses[2])).toBe("run-2");
    expect(
      executionApprovalContinuationRunId(
        executionApprovalProjectionSchema.parse({
          ...statuses[2],
          continuation_run: { run_id: "run-2", status: "success" },
        }),
      ),
    ).toBeNull();
    expect(executionApprovalContinuationRunId(parsedStatuses[0])).toBeNull();
    expect(
      executionApprovalContinuationRunId(
        executionApprovalProjectionSchema.parse({
          ...statuses[3],
          continuation_run: { run_id: "run-2", status: "running" },
        }),
      ),
    ).toBe("run-2");
    expect(
      executionApprovalContinuationRunId(
        executionApprovalProjectionSchema.parse({
          ...statuses[1],
          continuation_run: null,
        }),
      ),
    ).toBeNull();
  });

  test("recovers only a durable approved decision without a continuation", () => {
    expect(
      executionApprovalNeedsAdmissionRecovery(
        executionApprovalProjectionSchema.parse({
          ...statuses[1],
          continuation_run: null,
        }),
      ),
    ).toBe(true);
    expect(executionApprovalNeedsAdmissionRecovery(parsedStatuses[0])).toBe(
      false,
    );
    expect(executionApprovalNeedsAdmissionRecovery(parsedStatuses[2])).toBe(
      false,
    );
  });

  test("keeps the newest projection while active and by-id requests overlap", () => {
    const pending = executionApprovalProjectionSchema.parse(statuses[0]);
    const approved = executionApprovalProjectionSchema.parse({
      ...statuses[1],
      version: "2",
      continuation_run: null,
    });

    expect(
      selectNewerExecutionApprovalProjection(pending, approved)?.status,
    ).toBe("approved");
    expect(
      selectNewerExecutionApprovalProjection(approved, pending)?.status,
    ).toBe("approved");
  });

  test("keeps the v1 active response singular", () => {
    expect(
      executionApprovalsActiveResponseSchema.parse({
        schema_version: 1,
        server_time: "2026-08-14T16:15:00Z",
        approval: statuses[0],
      }).approval?.status,
    ).toBe("pending");
    expect(
      executionApprovalsActiveResponseSchema.parse({
        schema_version: 1,
        server_time: "2026-08-14T16:15:00Z",
        approval: null,
      }).approval,
    ).toBeNull();
    expect(() =>
      executionApprovalsActiveResponseSchema.parse({
        schema_version: 1,
        server_time: "2026-08-14T16:15:00Z",
        approvals: [statuses[0]],
      }),
    ).toThrow();
  });

  test("accepts one-time decisions without browser-supplied authority", () => {
    expect(
      executionApprovalDecisionInputSchema.parse({
        schema_version: 1,
        decision: "allow_once",
        expected_version: "1",
        idempotency_key: "22222222-2222-4222-8222-222222222222",
      }).decision,
    ).toBe("allow_once");
    expect(
      executionApprovalDecisionInputSchema.parse({
        schema_version: 1,
        decision: "deny",
        expected_version: "1",
        idempotency_key: "22222222-2222-4222-8222-222222222222",
      }).decision,
    ).toBe("deny");
    expect(() =>
      executionApprovalDecisionInputSchema.parse({
        schema_version: 1,
        decision: "allow_once",
        expected_version: "1",
        idempotency_key: "22222222-2222-4222-8222-222222222222",
        step_up_receipt_id: "receipt-1",
      }),
    ).toThrow();
  });
});
