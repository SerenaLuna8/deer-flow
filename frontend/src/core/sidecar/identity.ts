export type SidecarIdentity = Readonly<{
  parentThreadId: string;
  generation: number;
}>;

export type SidecarThreadBinding = Readonly<{
  identity: SidecarIdentity;
  threadId: string;
}>;

export type SidecarQueuedValue<T> = Readonly<{
  identity: SidecarIdentity;
  threadId: string;
  value: T;
}>;

export type SidecarQueueSettlement = Readonly<{
  promise: Promise<void>;
  isPending: () => boolean;
  resolve: () => boolean;
  reject: (error: unknown) => boolean;
}>;

export function createSidecarQueueSettlement(): SidecarQueueSettlement {
  let pending = true;
  let resolvePromise!: () => void;
  let rejectPromise!: (error: unknown) => void;
  const promise = new Promise<void>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  // A queued submission can be cancelled synchronously (for example during an
  // unmount) before its caller reaches `await settlement.promise`. Attach a
  // rejection observer immediately so that cancellation never becomes an
  // unhandled rejection; awaiting the original promise still rejects normally.
  void promise.catch(() => undefined);
  return {
    promise,
    isPending: () => pending,
    resolve: () => {
      if (!pending) return false;
      pending = false;
      resolvePromise();
      return true;
    },
    reject: (error) => {
      if (!pending) return false;
      pending = false;
      rejectPromise(error);
      return true;
    },
  };
}

export async function awaitAbortableSidecarPreparation<T>(
  signal: AbortSignal,
  prepare: () => Promise<T>,
): Promise<T> {
  const abortReason = () => {
    if (signal.reason instanceof Error) return signal.reason;
    const error = new Error("The side conversation closed before admission.");
    error.name = "AbortError";
    return error;
  };
  if (signal.aborted) {
    throw abortReason();
  }

  let onAbort: (() => void) | undefined;
  const aborted = new Promise<never>((_, reject) => {
    onAbort = () => reject(abortReason());
    signal.addEventListener("abort", onAbort, { once: true });
  });
  try {
    // Promise.race installs rejection handlers on both branches. The underlying
    // request may be non-abortable, but a late failure cannot leak and its
    // result is ignored after this operation has been cancelled.
    return await Promise.race([prepare(), aborted]);
  } finally {
    if (onAbort) {
      signal.removeEventListener("abort", onAbort);
    }
  }
}

export async function settleSidecarQueueSubmission(
  settlement: SidecarQueueSettlement,
  submit: () => Promise<void>,
): Promise<void> {
  try {
    await submit();
    settlement.resolve();
  } catch (error) {
    settlement.reject(error);
  }
}

export function canClaimSidecarQueue<T>(
  current: SidecarQueuedValue<T> | null,
  candidate: SidecarQueuedValue<T> | null,
): boolean {
  return candidate !== null && current === candidate;
}

export function createSidecarIdentity(parentThreadId: string): SidecarIdentity {
  return { parentThreadId, generation: 0 };
}

export function advanceSidecarIdentity(
  current: SidecarIdentity,
  parentThreadId: string = current.parentThreadId,
): SidecarIdentity {
  return { parentThreadId, generation: current.generation + 1 };
}

export function isCurrentSidecarIdentity(
  current: SidecarIdentity,
  candidate: SidecarIdentity,
): boolean {
  return (
    current.parentThreadId === candidate.parentThreadId &&
    current.generation === candidate.generation
  );
}

export function adoptSidecarThread(
  current: SidecarIdentity,
  candidate: SidecarIdentity,
  threadId: string,
): SidecarThreadBinding | null {
  return isCurrentSidecarIdentity(current, candidate)
    ? { identity: candidate, threadId }
    : null;
}

export function visibleSidecarThreadId(
  current: SidecarIdentity,
  binding: SidecarThreadBinding | null,
): string | null {
  return binding && isCurrentSidecarIdentity(current, binding.identity)
    ? binding.threadId
    : null;
}

export function guardSidecarClear(
  current: SidecarIdentity,
  candidate: SidecarIdentity,
): boolean {
  return isCurrentSidecarIdentity(current, candidate);
}

export function consumeSidecarQueue<T>({
  currentIdentity,
  queued,
  sidecarThreadId,
  boundThreadId,
}:
  | {
      currentIdentity: SidecarIdentity;
      queued: SidecarQueuedValue<T>;
      sidecarThreadId: string | null;
      boundThreadId: string | null;
    }
  | {
      currentIdentity: SidecarIdentity;
      queued: null;
      sidecarThreadId: string | null;
      boundThreadId: string | null;
    }):
  | { action: "wait"; queued: SidecarQueuedValue<T> | null }
  | { action: "drop"; queued: SidecarQueuedValue<T> }
  | { action: "send"; queued: null; value: T } {
  if (!queued) return { action: "wait", queued: null };
  if (
    !isCurrentSidecarIdentity(currentIdentity, queued.identity) ||
    sidecarThreadId !== queued.threadId
  ) {
    return { action: "drop", queued };
  }
  if (boundThreadId !== queued.threadId) {
    return { action: "wait", queued };
  }
  return { action: "send", queued: null, value: queued.value };
}
