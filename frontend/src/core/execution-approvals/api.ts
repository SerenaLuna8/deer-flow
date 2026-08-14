import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  projectClientScopeSchema,
  type PrivateWorkAccess,
} from "@/core/private-work/types";

import { executionApprovalsActiveResponseSchema } from "./schemas";
import {
  executionApprovalIdSchema,
  executionApprovalThreadIdSchema,
} from "./validation";

export type ExecutionApprovalAccess = Pick<
  PrivateWorkAccess,
  "apiBaseURL" | "scope"
>;

function executionApprovalThreadBaseURL(
  access: ExecutionApprovalAccess,
  threadId: string,
) {
  const scope = projectClientScopeSchema.parse(access.scope);
  const privateSuffix = `/projects/${scope.projectId}/private-work`;
  const baseURL = access.apiBaseURL.replace(/\/$/u, "");
  if (!baseURL.endsWith(privateSuffix)) {
    throw new Error(
      "Execution approvals require a project-scoped private-work URL",
    );
  }
  const selectedThreadId = executionApprovalThreadIdSchema.parse(threadId);
  return `${baseURL}/threads/${encodeURIComponent(selectedThreadId)}/execution-approvals`;
}

async function readProjectionResponse(response: Response, fallback: string) {
  if (!response.ok) await throwGatewayApiError(response, fallback);
  return executionApprovalsActiveResponseSchema.parse(await response.json());
}

export async function fetchActiveExecutionApproval(
  access: ExecutionApprovalAccess,
  threadId: string,
  signal?: AbortSignal,
) {
  const response = await fetchWithAuth(
    `${executionApprovalThreadBaseURL(access, threadId)}/active`,
    { signal },
  );
  return readProjectionResponse(
    response,
    "Failed to load the active execution approval.",
  );
}

export async function fetchExecutionApproval(
  access: ExecutionApprovalAccess,
  threadId: string,
  approvalId: string,
  signal?: AbortSignal,
) {
  const selectedApprovalId = executionApprovalIdSchema.parse(approvalId);
  const response = await fetchWithAuth(
    `${executionApprovalThreadBaseURL(access, threadId)}/${encodeURIComponent(selectedApprovalId)}`,
    { signal },
  );
  return readProjectionResponse(response, "Failed to load execution approval.");
}

export { executionApprovalThreadBaseURL };
