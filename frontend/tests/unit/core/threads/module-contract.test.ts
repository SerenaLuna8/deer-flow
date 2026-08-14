import { describe, expect, test } from "@rstest/core";

import * as hooks from "@/core/threads/hooks";
import {
  attachRunIdToNewMessages,
  computeSummarizationMovedMessages,
  countHumanMessagesExcludingSuperseded,
  filterMessagesBySupersededRunIds,
  getLatestRegenerationTarget,
  getSummarizationMiddlewareMessages,
  getVisibleOptimisticMessages,
  mergeMessages,
  overlayThreadProjection,
  projectThreadMessages,
  pruneConfirmedArchivedMessages,
  resolveActiveRunIdForMessages,
  resolvePreservedHistory,
} from "@/core/threads/message-projection";
import {
  canStartPreparedReplay,
  classifyPreparedReplaySdkError,
  getMessageMasksAfterPreparedReplayFailure,
  getPreparedReplayStopRollback,
  getRunMasksAfterPreparedReplayFailure,
  removeSetItems,
} from "@/core/threads/prepared-replay";
import {
  buildVisibleHistoryMessages,
  findLatestUnloadedRunIndex,
  getNextRunMessagesBeforeSeq,
  getOldestRunMessageSeq,
  getSupersededRunIds,
  latestRunFailureCode,
  latestRunHasTerminalFailure,
  MAX_CONSECUTIVE_EMPTY_RUN_LOADS,
  mergeRunMessageRows,
  rememberActiveRun,
  resolveRunFailureCode,
  resolveRunFailureRunId,
  runMessagesPageHasMore,
  shouldAutoContinueOnEmptyRun,
} from "@/core/threads/run-history";
import {
  buildThreadSubmitCheckpointOptions,
  buildThreadSubmitMessages,
  uploadedFileInfoToMessage,
} from "@/core/threads/send-message";
import {
  deleteThreadEverywhere,
  findSidecarThreadIdsForParent,
  useBranchThread,
  useDeleteThread,
  useRenameThread,
  useRunDetail,
  useThreadContextUsage,
  useThreadMetadata,
  useThreadTokenUsage,
} from "@/core/threads/thread-actions";
import {
  invalidateStoppedThreadCaches,
  STOP_THREAD_FINALIZATION_REFETCH_DELAY_MS,
  stopThreadAndInvalidateCaches,
  upsertThreadInInfiniteCache,
  upsertThreadInSearchCache,
} from "@/core/threads/thread-cache";
import {
  fetchInfiniteThreadsPage,
  filterInfiniteThreadsCache,
  getInfiniteThreadsNextPageParam,
  INFINITE_THREADS_PAGE_SIZE,
  INFINITE_THREADS_QUERY_KEY_PREFIX,
  mapInfiniteThreadsCache,
  useInfiniteThreads,
  useThreads,
} from "@/core/threads/thread-lists";
import {
  fetchAllThreadRuns,
  THREAD_RUNS_MAX_OFFSET,
  THREAD_RUNS_MAX_PAGES,
  THREAD_RUNS_PAGE_SIZE,
  useThreadRuns,
} from "@/core/threads/thread-runs";
import { useThreadHistory } from "@/core/threads/use-thread-history";
import {
  useProjectedThreadMessages,
  useThreadStream,
} from "@/core/threads/use-thread-stream";

const compatibilityExports = {
  attachRunIdToNewMessages,
  buildThreadSubmitCheckpointOptions,
  buildThreadSubmitMessages,
  buildVisibleHistoryMessages,
  canStartPreparedReplay,
  classifyPreparedReplaySdkError,
  computeSummarizationMovedMessages,
  countHumanMessagesExcludingSuperseded,
  deleteThreadEverywhere,
  fetchAllThreadRuns,
  fetchInfiniteThreadsPage,
  filterInfiniteThreadsCache,
  filterMessagesBySupersededRunIds,
  findSidecarThreadIdsForParent,
  findLatestUnloadedRunIndex,
  getInfiniteThreadsNextPageParam,
  getLatestRegenerationTarget,
  getMessageMasksAfterPreparedReplayFailure,
  getNextRunMessagesBeforeSeq,
  getOldestRunMessageSeq,
  getPreparedReplayStopRollback,
  getRunMasksAfterPreparedReplayFailure,
  getSummarizationMiddlewareMessages,
  getSupersededRunIds,
  getVisibleOptimisticMessages,
  INFINITE_THREADS_PAGE_SIZE,
  INFINITE_THREADS_QUERY_KEY_PREFIX,
  invalidateStoppedThreadCaches,
  latestRunFailureCode,
  latestRunHasTerminalFailure,
  MAX_CONSECUTIVE_EMPTY_RUN_LOADS,
  mergeMessages,
  mergeRunMessageRows,
  mapInfiniteThreadsCache,
  overlayThreadProjection,
  projectThreadMessages,
  pruneConfirmedArchivedMessages,
  rememberActiveRun,
  removeSetItems,
  resolveActiveRunIdForMessages,
  resolvePreservedHistory,
  resolveRunFailureCode,
  resolveRunFailureRunId,
  runMessagesPageHasMore,
  shouldAutoContinueOnEmptyRun,
  STOP_THREAD_FINALIZATION_REFETCH_DELAY_MS,
  stopThreadAndInvalidateCaches,
  THREAD_RUNS_MAX_OFFSET,
  THREAD_RUNS_MAX_PAGES,
  THREAD_RUNS_PAGE_SIZE,
  upsertThreadInInfiniteCache,
  upsertThreadInSearchCache,
  uploadedFileInfoToMessage,
  useBranchThread,
  useDeleteThread,
  useInfiniteThreads,
  useProjectedThreadMessages,
  useRenameThread,
  useRunDetail,
  useThreadContextUsage,
  useThreadHistory,
  useThreadMetadata,
  useThreadRuns,
  useThreadStream,
  useThreadTokenUsage,
  useThreads,
};

describe("thread helper module compatibility", () => {
  test("keeps hooks exports identical to their split module authorities", () => {
    for (const [name, value] of Object.entries(compatibilityExports)) {
      expect(Reflect.get(hooks, name)).toBe(value);
    }
  });
});
