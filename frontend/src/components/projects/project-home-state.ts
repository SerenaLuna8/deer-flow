import type { Project } from "@/core/projects/types";

export interface ProjectHomeResult {
  identity: string;
  project: Project;
}

export function projectHomeIdentityKey(
  userId: string | null | undefined,
  slug: string,
  projectId: string | null | undefined,
): string | null {
  if (!userId || !slug || !projectId) return null;
  return JSON.stringify([userId, slug, projectId]);
}

export function projectResultForIdentity(
  identity: string | null,
  result: ProjectHomeResult | null,
): Project | null {
  return identity !== null && result?.identity === identity
    ? result.project
    : null;
}
