import type { Message } from "@langchain/langgraph-sdk";

import type { Subtask } from "../tasks/types";

import { extractExecutionApprovalArtifact } from "./artifacts";
import type { ExecutionApprovalProjection } from "./schemas";

export interface ExecutionApprovalSubtaskContext {
  approval: ExecutionApprovalProjection | null;
  observedApprovalId: string | null;
}

type ExecutionApprovalSubtaskUpdate = Partial<Subtask> & { id: string };

/** Map a delegated approval artifact to its parent `task` ToolMessage. */
export function deriveExecutionApprovalSubtaskUpdate(
  message: Message,
  context: ExecutionApprovalSubtaskContext,
): ExecutionApprovalSubtaskUpdate | null {
  if (message.type !== "tool" || message.name !== "task") return null;
  const taskId = message.tool_call_id;
  const artifact = extractExecutionApprovalArtifact(message);
  if (!taskId || !artifact) return null;

  const approval =
    context.approval?.approval_id === artifact.approval_id
      ? context.approval
      : null;
  if (!approval) {
    if (context.observedApprovalId === artifact.approval_id) {
      return approvalUpdate(taskId, artifact.approval_id, "pending");
    }
    // Reload only queries the newest anchor. Older anchors nevertheless mark
    // source tasks the backend already completed at the approval boundary.
    return {
      id: taskId,
      status: "completed",
      statusSource: "execution_approval",
    };
  }

  switch (approval.status) {
    case "pending":
    case "approved":
    case "claimed":
      return approvalUpdate(taskId, approval.approval_id, approval.status);
    case "finished":
      return approvalUpdate(
        taskId,
        approval.approval_id,
        approval.status,
        approval.exit_code === 0 ? "completed" : "failed",
        { exitCode: approval.exit_code },
      );
    case "launch_failed":
    case "expired":
    case "cancelled":
      return approvalUpdate(
        taskId,
        approval.approval_id,
        approval.status,
        "failed",
        { reasonCode: approval.reason_code },
      );
    case "unknown":
    case "denied":
      return approvalUpdate(
        taskId,
        approval.approval_id,
        approval.status,
        "failed",
      );
  }
}

function approvalUpdate(
  id: string,
  approvalId: string,
  approvalStatus: ExecutionApprovalProjection["status"],
  status: Subtask["status"] = "in_progress",
  details: Pick<
    NonNullable<Subtask["executionApproval"]>,
    "exitCode" | "reasonCode"
  > = {},
): ExecutionApprovalSubtaskUpdate {
  return {
    id,
    status,
    statusSource: "execution_approval",
    executionApproval: {
      approvalId,
      status: approvalStatus,
      ...details,
    },
  };
}
