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
  | { action: "drop"; queued: null }
  | { action: "send"; queued: null; value: T } {
  if (!queued) return { action: "wait", queued: null };
  if (
    !isCurrentSidecarIdentity(currentIdentity, queued.identity) ||
    sidecarThreadId !== queued.threadId
  ) {
    return { action: "drop", queued: null };
  }
  if (boundThreadId !== queued.threadId) {
    return { action: "wait", queued };
  }
  return { action: "send", queued: null, value: queued.value };
}
