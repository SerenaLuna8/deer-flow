import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import type { ExecutionApprovalProjection } from "@/core/execution-approvals/schemas";
import { deriveExecutionApprovalSubtaskUpdate } from "@/core/execution-approvals/subtask-state";

const APPROVAL_ID = "11111111-1111-4111-8111-111111111111";
const PARENT_TASK_CALL_ID = "parent-task-call";

const commonApproval = {
  approval_id: APPROVAL_ID,
  source_run_id: "source-run",
  source_tool_call_id: "nested-bash-call",
  version: "1",
  execution_domain: {
    label: "Local Worker",
    effective_user_label: "local-user",
  },
  command_preview: "printf approval-test",
  cwd_preview: "/workspace",
  timeout_seconds: 60,
  source_agent: {
    kind: "subagent" as const,
    label: "general-purpose",
    path: ["lead", "general-purpose"],
  },
  risk_level: "host_execution" as const,
  warning_code: "LOCAL_PROCESS_RUNS_ON_HOST" as const,
  continuation_run: null,
};

function taskApprovalMessage(approvalId = APPROVAL_ID) {
  return {
    type: "tool",
    name: "task",
    tool_call_id: PARENT_TASK_CALL_ID,
    content: "Delegated host command execution requires approval.",
    artifact: {
      host_execution_approval: {
        schema_version: 1,
        kind: "local_shell",
        approval_id: approvalId,
        source_run_id: "source-run",
        source_tool_call_id: "nested-bash-call",
      },
    },
  } as unknown as Message;
}

function pendingApproval(): ExecutionApprovalProjection {
  return {
    ...commonApproval,
    status: "pending",
    can_decide: true,
    decision_expires_at: "2026-08-16T00:10:00Z",
    remaining_ttl_seconds: 300,
  };
}

describe("delegated execution approval subtask state", () => {
  test("binds the approval to the parent task ToolMessage rather than the nested Bash call", () => {
    expect(
      deriveExecutionApprovalSubtaskUpdate(taskApprovalMessage(), {
        approval: pendingApproval(),
        observedApprovalId: APPROVAL_ID,
      }),
    ).toEqual({
      id: PARENT_TASK_CALL_ID,
      status: "in_progress",
      statusSource: "execution_approval",
      executionApproval: {
        approvalId: APPROVAL_ID,
        status: "pending",
      },
    });
  });

  test("settles denied, expired, and cancelled approvals instead of leaving a spinner", () => {
    for (const status of ["denied", "expired", "cancelled"] as const) {
      const approval = {
        ...commonApproval,
        status,
        can_decide: false,
        ...(status === "denied"
          ? {
              decision_at: "2026-08-16T00:05:00Z",
              denial_delivery_status: "delivered" as const,
            }
          : {
              finished_at: "2026-08-16T00:05:00Z",
              reason_code: status.toUpperCase(),
            }),
      } as ExecutionApprovalProjection;

      expect(
        deriveExecutionApprovalSubtaskUpdate(taskApprovalMessage(), {
          approval,
          observedApprovalId: APPROVAL_ID,
        }),
      ).toMatchObject({
        id: PARENT_TASK_CALL_ID,
        status: "failed",
        statusSource: "execution_approval",
        executionApproval: { approvalId: APPROVAL_ID, status },
      });
    }
  });

  test("settles a finished command from its exit code", () => {
    const finished = {
      ...commonApproval,
      status: "finished" as const,
      can_decide: false as const,
      finished_at: "2026-08-16T00:05:00Z",
      result_summary_code: "PROCESS_EXITED",
    };

    expect(
      deriveExecutionApprovalSubtaskUpdate(taskApprovalMessage(), {
        approval: { ...finished, exit_code: 0 },
        observedApprovalId: APPROVAL_ID,
      }),
    ).toMatchObject({ status: "completed" });
    expect(
      deriveExecutionApprovalSubtaskUpdate(taskApprovalMessage(), {
        approval: { ...finished, exit_code: 7 },
        observedApprovalId: APPROVAL_ID,
      }),
    ).toMatchObject({ status: "failed" });
  });

  test("treats an unmatched historical approval anchor as a completed source task", () => {
    expect(
      deriveExecutionApprovalSubtaskUpdate(taskApprovalMessage(), {
        approval: null,
        observedApprovalId: "22222222-2222-4222-8222-222222222222",
      }),
    ).toEqual({
      id: PARENT_TASK_CALL_ID,
      status: "completed",
      statusSource: "execution_approval",
    });
  });

  test("keeps the newest anchor paused while its projection is becoming visible", () => {
    expect(
      deriveExecutionApprovalSubtaskUpdate(taskApprovalMessage(), {
        approval: null,
        observedApprovalId: APPROVAL_ID,
      }),
    ).toMatchObject({
      status: "in_progress",
      executionApproval: { status: "pending" },
    });
  });
});
