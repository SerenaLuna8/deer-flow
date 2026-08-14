import type { Message } from "@langchain/langgraph-sdk";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { EventSequence } from "../private-work/event-sequence";
import { usePrivateWorkAccess } from "../private-work/provider";
import {
  runPrivateWorkAbortable,
  type ProjectPrivateWorkScope,
} from "../private-work/types";

import { fetchRunMessagesPage } from "./api";
import { dedupeMessagesByIdentity } from "./message-projection";
import {
  buildVisibleHistoryMessages,
  filterVisibleHistoryRows,
  findLatestUnloadedRunIndex,
  getNextRunMessagesBeforeSeq,
  getSupersededRunIds,
  mergeRunMessageRows,
  shouldAutoContinueOnEmptyRun,
} from "./run-history";
import { useThreadRuns } from "./thread-runs";
import type { RunMessage } from "./types";

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
  const runs = useThreadRuns(threadId, { enabled }, privateWork);
  const threadIdRef = useRef(threadId);
  const runsRef = useRef(runs.data ?? []);
  const indexRef = useRef(-1);
  const loadingRef = useRef(false);
  const pendingLoadRef = useRef(false);
  const loadingRunIdRef = useRef<string | null>(null);
  const loadedRunIdsRef = useRef<Set<string>>(new Set());
  const runBeforeSeqRef = useRef<Map<string, EventSequence>>(new Map());
  const loadGenerationRef = useRef(0);
  const [loading, setLoading] = useState(false);
  const [messageLoadError, setMessageLoadError] = useState<Error | null>(null);
  const [messageRows, setMessageRows] = useState<RunMessage[]>([]);
  const [appendedMessages, setAppendedMessages] = useState<Message[]>([]);

  const supersededRunIds = useMemo(() => {
    return getSupersededRunIds(runs.data, pendingSupersededRunIds);
  }, [pendingSupersededRunIds, runs.data]);

  const messages = useMemo(() => {
    return buildVisibleHistoryMessages(
      messageRows,
      supersededRunIds,
      appendedMessages,
    );
  }, [appendedMessages, messageRows, supersededRunIds]);

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
        setMessageRows((prev) =>
          mergeRunMessageRows(prev, _messages, runsRef.current),
        );
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
          loadedRunIdsRef.current.add(run.run_id);
          if (
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
      runBeforeSeqRef.current = new Map();
      loadingRef.current = false;
      setLoading(false);
      setMessageLoadError(null);
      setMessageRows([]);
      setAppendedMessages([]);
    }

    if (!enabled) {
      return;
    }

    if (runs.data && runs.data.length > 0) {
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
  return {
    runs: runs.data,
    messages,
    loading: loading || isRunsLoading || isRunsUnresolved,
    appendMessages,
    hasMore,
    loadMore: loadMessages,
    error: historyError,
    retry: retryHistory,
  };
}
