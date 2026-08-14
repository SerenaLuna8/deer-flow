export type PendingPreparedReplayMask = {
  kind: "regenerate" | "edit";
  targetRunId: string;
  supersededMessageIds: string[];
  replacementHumanMessageId?: string;
};

export type PreparedReplayAttempt = {
  replay: PendingPreparedReplayMask;
  threadId: string;
  createdRunId: string | null;
  historyRefetchError: unknown | null;
  status: "submitting" | "succeeded" | "failed";
};

export function canStartPreparedReplay({
  threadId,
  sendInFlight,
  isThreadLoading,
}: {
  threadId: string | null | undefined;
  sendInFlight: boolean;
  isThreadLoading: boolean;
}): boolean {
  return Boolean(threadId) && !sendInFlight && !isThreadLoading;
}

export function getPreparedReplayStopRollback(
  attempt: {
    replay: PendingPreparedReplayMask;
    createdRunId: string | null;
    status: "submitting" | "succeeded" | "failed";
  } | null,
):
  | {
      replay: PendingPreparedReplayMask;
      failedRunId: string | null;
    }
  | undefined {
  if (attempt?.status !== "submitting") {
    return undefined;
  }
  return {
    replay: attempt.replay,
    failedRunId: attempt.createdRunId,
  };
}

export function removeSetItems<T>(
  values: ReadonlySet<T>,
  itemsToRemove: Iterable<T>,
) {
  const next = new Set(values);
  for (const item of itemsToRemove) {
    next.delete(item);
  }
  return next;
}

export function getRunMasksAfterPreparedReplayFailure(
  current: ReadonlySet<string>,
  replay: PendingPreparedReplayMask,
  failedRunId?: string | null,
) {
  const next = removeSetItems(current, [replay.targetRunId]);
  if (failedRunId) {
    next.add(failedRunId);
  }
  return next;
}

export function getMessageMasksAfterPreparedReplayFailure(
  current: ReadonlySet<string>,
  replay: PendingPreparedReplayMask,
) {
  const next = removeSetItems(current, replay.supersededMessageIds);
  if (replay.replacementHumanMessageId) {
    next.add(replay.replacementHumanMessageId);
  }
  return next;
}

export function classifyPreparedReplaySdkError({
  createdRunId,
  callbackRunId,
  error,
  historyRefetchError,
}: {
  createdRunId: string | null;
  callbackRunId: string | null;
  error: unknown;
  historyRefetchError: unknown | null;
}):
  | { kind: "rollback"; failedRunId: string | null }
  | { kind: "history-refetch-failure"; error: unknown }
  | { kind: "ignore-history-refetch-duplicate" }
  | { kind: "ignore-unrelated-run" } {
  if (!callbackRunId) {
    // In @langchain/langgraph-sdk 1.9.27, an error before onRunCreated has no
    // callback metadata. The same hook-level callback is also invoked without
    // metadata when the SDK's post-stream state refetch fails. A Run ID already
    // captured by onCreated is the reliable boundary between those two phases.
    return createdRunId
      ? { kind: "history-refetch-failure", error }
      : { kind: "rollback", failedRunId: null };
  }
  if (callbackRunId !== createdRunId) {
    return { kind: "ignore-unrelated-run" };
  }
  if (historyRefetchError !== null && Object.is(historyRefetchError, error)) {
    // The SDK rethrows the same state-refetch error through StreamManager after
    // first reporting it without metadata from useThreadHistory.
    return { kind: "ignore-history-refetch-duplicate" };
  }
  return { kind: "rollback", failedRunId: callbackRunId };
}
