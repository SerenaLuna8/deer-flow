import {
  projectClientScopeSchema,
  type ProjectClientScope,
} from "@/core/private-work/types";

export function governanceRoot(scope: ProjectClientScope) {
  const parsed = projectClientScopeSchema.parse(scope);
  return [
    "account",
    parsed.accountId,
    "project",
    parsed.projectId,
    "governance",
  ] as const;
}
