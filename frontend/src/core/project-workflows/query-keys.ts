import {
  projectClientScopeSchema,
  type ProjectClientScope,
} from "@/core/private-work/types";

export function projectWorkflowRoot(scope: ProjectClientScope) {
  const parsed = projectClientScopeSchema.parse(scope);
  return [
    "account",
    parsed.accountId,
    "project",
    parsed.projectId,
    "workflows",
  ] as const;
}

export function projectWorkflowQueryKey(
  scope: ProjectClientScope,
  ...segments: readonly unknown[]
) {
  return [...projectWorkflowRoot(scope), ...segments] as const;
}
