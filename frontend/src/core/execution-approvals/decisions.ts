import type { QueryClient } from "@tanstack/react-query";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import type { ProjectClientScope } from "@/core/private-work/types";

import {
  executionApprovalThreadBaseURL,
  type ExecutionApprovalAccess,
} from "./api";
import {
  executionApprovalActiveQueryKey,
  executionApprovalQueryKey,
} from "./query-keys";
import {
  executionApprovalDecisionInputSchema,
  executionApprovalIsActive,
  executionApprovalsActiveResponseSchema,
  type ExecutionApprovalDecisionInput,
} from "./schemas";
import {
  executionApprovalIdSchema,
  executionApprovalRunIdSchema,
} from "./validation";

export async function submitExecutionApprovalDecision(
  access: ExecutionApprovalAccess,
  threadId: string,
  sourceRunId: string,
  approvalId: string,
  input: ExecutionApprovalDecisionInput,
  signal?: AbortSignal,
) {
  const selectedRunId = executionApprovalRunIdSchema.parse(sourceRunId);
  const selectedApprovalId = executionApprovalIdSchema.parse(approvalId);
  const parsed = executionApprovalDecisionInputSchema.parse(input);
  const response = await fetchWithAuth(
    `${executionApprovalThreadBaseURL(access, threadId).replace(/\/execution-approvals$/u, "")}/runs/${encodeURIComponent(selectedRunId)}/execution-approvals/${encodeURIComponent(selectedApprovalId)}/decision`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed),
      signal,
    },
  );
  if (!response.ok) {
    await throwGatewayApiError(
      response,
      "Failed to submit the execution approval decision.",
    );
  }
  return executionApprovalsActiveResponseSchema.parse(await response.json());
}

export function commitExecutionApprovalDecisionResponse(
  queryClient: QueryClient,
  scope: ProjectClientScope,
  threadId: string,
  approvalId: string,
  value: unknown,
) {
  const response = executionApprovalsActiveResponseSchema.parse(value);
  if (response.approval?.approval_id !== approvalId) {
    throw new Error("Execution approval decision response scope mismatch");
  }
  queryClient.setQueryData(
    executionApprovalQueryKey(scope, threadId, approvalId),
    response,
  );
  queryClient.setQueryData(
    executionApprovalActiveQueryKey(scope, threadId),
    executionApprovalIsActive(response.approval)
      ? response
      : { ...response, approval: null },
  );
  return response;
}
