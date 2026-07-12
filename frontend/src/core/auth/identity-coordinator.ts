export interface AuthRefreshAttempt {
  readonly generation: number;
  readonly controller: AbortController;
  readonly signal: AbortSignal;
}

export interface AuthIdentityCoordinator {
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
  let disposed = false;

  const abortRefresh = () => {
    activeRefresh?.controller.abort();
    activeRefresh = null;
  };

  return {
    startRefresh() {
      abortRefresh();
      generation += 1;
      const controller = new AbortController();
      const attempt = { generation, controller, signal: controller.signal };
      activeRefresh = attempt;
      onLoadingChange(true);
      return attempt;
    },
    beginIdentityChange() {
      abortRefresh();
      generation += 1;
      onLoadingChange(false);
      return generation;
    },
    isCurrent(attempt) {
      return (
        !disposed &&
        activeRefresh === attempt &&
        generation === attempt.generation &&
        !attempt.signal.aborted
      );
    },
    isGenerationCurrent(candidate) {
      return !disposed && generation === candidate;
    },
    async commitAtGeneration(candidate, transition, commit) {
      if (disposed || generation !== candidate) return false;
      await transition();
      if (disposed || generation !== candidate) return false;
      commit();
      return true;
    },
    finishRefresh(attempt) {
      if (
        disposed ||
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
      if (disposed) return;
      disposed = true;
      generation += 1;
      abortRefresh();
    },
  };
}
