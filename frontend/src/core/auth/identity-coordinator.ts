export interface AuthRefreshAttempt {
  readonly generation: number;
  readonly controller: AbortController;
  readonly signal: AbortSignal;
}

export interface AuthIdentityCoordinator {
  activate: () => void;
  startRefresh: () => AuthRefreshAttempt;
  beginIdentityChange: () => number;
  isCurrent: (attempt: AuthRefreshAttempt) => boolean;
  isGenerationCurrent: (generation: number) => boolean;
  commitAtGeneration: (
    generation: number,
    transition: () => Promise<unknown>,
    commit: () => void,
  ) => Promise<boolean>;
  finishRefresh: (attempt: AuthRefreshAttempt) => boolean;
  dispose: () => void;
}

export function createAuthIdentityCoordinator(
  onLoadingChange: (isLoading: boolean) => void,
): AuthIdentityCoordinator {
  let generation = 0;
  let activeRefresh: AuthRefreshAttempt | null = null;
  let active = true;

  const abortRefresh = () => {
    activeRefresh?.controller.abort();
    activeRefresh = null;
  };

  return {
    activate() {
      if (active) return;
      active = true;
      generation += 1;
      onLoadingChange(false);
    },
    startRefresh() {
      abortRefresh();
      generation += 1;
      const controller = new AbortController();
      const attempt = { generation, controller, signal: controller.signal };
      if (!active) {
        controller.abort();
        return attempt;
      }
      activeRefresh = attempt;
      onLoadingChange(true);
      return attempt;
    },
    beginIdentityChange() {
      abortRefresh();
      generation += 1;
      if (!active) return generation;
      onLoadingChange(false);
      return generation;
    },
    isCurrent(attempt) {
      return (
        active &&
        activeRefresh === attempt &&
        generation === attempt.generation &&
        !attempt.signal.aborted
      );
    },
    isGenerationCurrent(candidate) {
      return active && generation === candidate;
    },
    async commitAtGeneration(candidate, transition, commit) {
      if (!active || generation !== candidate) return false;
      await transition();
      if (!active || generation !== candidate) return false;
      commit();
      return true;
    },
    finishRefresh(attempt) {
      if (
        !active ||
        activeRefresh !== attempt ||
        generation !== attempt.generation
      ) {
        return false;
      }
      activeRefresh = null;
      onLoadingChange(false);
      return true;
    },
    dispose() {
      if (!active) return;
      active = false;
      generation += 1;
      abortRefresh();
    },
  };
}
