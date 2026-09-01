import {
  projectClientScopeSchema,
  type ProjectClientScope,
} from "@/core/private-work/types";

export function knowledgeRoot(scope: ProjectClientScope) {
  const parsed = projectClientScopeSchema.parse(scope);
  return [
    "account",
    parsed.accountId,
    "project",
    parsed.projectId,
    "knowledge",
  ] as const;
}

export function knowledgeQueryKey(
  scope: ProjectClientScope,
  ...segments: readonly unknown[]
) {
  return [...knowledgeRoot(scope), ...segments] as const;
}

export function knowledgeFileCapabilitiesQueryKey(scope: ProjectClientScope) {
  return knowledgeQueryKey(scope, "file-capabilities");
}
