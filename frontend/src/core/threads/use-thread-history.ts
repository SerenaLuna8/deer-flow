import type { Message, Run } from "@langchain/langgraph-sdk";
import { useQueryClient } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { EventSequence } from "../private-work/event-sequence";
import { usePrivateWorkAccess } from "../private-work/provider";
import {
  runPrivateWorkAbortable,
  type ProjectPrivateWorkScope,
} from "../private-work/types";

import { fetchRunMessagesPage } from "./api";
import {
  dedupeMessagesByIdentity,
  messageIdentity,
  messageRunId,
} from "./message-projection";
import {
  buildVisibleHistoryMessages,
  filterVisibleHistoryRows,
  findTerminalFailureRunIdsToReload,
  findLatestUnloadedRunIndex,
  getNextRunMessagesBeforeSeq,
  getSupersededRunIds,
  isInitialHistoryWindowLoaded,
  mergeRunMessageRows,
  shouldReloadEmptyRunAfterTerminalFailure,
  shouldAutoContinueOnEmptyRun,
} from "./run-history";
import { scopedThreadQueryKey } from "./thread-query-key";
import { useThreadRuns } from "./thread-runs";
import type { RunMessage } from "./types";

const CANONICAL_RUN_HISTORY_MAX_MESSAGE_PAGES = 1000;

type RunsGetClient = {
  runs: {
    get(
      threadId: string,
      runId: string,
      options?: { signal?: AbortSignal },
    ): Promise<unknown>;
  };
};

export type CanonicalRunHistory = Readonly<{
  threadId: string;
  run: Run;
  rows: RunMessage[];
}>;

export async function fetchCanonicalRunHistory(
  apiClient: RunsGetClient,
  apiBaseURL: string,
  threadId: string,
  runId: string,
  signal: AbortSignal,
): Promise<CanonicalRunHistory> {
  const run = (await apiClient.runs.get(threadId, runId, { signal })) as Run;
  if (run.run_id !== runId || run.thread_id !== threadId) {
    throw new Error("Canonical REST returned a different Run authority.");
  }
  if (
    !["success", "error", "timeout", "interrupted"].includes(
      run.status as string,
    )
  ) {
    throw new Error("Canonical REST did not confirm the terminal Run.");
  }
  let rows: RunMessage[] = [];
  let beforeSeq: EventSequence | undefined;
  let messagePageCount = 0;

  while (true) {
    signal.throwIfAborted();
    messagePageCount += 1;
    if (messagePageCount > CANONICAL_RUN_HISTORY_MAX_MESSAGE_PAGES) {
      throw new Error("Canonical Run history exceeded the page limit.");
    }
    const page = await fetchRunMessagesPage(
      apiBaseURL,
      threadId,
      runId,
      beforeSeq,
      signal,
    );
    rows = mergeRunMessageRows(rows, page.data, [run]);
    const nextBeforeSeq = getNextRunMessagesBeforeSeq(page);
    if (nextBeforeSeq === null) break;
    if (nextBeforeSeq === undefined) {
      throw new Error(`Run ${runId} returned a non-advancing message page.`);
    }
    beforeSeq = nextBeforeSeq;
  }

  return { threadId, run, rows };
}

export type TerminalRunHistoryCommitResult =
  | Readonly<{ kind: "stale" }>
  | Readonly<{
      kind: "committed";
      messages: Message[];
      capturedFallback: Message[];
    }>;

function mergeCanonicalRunMessages(
  canonicalMessages: Message[],
  capturedMessages: Message[],
): Message[] {
  const canonicalIdentities = new Set(
    canonicalMessages
      .map(messageIdentity)
      .filter((identity): identity is string => identity !== undefined),
  );
  const capturedBeforeCanonical = new Map<string, Message[]>();
  const capturedAfterCanonical: Message[] = [];
  let nextCanonicalIdentity: string | undefined;

  for (let index = capturedMessages.length - 1; index >= 0; index -= 1) {
    const message = capturedMessages[index];
    if (!message) continue;
    const identity = messageIdentity(message);
    if (identity !== undefined && canonicalIdentities.has(identity)) {
      nextCanonicalIdentity = identity;
      continue;
    }
    if (nextCanonicalIdentity === undefined) {
      capturedAfterCanonical.unshift(message);
      continue;
    }
    const before = capturedBeforeCanonical.get(nextCanonicalIdentity) ?? [];
    before.unshift(message);
    capturedBeforeCanonical.set(nextCanonicalIdentity, before);
  }

  const merged: Message[] = [];
  for (const message of canonicalMessages) {
    const identity = messageIdentity(message);
    if (identity !== undefined) {
      merged.push(...(capturedBeforeCanonical.get(identity) ?? []));
    }
    merged.push(message);
  }
  merged.push(...capturedAfterCanonical);
  return dedupeMessagesByIdentity(merged);
}

export function projectTerminalRunFallbacks(
  canonicalHistory: Message[],
  capturedFallbacks: ReadonlyMap<string, Message[]>,
  runsNewestFirst: Run[],
  supersededRunIds: ReadonlySet<string>,
): Message[] {
  if (capturedFallbacks.size === 0) {
    return canonicalHistory;
  }
  const chronologicalRunOrder = new Map<string, number>();
  [...runsNewestFirst]
    .reverse()
    .forEach((run, index) => chronologicalRunOrder.set(run.run_id, index));
  const orderedFallbacks = [...capturedFallbacks.entries()].sort(
    ([leftRunId], [rightRunId]) =>
      (chronologicalRunOrder.get(leftRunId) ?? Number.MAX_SAFE_INTEGER) -
      (chronologicalRunOrder.get(rightRunId) ?? Number.MAX_SAFE_INTEGER),
  );
  let projected = canonicalHistory;

  for (const [runId, capturedMessages] of orderedFallbacks) {
    if (supersededRunIds.has(runId)) continue;
    const firstRunIndex = projected.findIndex(
      (message) => messageRunId(message) === runId,
    );
    const canonicalRunMessages = projected.filter(
      (message) => messageRunId(message) === runId,
    );
    const mergedRunMessages = mergeCanonicalRunMessages(
      canonicalRunMessages,
      capturedMessages,
    );
    const withoutRun = projected.filter(
      (message) => messageRunId(message) !== runId,
    );
    let insertionIndex = firstRunIndex;
    if (insertionIndex < 0) {
      const targetOrder = chronologicalRunOrder.get(runId);
      insertionIndex =
        targetOrder === undefined
          ? withoutRun.length
          : withoutRun.findIndex((message) => {
              const messageOrder = chronologicalRunOrder.get(
                messageRunId(message) ?? "",
              );
              return messageOrder !== undefined && messageOrder > targetOrder;
            });
      if (insertionIndex < 0) insertionIndex = withoutRun.length;
    }
    projected = [
      ...withoutRun.slice(0, insertionIndex),
      ...mergedRunMessages,
      ...withoutRun.slice(insertionIndex),
    ];
  }
  return dedupeMessagesByIdentity(projected);
}

export function pruneConfirmedTerminalRunFallbacks(
  capturedFallbacks: ReadonlyMap<string, Message[]>,
  canonicalHistory: Message[],
  supersededRunIds: ReadonlySet<string>,
): ReadonlyMap<string, Message[]> {
  let next: Map<string, Message[]> | null = null;
  for (const [runId, capturedMessages] of capturedFallbacks) {
    const canonicalIdentities = new Set(
      canonicalHistory
        .filter((message) => messageRunId(message) === runId)
        .map(messageIdentity)
        .filter((identity): identity is string => identity !== undefined),
    );
    const confirmed = capturedMessages.every((message) => {
      const identity = messageIdentity(message);
      return identity !== undefined && canonicalIdentities.has(identity);
    });
    if (!supersededRunIds.has(runId) && !confirmed) continue;
    next ??= new Map(capturedFallbacks);
    next.delete(runId);
  }
  return next ?? capturedFallbacks;
}

export function resolveTerminalRunHistoryCommit({
  boundThreadId,
  snapshot,
  capturedMessages,
}: {
  boundThreadId: string;
  snapshot: CanonicalRunHistory;
  capturedMessages: Message[];
}): TerminalRunHistoryCommitResult {
  if (boundThreadId !== snapshot.threadId) {
    return { kind: "stale" };
  }
  const runId = snapshot.run.run_id;
  if (snapshot.run.thread_id !== snapshot.threadId) {
    throw new Error("Canonical Run does not belong to the target Thread.");
  }
  if (snapshot.rows.some((row) => row.run_id !== runId)) {
    throw new Error("Canonical Run history contains a row from another Run.");
  }

  const canonicalMessages = buildVisibleHistoryMessages(
    snapshot.rows,
    new Set(),
    [],
  );
  const capturedRunMessages = dedupeMessagesByIdentity(
    capturedMessages.filter(
      (message) =>
        messageRunId(message) === runId &&
        messageIdentity(message) !== undefined,
    ),
  );
  const canonicalIdentities = new Set(
    canonicalMessages
      .map(messageIdentity)
      .filter((identity): identity is string => identity !== undefined),
  );
  const hasCapturedOnlyMessage = capturedRunMessages.some((message) => {
    const identity = messageIdentity(message);
    return identity !== undefined && !canonicalIdentities.has(identity);
  });
  const hasSharedIdentity = capturedRunMessages.some((message) => {
    const identity = messageIdentity(message);
    return identity !== undefined && canonicalIdentities.has(identity);
  });
  if (hasCapturedOnlyMessage && !hasSharedIdentity) {
    throw new Error("Captured terminal messages have no canonical anchor.");
  }
  const capturedFallback = hasCapturedOnlyMessage ? capturedRunMessages : [];
  return {
    kind: "committed",
    messages:
      capturedFallback.length > 0
        ? mergeCanonicalRunMessages(canonicalMessages, capturedFallback)
        : canonicalMessages,
    capturedFallback,
  };
}

function upsertCanonicalRun(runs: Run[], canonicalRun: Run): Run[] {
  const existingIndex = runs.findIndex(
    (run) => run.run_id === canonicalRun.run_id,
  );
  if (existingIndex < 0) {
    return [canonicalRun, ...runs];
  }
  return runs.map((run, index) =>
    index === existingIndex ? canonicalRun : run,
  );
}

type ThreadHistoryOptions = {
  enabled?: boolean;
  pendingSupersededRunIds?: ReadonlySet<string>;
  privateWork?: ProjectPrivateWorkScope;
};

export function useThreadHistory(
  threadId: string,
  {
    enabled = true,
    pendingSupersededRunIds,
    privateWork: explicitPrivateWork,
  }: ThreadHistoryOptions = {},
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();
  const runs = useThreadRuns(threadId, { enabled }, privateWork);
  const threadIdRef = useRef(threadId);
  const runsRef = useRef(runs.data ?? []);
  const indexRef = useRef(-1);
  const loadingRef = useRef(false);
  const pendingLoadRef = useRef(false);
  const loadingRunIdRef = useRef<string | null>(null);
  const loadedRunIdsRef = useRef<Set<string>>(new Set());
  const runStatusesRef = useRef<Map<string, string>>(new Map());
  const runBeforeSeqRef = useRef<Map<string, EventSequence>>(new Map());
  const initialHistoryPublishedRef = useRef(false);
  const initialHistoryStagedRowsRef = useRef<RunMessage[]>([]);
  const loadGenerationRef = useRef(0);
  const pendingTerminalCommitRef = useRef<{
    threadId: string;
    result: TerminalRunHistoryCommitResult;
    resolve(result: TerminalRunHistoryCommitResult): void;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [messageLoadError, setMessageLoadError] = useState<Error | null>(null);
  const [messageRows, setMessageRows] = useState<RunMessage[]>([]);
  const [appendedMessages, setAppendedMessages] = useState<Message[]>([]);
  const [terminalRunFallbacks, setTerminalRunFallbacks] = useState<
    ReadonlyMap<string, Message[]>
  >(() => new Map());

  const supersededRunIds = useMemo(() => {
    return getSupersededRunIds(runs.data, pendingSupersededRunIds);
  }, [pendingSupersededRunIds, runs.data]);

  const canonicalMessages = useMemo(
    () =>
      buildVisibleHistoryMessages(
        messageRows,
        supersededRunIds,
        appendedMessages,
      ),
    [appendedMessages, messageRows, supersededRunIds],
  );
  const messages = useMemo(() => {
    return projectTerminalRunFallbacks(
      canonicalMessages,
      terminalRunFallbacks,
      runs.data ?? runsRef.current,
      supersededRunIds,
    );
  }, [canonicalMessages, runs.data, supersededRunIds, terminalRunFallbacks]);
  useLayoutEffect(() => {
    const pending = pendingTerminalCommitRef.current;
    if (!pending) {
      return;
    }
    pendingTerminalCommitRef.current = null;
    pending.resolve(
      pending.threadId === threadId ? pending.result : { kind: "stale" },
    );
  }, [messages, threadId]);
  useEffect(
    () => () => {
      const pending = pendingTerminalCommitRef.current;
      pendingTerminalCommitRef.current = null;
      pending?.resolve({ kind: "stale" });
    },
    [],
  );
  useEffect(() => {
    setTerminalRunFallbacks((current) =>
      pruneConfirmedTerminalRunFallbacks(
        current,
        canonicalMessages,
        supersededRunIds,
      ),
    );
  }, [canonicalMessages, supersededRunIds]);

  const loadMessages = useCallback(async () => {
    if (!enabled) {
      return;
    }
    const loadGeneration = loadGenerationRef.current;
    if (loadingRef.current) {
      const pendingRunIndex = findLatestUnloadedRunIndex(
        runsRef.current,
        loadedRunIdsRef.current,
      );
      const pendingRun = runsRef.current[pendingRunIndex];
      if (pendingRun && pendingRun.run_id !== loadingRunIdRef.current) {
        pendingLoadRef.current = true;
      }
      return;
    }
    if (runsRef.current.length === 0) {
      return;
    }

    const stagingInitialHistory = !initialHistoryPublishedRef.current;
    loadingRef.current = true;
    setMessageLoadError(null);
    setLoading(true);

    try {
      let consecutiveEmptyLoads = 0;
      do {
        pendingLoadRef.current = false;

        const nextRunIndex = findLatestUnloadedRunIndex(
          runsRef.current,
          loadedRunIdsRef.current,
        );
        indexRef.current = nextRunIndex;

        const run = runsRef.current[nextRunIndex];
        if (!run) {
          indexRef.current = -1;
          return;
        }

        const requestThreadId = threadIdRef.current;
        loadingRunIdRef.current = run.run_id;
        const runStatusAtRequest = run.status as string;
        const beforeSeq = runBeforeSeqRef.current.get(run.run_id);
        const result = await runPrivateWorkAbortable(privateWork, (signal) =>
          fetchRunMessagesPage(
            privateWork.apiBaseURL,
            requestThreadId,
            run.run_id,
            beforeSeq,
            signal,
          ),
        );
        if (
          loadGenerationRef.current !== loadGeneration ||
          threadIdRef.current !== requestThreadId
        ) {
          return;
        }
        const _messages = result.data;
        if (stagingInitialHistory && !initialHistoryPublishedRef.current) {
          initialHistoryStagedRowsRef.current = mergeRunMessageRows(
            initialHistoryStagedRowsRef.current,
            _messages,
            runsRef.current,
          );
        } else {
          setMessageRows((prev) =>
            mergeRunMessageRows(prev, _messages, runsRef.current),
          );
        }
        const nextBeforeSeq = getNextRunMessagesBeforeSeq(result);
        if (typeof nextBeforeSeq === "string") {
          runBeforeSeqRef.current.set(run.run_id, nextBeforeSeq);
          pendingLoadRef.current = true;
        } else if (nextBeforeSeq === undefined) {
          throw new Error(
            `Run ${run.run_id} returned a non-advancing message page.`,
          );
        } else {
          runBeforeSeqRef.current.delete(run.run_id);
          const currentRunStatus = runsRef.current.find(
            (candidate) => candidate.run_id === run.run_id,
          )?.status as string | undefined;
          const failedWhileLoading = shouldReloadEmptyRunAfterTerminalFailure({
            statusAtRequest: runStatusAtRequest,
            currentStatus: currentRunStatus,
            visibleMessageCount: filterVisibleHistoryRows(_messages).length,
          });
          if (failedWhileLoading) {
            loadedRunIdsRef.current.delete(run.run_id);
            pendingLoadRef.current = true;
          } else {
            loadedRunIdsRef.current.add(run.run_id);
          }
          if (stagingInitialHistory && !initialHistoryPublishedRef.current) {
            pendingLoadRef.current = !isInitialHistoryWindowLoaded(
              runsRef.current,
              loadedRunIdsRef.current,
            );
          } else if (
            !failedWhileLoading &&
            shouldAutoContinueOnEmptyRun(
              filterVisibleHistoryRows(_messages).length,
              consecutiveEmptyLoads,
            )
          ) {
            consecutiveEmptyLoads += 1;
            pendingLoadRef.current = true;
          } else {
            consecutiveEmptyLoads = 0;
          }
        }
        indexRef.current = findLatestUnloadedRunIndex(
          runsRef.current,
          loadedRunIdsRef.current,
        );
      } while (pendingLoadRef.current);

      if (
        stagingInitialHistory &&
        !initialHistoryPublishedRef.current &&
        isInitialHistoryWindowLoaded(runsRef.current, loadedRunIdsRef.current)
      ) {
        const stagedRows = initialHistoryStagedRowsRef.current;
        initialHistoryStagedRowsRef.current = [];
        initialHistoryPublishedRef.current = true;
        setMessageRows((prev) =>
          mergeRunMessageRows(prev, stagedRows, runsRef.current),
        );
      }
    } catch (err) {
      pendingLoadRef.current = false;
      if (loadGenerationRef.current === loadGeneration) {
        setMessageLoadError(
          err instanceof Error
            ? err
            : new Error("Failed to load thread history."),
        );
      }
    } finally {
      if (loadGenerationRef.current === loadGeneration) {
        loadingRef.current = false;
        loadingRunIdRef.current = null;
        setLoading(false);
      }
    }
  }, [enabled, privateWork]);
  useEffect(() => {
    const threadChanged = threadIdRef.current !== threadId;
    threadIdRef.current = threadId;

    if (!enabled || threadChanged) {
      loadGenerationRef.current += 1;
      runsRef.current = [];
      indexRef.current = -1;
      pendingLoadRef.current = false;
      loadingRunIdRef.current = null;
      loadedRunIdsRef.current = new Set();
      runStatusesRef.current = new Map();
      runBeforeSeqRef.current = new Map();
      initialHistoryPublishedRef.current = false;
      initialHistoryStagedRowsRef.current = [];
      loadingRef.current = false;
      setLoading(false);
      setMessageLoadError(null);
      setMessageRows([]);
      setAppendedMessages([]);
      setTerminalRunFallbacks(new Map());
    }

    if (!enabled) {
      return;
    }

    if (runs.data && runs.data.length > 0) {
      const terminalReloadRunIds = findTerminalFailureRunIdsToReload(
        runStatusesRef.current,
        runs.data,
      );
      for (const runId of terminalReloadRunIds) {
        loadedRunIdsRef.current.delete(runId);
        runBeforeSeqRef.current.delete(runId);
      }
      if (terminalReloadRunIds.length > 0 && loadingRef.current) {
        pendingLoadRef.current = true;
      }
      runStatusesRef.current = new Map(
        runs.data.map((run) => [run.run_id, run.status as string]),
      );
      runsRef.current = runs.data ?? [];
      indexRef.current = findLatestUnloadedRunIndex(
        runs.data,
        loadedRunIdsRef.current,
      );
    }
    void loadMessages();
  }, [enabled, threadId, runs.data, loadMessages]);

  const appendMessages = useCallback((_messages: Message[]) => {
    setAppendedMessages((prev) => {
      return dedupeMessagesByIdentity([...prev, ..._messages]);
    });
  }, []);
  const hasThreadId = Boolean(threadId);
  const hasUnloadedRuns = Boolean(
    runs.data?.some((run) => !loadedRunIdsRef.current.has(run.run_id)),
  );
  const isRunsLoading =
    enabled &&
    hasThreadId &&
    (runs.isLoading || (runs.isFetching && !runs.data));
  const isRunsUnresolved =
    enabled && hasThreadId && !runs.data && !runs.isError;
  const historyError = messageLoadError ?? runs.error;
  const hasMore =
    enabled &&
    hasThreadId &&
    historyError === null &&
    (indexRef.current >= 0 || hasUnloadedRuns);
  const runsFailed = runs.isError;
  const refetchRuns = runs.refetch;
  const retryHistory = useCallback(() => {
    if (runsFailed) {
      void refetchRuns();
      return;
    }
    void loadMessages();
  }, [loadMessages, refetchRuns, runsFailed]);
  const refetchCanonicalRun = useCallback(
    (targetThreadId: string, targetRunId: string) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        fetchCanonicalRunHistory(
          privateWork.client,
          privateWork.apiBaseURL,
          targetThreadId,
          targetRunId,
          signal ?? new AbortController().signal,
        ),
      ),
    [privateWork],
  );
  const commitTerminalRun = useCallback(
    (
      snapshot: CanonicalRunHistory,
      capturedMessages: Message[],
    ): Promise<TerminalRunHistoryCommitResult> => {
      const resolved = resolveTerminalRunHistoryCommit({
        boundThreadId: threadIdRef.current,
        snapshot,
        capturedMessages,
      });
      if (resolved.kind === "stale") {
        return Promise.resolve(resolved);
      }

      const runId = snapshot.run.run_id;
      const nextRuns = upsertCanonicalRun(runsRef.current, snapshot.run);
      runsRef.current = nextRuns;
      runStatusesRef.current.set(runId, snapshot.run.status as string);
      loadedRunIdsRef.current.add(runId);
      runBeforeSeqRef.current.delete(runId);
      indexRef.current = findLatestUnloadedRunIndex(
        nextRuns,
        loadedRunIdsRef.current,
      );
      let committedRows = snapshot.rows;
      if (!initialHistoryPublishedRef.current) {
        committedRows = mergeRunMessageRows(
          initialHistoryStagedRowsRef.current,
          snapshot.rows,
          nextRuns,
        );
        initialHistoryStagedRowsRef.current = [];
        initialHistoryPublishedRef.current = true;
      }
      setMessageRows((previous) =>
        mergeRunMessageRows(previous, committedRows, nextRuns),
      );
      setTerminalRunFallbacks((previous) => {
        const next = new Map(previous);
        if (resolved.capturedFallback.length > 0) {
          next.set(runId, resolved.capturedFallback);
        } else {
          next.delete(runId);
        }
        return next;
      });
      setMessageLoadError(null);
      queryClient.setQueryData<Run[]>(
        scopedThreadQueryKey(privateWork.scope, "thread", snapshot.threadId),
        (current) => upsertCanonicalRun(current ?? nextRuns, snapshot.run),
      );
      return new Promise((resolve) => {
        pendingTerminalCommitRef.current = {
          threadId: snapshot.threadId,
          result: resolved,
          resolve,
        };
      });
    },
    [privateWork.scope, queryClient],
  );
  return {
    runs: runs.data,
    messages,
    loading: loading || isRunsLoading || isRunsUnresolved,
    appendMessages,
    hasMore,
    loadMore: loadMessages,
    error: historyError,
    retry: retryHistory,
    refetchCanonicalRun,
    commitTerminalRun,
  };
}
