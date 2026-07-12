import type { Project } from "@/core/projects/types";

export interface ProjectHomeResult {
  identity: string;
  project: Project;
}

export interface ProjectHomeAttemptToken {
  readonly identity: string;
  readonly generation: number;
}

export interface ProjectHomeAttemptCoordinator {
  activate: (identity: string | null) => void;
  start: (identity: string) => ProjectHomeAttemptToken | null;
  complete: (token: ProjectHomeAttemptToken) => boolean;
  fail: (token: ProjectHomeAttemptToken) => boolean;
  dispose: (token: ProjectHomeAttemptToken) => void;
}

export function createProjectHomeAttemptCoordinator(): ProjectHomeAttemptCoordinator {
  let generation = 0;
  let active: ProjectHomeAttemptToken | null = null;
  let activeIdentity: string | null = null;
  let completedIdentity: string | null = null;

  const take = (token: ProjectHomeAttemptToken): boolean => {
    if (active !== token) return false;
    active = null;
    return true;
  };
  const activate = (identity: string | null) => {
    if (activeIdentity === identity) return;
    activeIdentity = identity;
    completedIdentity = null;
    if (active) {
      active = null;
      generation += 1;
    }
  };

  return {
    activate,
    start(identity) {
      activate(identity);
      if (completedIdentity === identity) return null;
      const token = { identity, generation: ++generation };
      active = token;
      return token;
    },
    complete(token) {
      if (!take(token)) return false;
      completedIdentity = token.identity;
      return true;
    },
    fail(token) {
      return take(token);
    },
    dispose(token) {
      if (!take(token)) return;
      generation += 1;
    },
  };
}

export function commitProjectHomeAttempt(
  coordinator: ProjectHomeAttemptCoordinator,
  token: ProjectHomeAttemptToken,
  currentIdentity: string | null,
  attemptedIdentity: string,
  project: Project,
): ProjectHomeResult | null {
  if (
    currentIdentity !== attemptedIdentity ||
    token.identity !== attemptedIdentity ||
    !coordinator.complete(token)
  ) {
    return null;
  }
  return { identity: attemptedIdentity, project };
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
