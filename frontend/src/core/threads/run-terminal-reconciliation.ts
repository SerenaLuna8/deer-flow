import { z } from "zod";

import {
  projectClientScopeSchema,
  type RunMetadataStorage,
} from "@/core/private-work/types";

const runTerminalCanonicalAuthoritySchema = projectClientScopeSchema
  .extend({
    threadId: z.string().uuid(),
    runId: z.string().uuid(),
    generation: z.number().int().positive(),
  })
  .strict();

export type RunTerminalCanonicalAuthority = Readonly<
  z.infer<typeof runTerminalCanonicalAuthoritySchema>
>;

export type RunTerminalReconciliationAdapters<CanonicalHistory> = {
  readCurrentAuthority(): RunTerminalCanonicalAuthority | null;
  reconnectStorage: RunMetadataStorage;
  setControlledThreadId(threadId: string | null): void;
  switchLocalThreadToNull(): void | Promise<void>;
  refetchCanonicalThread(
    target: RunTerminalCanonicalAuthority,
  ): Promise<CanonicalHistory>;
  mergeLiveWithCanonicalHistory(
    target: RunTerminalCanonicalAuthority,
    history: CanonicalHistory,
  ): void | Promise<void>;
};

export type RunTerminalReconciliationStage =
  | "initial"
  | "reconnect-cleared"
  | "controlled-thread-cleared"
  | "local-thread-detached"
  | "canonical-history-refetched"
  | "history-merged"
  | "reconnect-confirmed"
  | "controlled-thread-restored";

export type RunTerminalReconciliationResult =
  | Readonly<{ kind: "reconciled" }>
  | Readonly<{ kind: "stale"; stage: RunTerminalReconciliationStage }>
  | Readonly<{
      kind: "failed";
      stage: "canonical-history-refetch" | "history-merge";
      error: unknown;
    }>
  | Readonly<{
      kind: "blocked";
      reason: "terminal-reconnect-present";
    }>;

export type RunTerminalReconciliationRetryAdapters = {
  readCurrentAuthority(): RunTerminalCanonicalAuthority | null;
  clearFailure(authority: RunTerminalCanonicalAuthority): void;
  reconcile(
    authority: RunTerminalCanonicalAuthority,
  ): Promise<RunTerminalReconciliationResult>;
  retryCanonicalHistory(): void;
};

export type RunTerminalReconciliationRetryResult =
  | Readonly<{
      kind: "terminal-reconciliation-retried";
      result: RunTerminalReconciliationResult;
    }>
  | Readonly<{ kind: "canonical-history-retried" }>;

function reconnectKey(threadId: string): `lg:stream:${string}` {
  return `lg:stream:${threadId}`;
}

function sameAuthority(
  left: RunTerminalCanonicalAuthority,
  right: RunTerminalCanonicalAuthority,
): boolean {
  return (
    left.accountId === right.accountId &&
    left.projectId === right.projectId &&
    left.threadId === right.threadId &&
    left.runId === right.runId &&
    left.generation === right.generation
  );
}

export async function retryTerminalReconciliation(
  failedAuthority: RunTerminalCanonicalAuthority,
  adapters: RunTerminalReconciliationRetryAdapters,
): Promise<RunTerminalReconciliationRetryResult> {
  const target = Object.freeze(
    runTerminalCanonicalAuthoritySchema.parse(failedAuthority),
  );
  const current = runTerminalCanonicalAuthoritySchema.safeParse(
    adapters.readCurrentAuthority(),
  );
  if (!current.success || !sameAuthority(target, current.data)) {
    adapters.retryCanonicalHistory();
    return { kind: "canonical-history-retried" };
  }
  adapters.clearFailure(target);
  return {
    kind: "terminal-reconciliation-retried",
    result: await adapters.reconcile(target),
  };
}

function isCurrent<CanonicalHistory>(
  target: RunTerminalCanonicalAuthority,
  adapters: RunTerminalReconciliationAdapters<CanonicalHistory>,
): boolean {
  const current = runTerminalCanonicalAuthoritySchema.safeParse(
    adapters.readCurrentAuthority(),
  );
  return current.success && sameAuthority(target, current.data);
}

function clearReconnectValue(
  storage: RunMetadataStorage,
  key: `lg:stream:${string}`,
  expectedRunId: string,
): void {
  if (storage.getItem(key) === expectedRunId) {
    storage.removeItem(key);
  }
}

function stale(
  stage: RunTerminalReconciliationStage,
): RunTerminalReconciliationResult {
  return { kind: "stale", stage };
}

export async function reconcileTerminalRun<CanonicalHistory>(
  authority: RunTerminalCanonicalAuthority,
  adapters: RunTerminalReconciliationAdapters<CanonicalHistory>,
): Promise<RunTerminalReconciliationResult> {
  const target = Object.freeze(
    runTerminalCanonicalAuthoritySchema.parse(authority),
  );
  if (!isCurrent(target, adapters)) return stale("initial");

  const key = reconnectKey(target.threadId);
  clearReconnectValue(adapters.reconnectStorage, key, target.runId);
  if (!isCurrent(target, adapters)) return stale("reconnect-cleared");

  adapters.setControlledThreadId(null);
  if (!isCurrent(target, adapters)) {
    return stale("controlled-thread-cleared");
  }

  await adapters.switchLocalThreadToNull();
  if (!isCurrent(target, adapters)) return stale("local-thread-detached");

  let history: CanonicalHistory;
  try {
    history = await adapters.refetchCanonicalThread(target);
  } catch (error) {
    if (!isCurrent(target, adapters)) {
      return stale("canonical-history-refetched");
    }
    clearReconnectValue(adapters.reconnectStorage, key, target.runId);
    return {
      kind: "failed",
      stage: "canonical-history-refetch",
      error,
    };
  }
  if (!isCurrent(target, adapters)) {
    return stale("canonical-history-refetched");
  }

  try {
    await adapters.mergeLiveWithCanonicalHistory(target, history);
  } catch (error) {
    if (!isCurrent(target, adapters)) return stale("history-merged");
    return { kind: "failed", stage: "history-merge", error };
  }
  if (!isCurrent(target, adapters)) return stale("history-merged");

  if (adapters.reconnectStorage.getItem(key) === target.runId) {
    return { kind: "blocked", reason: "terminal-reconnect-present" };
  }
  if (!isCurrent(target, adapters)) return stale("reconnect-confirmed");

  adapters.setControlledThreadId(target.threadId);
  if (!isCurrent(target, adapters)) {
    return stale("controlled-thread-restored");
  }
  return { kind: "reconciled" };
}
