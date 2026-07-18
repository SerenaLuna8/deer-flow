import { projectClientScopeSchema, type ProjectClientScope } from "./types";

export function privateWorkRoot(scope: ProjectClientScope) {
  const parsed = projectClientScopeSchema.parse(scope);
  return [
    "account",
    parsed.accountId,
    "project",
    parsed.projectId,
    "private-work",
  ] as const;
}

export function privateWorkQueryKey(
  scope: ProjectClientScope,
  ...segments: readonly unknown[]
) {
  return [...privateWorkRoot(scope), ...segments] as const;
}
