export type StreamCallbackScope = {
  namespace: readonly string[] | undefined;
};

/**
 * Root thread UI state may only consume root-graph callbacks. Delegated graph
 * frames carry a non-empty namespace and must stay isolated from the parent
 * message list and SubtaskCard lifecycle.
 */
export function isRootStreamCallback(
  options: StreamCallbackScope | undefined,
): boolean {
  return !options?.namespace || options.namespace.length === 0;
}

/**
 * Both initial submit and regenerate use this transport boundary. Subtask
 * progress arrives through root task_* custom events, so requesting raw
 * delegated graph frames would only risk child values/messages entering the
 * parent thread projection.
 */
export function buildRootThreadStreamOptions() {
  return {
    streamResumable: true,
    config: { recursion_limit: 1000 },
  } as const;
}

type DeferredStreamDetach = {
  cancelled: boolean;
};

/**
 * React Strict Effects runs setup → cleanup → setup during development.
 * Deferring the local detach lets the second setup retain the same stream,
 * while a real unmount still aborts the SDK consumer without cancelling the
 * backend Run.
 */
export function createDeferredThreadStreamDetach(
  schedule: (task: () => void) => void = queueMicrotask,
) {
  const pending = new Set<DeferredStreamDetach>();
  return {
    retain() {
      for (const detach of pending) detach.cancelled = true;
    },
    defer(disconnect: () => void) {
      const detach: DeferredStreamDetach = { cancelled: false };
      pending.add(detach);
      schedule(() => {
        pending.delete(detach);
        if (!detach.cancelled) disconnect();
      });
    },
  };
}
