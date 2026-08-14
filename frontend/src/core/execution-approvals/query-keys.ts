import { privateWorkQueryKey } from "@/core/private-work/query-keys";
import type { ProjectClientScope } from "@/core/private-work/types";

import {
  executionApprovalIdSchema,
  executionApprovalThreadIdSchema,
} from "./validation";

export function executionApprovalRootQueryKey(
  scope: ProjectClientScope,
  threadId: string,
) {
  return privateWorkQueryKey(
    scope,
    "execution-approvals",
    executionApprovalThreadIdSchema.parse(threadId),
  );
}

export function executionApprovalActiveQueryKey(
  scope: ProjectClientScope,
  threadId: string,
) {
  return [...executionApprovalRootQueryKey(scope, threadId), "active"] as const;
}

export function executionApprovalQueryKey(
  scope: ProjectClientScope,
  threadId: string,
  approvalId: string,
) {
  return [
    ...executionApprovalRootQueryKey(scope, threadId),
    executionApprovalIdSchema.parse(approvalId),
  ] as const;
}
