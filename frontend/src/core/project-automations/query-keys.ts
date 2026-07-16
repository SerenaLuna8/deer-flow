import {
  projectClientScopeSchema,
  type ProjectClientScope,
} from "@/core/private-work/types";

export function automationRoot(scope: ProjectClientScope) {
  const parsed = projectClientScopeSchema.parse(scope);
  return [
    "account",
    parsed.accountId,
    "project",
    parsed.projectId,
    "automations",
  ] as const;
}

export function automationQueryKey(
  scope: ProjectClientScope,
  ...segments: readonly unknown[]
) {
  return [...automationRoot(scope), ...segments] as const;
}

export function automationMutationKey(
  scope: ProjectClientScope,
  action: string,
) {
  return automationQueryKey(scope, "mutation", action);
}
