import type { AIMessage, Message, Run } from "@langchain/langgraph-sdk";
import { useStream } from "@langchain/langgraph-sdk/react";
import { type InfiniteData, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import type { PromptInputMessage } from "@/components/ai-elements/prompt-input";
import { useModels } from "@/core/models/hooks";
import {
  buildOutputLimitRetryProfile,
  collectRunExecutionProfiles,
  type RunExecutionProfileRequest,
  withRunExecutionProfileContext,
} from "@/core/private-work/execution-profile";
import {
  buildRunExecutionProfileRequest,
  resolveAgentExecutionModelSelection,
} from "@/core/threads/agent-mode";

import { fetch } from "../api/fetcher";
import { useI18n } from "../i18n/hooks";
import type { FileInMessage } from "../messages/utils";
import {
  isModelOutputLimitError,
  isOutputDeliveryIncompleteError,
  isProjectRunTerminalFailure,
} from "../private-work/api-client";
import { usePrivateWorkAccess } from "../private-work/provider";
import {
  runPrivateWorkAbortable,
  type ProjectPrivateWorkScope,
} from "../private-work/types";
import type { LocalSettings } from "../settings";
import { useUpdateSubtask } from "../tasks/context";
import { taskEventToSubtaskUpdate } from "../tasks/lifecycle";
import { messageToStep } from "../tasks/steps";
import { parseSubtaskTerminalEvent } from "../tasks/subtask-result";
import type { UploadedFileInfo } from "../uploads";
import {
  promptInputFilePartToFile,
  uploadFailureMessage,
  uploadFiles,
} from "../uploads";

import { useCoalescedStreamMessages } from "./coalesce";
import {
  attachRunIdToNewMessages,
  computeSummarizationMovedMessages,
  countHumanMessagesExcludingSuperseded,
  dedupeMessagesByIdentity,
  getMessagesAfterBaseline,
  getSummarizationMiddlewareMessages,
  getVisibleOptimisticMessages,
  hasAcknowledgedOptimisticHuman,
  messageIdentity,
  overlayThreadProjection,
  projectThreadMessages,
  pruneConfirmedArchivedMessages,
  resolveActiveRunIdForMessages,
  type ThreadMessageProjectionInput,
} from "./message-projection";
import {
  canStartPreparedReplay,
  classifyPreparedReplaySdkError,
  getMessageMasksAfterPreparedReplayFailure,
  getPreparedReplayStopRollback,
  getRunMasksAfterPreparedReplayFailure,
  type PendingPreparedReplayMask,
  type PreparedReplayAttempt,
  removeSetItems,
} from "./prepared-replay";
import {
  latestRunHasTerminalFailure,
  rememberActiveRun,
  resolveRunFailureCode,
  resolveRunFailureRunId,
} from "./run-history";
import {
  buildThreadSubmitCheckpointOptions,
  buildThreadSubmitMessages,
  type SendMessageOptions,
  uploadedFileInfoToMessage,
} from "./send-message";
import {
  buildRootThreadStreamOptions,
  createDeferredThreadStreamDetach,
  isRootStreamCallback,
} from "./stream-events";
import {
  invalidateStoppedThreadCaches,
  stopThreadAndInvalidateCaches,
  upsertThreadInInfiniteCache,
  upsertThreadInSearchCache,
} from "./thread-cache";
import {
  INFINITE_THREADS_QUERY_KEY_PREFIX,
  mapInfiniteThreadsCache,
} from "./thread-lists";
import { scopedThreadQueryKey } from "./thread-query-key";
import { threadTokenUsageQueryKey } from "./token-usage";
import type { AgentThread, AgentThreadState } from "./types";
import { useThreadHistory } from "./use-thread-history";

export type ToolEndEvent = {
  name: string;
  data: unknown;
};

export type ThreadStreamOptions = {
  threadId?: string | null | undefined;
  displayThreadId?: string | null | undefined;
  context: LocalSettings["context"];
  agentModelRef?: string | null;
  isMock?: boolean;
  privateWork?: ProjectPrivateWorkScope;
  onSend?: (threadId: string) => void;
  onStart?: (threadId: string, runId: string) => void;
  onFinish?: (state: AgentThreadState) => void;
  onToolEnd?: (event: ToolEndEvent) => void;
};

type RegeneratePrepareResponse = {
  input: Partial<AgentThreadState>;
  checkpoint: {
    checkpoint_ns: string;
    checkpoint_id: string;
    checkpoint_map: Record<string, unknown> | null;
  };
  metadata: Record<string, unknown>;
  target_run_id: string;
};

type EditRegeneratePrepareResponse = RegeneratePrepareResponse & {
  replacement_human_message_id: string;
  source_message_ids: string[];
};

const EMPTY_THREAD_VALUES: AgentThreadState = {
  title: "",
  messages: [],
  artifacts: [],
  todos: [],
};
const EMPTY_MESSAGES: Message[] = [];

export function useProjectedThreadMessages(
  input: ThreadMessageProjectionInput,
): Message[] {
  const {
    threadId,
    visibleHistory,
    pendingArchivedMessages,
    pendingArchiveThreadId,
    renderMessages,
    activeRunId,
    runBaselineMessageIds,
    pendingSupersededRunIds,
    visibleOptimisticMessages,
    historyRuns,
  } = input;
  return useMemo(
    () =>
      projectThreadMessages({
        threadId,
        visibleHistory,
        pendingArchivedMessages,
        pendingArchiveThreadId,
        renderMessages,
        activeRunId,
        runBaselineMessageIds,
        pendingSupersededRunIds,
        visibleOptimisticMessages,
        historyRuns,
      }),
    [
      activeRunId,
      historyRuns,
      pendingArchivedMessages,
      pendingArchiveThreadId,
      pendingSupersededRunIds,
      renderMessages,
      runBaselineMessageIds,
      threadId,
      visibleHistory,
      visibleOptimisticMessages,
    ],
  );
}

function getStreamErrorMessage(error: unknown): string {
  if (typeof error === "string" && error.trim()) {
    return error;
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  if (typeof error === "object" && error !== null) {
    const message = Reflect.get(error, "message");
    if (typeof message === "string" && message.trim()) {
      return message;
    }
    const nestedError = Reflect.get(error, "error");
    if (nestedError instanceof Error && nestedError.message.trim()) {
      return nestedError.message;
    }
    if (typeof nestedError === "string" && nestedError.trim()) {
      return nestedError;
    }
  }
  return "Request failed.";
}

async function readResponseErrorMessage(
  response: Response,
  fallback = "Request failed.",
) {
  try {
    const data = await response.json();
    if (typeof data?.detail === "string" && data.detail.trim()) {
      return data.detail;
    }
  } catch {
    // Use the fallback below when the response body is not JSON.
  }
  return response.statusText || fallback;
}

export function useThreadStream({
  threadId,
  displayThreadId,
  context,
  agentModelRef,
  isMock,
  privateWork: explicitPrivateWork,
  onSend,
  onStart,
  onFinish,
  onToolEnd,
}: ThreadStreamOptions) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const { t } = useI18n();
  const { models: executionModels } = useModels({ enabled: !isMock });
  const executionModelSelection = useMemo(
    () =>
      resolveAgentExecutionModelSelection(
        executionModels,
        context.model_name,
        agentModelRef,
        context.model_selection_explicit === true,
      ),
    [
      agentModelRef,
      context.model_name,
      context.model_selection_explicit,
      executionModels,
    ],
  );
  const executionModel = executionModelSelection.model;
  const executionProfile = useMemo<RunExecutionProfileRequest>(
    () =>
      buildRunExecutionProfileRequest({
        mode: context.mode,
        modeSelectionExplicit: context.mode_selection_explicit === true,
        modelName: context.model_name,
        modelSelectionExplicit: context.model_selection_explicit === true,
        agentModelRef,
        model: executionModel,
      }),
    [
      context.mode,
      context.mode_selection_explicit,
      context.model_name,
      context.model_selection_explicit,
      agentModelRef,
      executionModel,
    ],
  );
  const currentViewThreadId = displayThreadId ?? threadId ?? null;
  const currentViewThreadIdRef = useRef(currentViewThreadId);
  currentViewThreadIdRef.current = currentViewThreadId;
  // Optimistic messages shown before the server stream responds.
  const [optimisticMessages, setOptimisticMessages] = useState<Message[]>([]);
  const [optimisticThreadId, setOptimisticThreadId] = useState<string | null>(
    null,
  );
  const [liveMessagesThreadId, setLiveMessagesThreadId] = useState<
    string | null
  >(null);
  const [pendingSupersededRunIds, setPendingSupersededRunIds] = useState<
    ReadonlySet<string>
  >(() => new Set());
  const [pendingSupersededMessageIds, setPendingSupersededMessageIds] =
    useState<ReadonlySet<string>>(() => new Set());
  const [isUploading, setIsUploading] = useState(false);
  // Track the thread ID that is currently streaming to handle thread changes during streaming
  const [onStreamThreadId, setOnStreamThreadId] = useState(() => threadId);
  // Ref to track current thread ID across async callbacks without causing re-renders,
  // and to allow access to the current thread id in onUpdateEvent
  const threadIdRef = useRef<string | null>(threadId ?? null);
  const messagesRef = useRef<Message[]>([]);
  const currentRunIdRef = useRef<string | null>(null);
  const currentRunThreadIdRef = useRef<string | null>(null);
  const currentRunBaselineMessageIdsRef = useRef<Set<string>>(new Set());
  const runBaselinePreparedRef = useRef(false);
  const startedRef = useRef(false);
  const pendingUsageBaselineMessageIdsRef = useRef<Set<string>>(new Set());
  const preparedReplayAttemptRef = useRef<PreparedReplayAttempt | null>(null);
  const attachedContinuationRunIdsRef = useRef<Set<string>>(new Set());
  const [ignoredReplayHistoryError, setIgnoredReplayHistoryError] = useState<{
    error: unknown;
  } | null>(null);
  const listeners = useRef({
    onSend,
    onStart,
    onFinish,
    onToolEnd,
  });

  const {
    messages: history,
    runs: historyRuns,
    hasMore: hasMoreHistory,
    loadMore: loadMoreHistory,
    loading: isHistoryLoading,
    error: historyError,
    retry: retryHistory,
    appendMessages,
  } = useThreadHistory(onStreamThreadId ?? "", {
    enabled: !isMock,
    pendingSupersededRunIds,
    privateWork,
  });
  const runExecutionProfiles = useMemo(
    () => collectRunExecutionProfiles(historyRuns ?? []),
    [historyRuns],
  );

  // Keep listeners ref updated with latest callbacks
  useEffect(() => {
    listeners.current = { onSend, onStart, onFinish, onToolEnd };
  }, [onSend, onStart, onFinish, onToolEnd]);

  useEffect(() => {
    const normalizedThreadId = threadId ?? null;
    if (!normalizedThreadId) {
      // Reset when the UI moves back to a brand new unsaved thread.
      startedRef.current = false;
      setOnStreamThreadId(normalizedThreadId);
    } else {
      setOnStreamThreadId(normalizedThreadId);
    }
    threadIdRef.current = normalizedThreadId;
  }, [threadId]);

  const handleStreamStart = useCallback((_threadId: string, _runId: string) => {
    threadIdRef.current = _threadId;
    if (!runBaselinePreparedRef.current) {
      currentRunBaselineMessageIdsRef.current = new Set(
        messagesRef.current
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
    }
    currentRunIdRef.current = _runId;
    currentRunThreadIdRef.current = _threadId;
    runBaselinePreparedRef.current = false;
    setOptimisticThreadId((currentOptimisticThreadId) => {
      const currentView = currentViewThreadIdRef.current;
      if (
        currentOptimisticThreadId &&
        (currentOptimisticThreadId === currentView ||
          currentOptimisticThreadId === _threadId)
      ) {
        return _threadId;
      }
      return currentOptimisticThreadId;
    });
    setLiveMessagesThreadId((currentLiveMessagesThreadId) => {
      const currentView = currentViewThreadIdRef.current;
      if (
        currentLiveMessagesThreadId &&
        (currentLiveMessagesThreadId === currentView ||
          currentLiveMessagesThreadId === _threadId)
      ) {
        return _threadId;
      }
      return currentLiveMessagesThreadId;
    });
    if (!startedRef.current) {
      listeners.current.onStart?.(_threadId, _runId);
      startedRef.current = true;
    }
    setOnStreamThreadId(_threadId);
  }, []);

  const queryClient = useQueryClient();
  const updateSubtask = useUpdateSubtask();
  const clearPreparedReplayMasks = useCallback(
    (replay: PendingPreparedReplayMask | null) => {
      if (!replay) {
        return;
      }
      setPendingSupersededRunIds((current) =>
        removeSetItems(current, [replay.targetRunId]),
      );
      setPendingSupersededMessageIds((current) =>
        removeSetItems(current, replay.supersededMessageIds),
      );
    },
    [],
  );
  const rollbackPreparedReplayFailure = useCallback(
    (replay: PendingPreparedReplayMask | null, failedRunId?: string | null) => {
      if (!replay) {
        return;
      }
      setPendingSupersededRunIds((current) =>
        getRunMasksAfterPreparedReplayFailure(current, replay, failedRunId),
      );
      setPendingSupersededMessageIds((current) =>
        getMessageMasksAfterPreparedReplayFailure(current, replay),
      );
    },
    [],
  );

  const thread = useStream<AgentThreadState>({
    client: privateWork.client,
    assistantId: "lead_agent",
    threadId: onStreamThreadId,
    reconnectOnMount: privateWork.reconnectOnMount,
    fetchStateHistory: { limit: 1 },
    throttle: true,
    onCreated(meta) {
      const replayAttempt = preparedReplayAttemptRef.current;
      if (
        replayAttempt?.status === "submitting" &&
        replayAttempt.threadId === meta.thread_id
      ) {
        replayAttempt.createdRunId = meta.run_id;
      }
      setIgnoredReplayHistoryError(null);
      handleStreamStart(meta.thread_id, meta.run_id);
      const now = new Date().toISOString();
      const runQueryKey = scopedThreadQueryKey(
        privateWork.scope,
        "thread",
        meta.thread_id,
      );
      queryClient.setQueryData<Run[]>(runQueryKey, (runs) =>
        rememberActiveRun(runs, {
          threadId: meta.thread_id,
          runId: meta.run_id,
          createdAt: now,
        }),
      );
      void queryClient.invalidateQueries({
        queryKey: runQueryKey,
        exact: true,
      });
      upsertThreadInSearchCache(
        queryClient,
        {
          thread_id: meta.thread_id,
          created_at: now,
          updated_at: now,
          state_updated_at: now,
          metadata: context.agent_name
            ? { agent_name: context.agent_name }
            : {},
          status: "busy",
          values: {
            title: t.pages.newChat,
            messages: [],
            artifacts: [],
          },
          interrupts: {},
        },
        privateWork.scope,
      );
      upsertThreadInInfiniteCache(
        queryClient,
        {
          thread_id: meta.thread_id,
          created_at: now,
          updated_at: now,
          state_updated_at: now,
          metadata: context.agent_name
            ? { agent_name: context.agent_name }
            : {},
          status: "busy",
          values: {
            title: t.pages.newChat,
            messages: [],
            artifacts: [],
          },
          interrupts: {},
        },
        privateWork.scope,
      );
      if (context.agent_name && !isMock) {
        void privateWork.client.threads
          .update(meta.thread_id, {
            metadata: { agent_name: context.agent_name },
          })
          .catch(() => ({}));
      }
    },
    onLangChainEvent(event) {
      if (event.event === "on_tool_end") {
        listeners.current.onToolEnd?.({
          name: event.name,
          data: event.data,
        });
      }
    },
    onUpdateEvent(data) {
      const _messages = getSummarizationMiddlewareMessages(data);
      if (_messages && _messages.length >= 2) {
        for (const m of _messages) {
          // Backward-compat shim: pre-PR2 threads may still carry a synthetic
          // HumanMessage(name="summary") from the old summarization path. New
          // threads keep the summary in ThreadState.summary_text instead.
          if (m.name === "summary" && m.type === "human") {
            summarizedRef.current?.add(m.id ?? "");
          }
        }
        const _movedMessages = attachRunIdToNewMessages(
          computeSummarizationMovedMessages(
            messagesRef.current,
            _messages,
            summarizedRef.current ?? new Set<string>(),
          ),
          currentRunIdRef.current,
          currentRunBaselineMessageIdsRef.current,
        );
        // Buffer the rescued messages synchronously so the merge can keep
        // displaying them immediately, even though appendMessages below only
        // updates the archived-history state asynchronously (#3825).
        pendingArchivedMessagesRef.current = dedupeMessagesByIdentity([
          ...pendingArchivedMessagesRef.current,
          ..._movedMessages,
        ]);
        pendingArchiveThreadIdRef.current = threadIdRef.current;
        appendMessages(_movedMessages);
        messagesRef.current = [];
      }

      const updates: Array<Partial<AgentThreadState> | null> = Object.values(
        data || {},
      );
      for (const update of updates) {
        if (update && "title" in update && update.title) {
          void queryClient.setQueriesData(
            {
              queryKey: scopedThreadQueryKey(
                privateWork.scope,
                "threads",
                "search",
              ),
              exact: false,
            },
            (oldData: Array<AgentThread> | undefined) => {
              return oldData?.map((t) => {
                if (t.thread_id === threadIdRef.current) {
                  return {
                    ...t,
                    values: {
                      ...t.values,
                      title: update.title,
                    },
                  };
                }
                return t;
              });
            },
          );
          const nextTitle: string = update.title;
          void queryClient.setQueriesData(
            {
              queryKey: scopedThreadQueryKey(
                privateWork.scope,
                ...INFINITE_THREADS_QUERY_KEY_PREFIX,
              ),
              exact: false,
            },
            (oldData: InfiniteData<AgentThread[]> | undefined) =>
              mapInfiniteThreadsCache(
                oldData,
                (t): AgentThread =>
                  t.thread_id === threadIdRef.current
                    ? {
                        ...t,
                        values: {
                          ...t.values,
                          title: nextTitle,
                        },
                      }
                    : t,
              ),
          );
        }
      }
    },
    onCustomEvent(event: unknown, callbackOptions) {
      if (!isRootStreamCallback(callbackOptions)) return;

      const terminalUpdate = parseSubtaskTerminalEvent(event);
      if (terminalUpdate) {
        updateSubtask({
          ...terminalUpdate,
          statusSource: "custom_event",
          ...(terminalUpdate.status === "failed" && !terminalUpdate.error
            ? { error: t.subtasks.failed }
            : {}),
        });
        return;
      }

      const lifecycleUpdate = taskEventToSubtaskUpdate(event);
      if (
        typeof event === "object" &&
        event !== null &&
        "type" in event &&
        event.type === "task_running"
      ) {
        const e = event as {
          type: "task_running";
          task_id: string;
          message: AIMessage;
          message_index?: number;
        };
        // Accumulate the full step history instead of overwriting (#3779): keep
        // latestMessage for the collapsed-header tool-call hint, and append the
        // normalized step (assistant turn or tool output) to the timeline.
        updateSubtask({
          ...(lifecycleUpdate ?? {}),
          id: e.task_id,
          status: "in_progress",
          statusSource: "custom_event",
          latestMessage: e.message,
          steps: [messageToStep(e.message, e.message_index ?? 0)],
        });
        return;
      }
      if (lifecycleUpdate) {
        updateSubtask(lifecycleUpdate);
        return;
      }

      if (
        typeof event === "object" &&
        event !== null &&
        "type" in event &&
        event.type === "llm_retry" &&
        "message" in event &&
        typeof event.message === "string" &&
        event.message.trim()
      ) {
        const e = event as { type: "llm_retry"; message: string };
        toast(e.message);
      }
    },
    onError(error, callbackOptions) {
      const replayAttempt = preparedReplayAttemptRef.current;
      const decision =
        replayAttempt?.status === "submitting"
          ? classifyPreparedReplaySdkError({
              createdRunId: replayAttempt.createdRunId,
              callbackRunId: callbackOptions?.run_id ?? null,
              error,
              historyRefetchError: replayAttempt.historyRefetchError,
            })
          : null;
      const leavesReplayProjectionIntact =
        decision?.kind === "history-refetch-failure" ||
        decision?.kind === "ignore-history-refetch-duplicate" ||
        decision?.kind === "ignore-unrelated-run";

      if (decision?.kind === "rollback" && replayAttempt) {
        replayAttempt.status = "failed";
        rollbackPreparedReplayFailure(
          replayAttempt.replay,
          decision.failedRunId,
        );
      } else if (
        decision?.kind === "history-refetch-failure" &&
        replayAttempt
      ) {
        replayAttempt.historyRefetchError = error;
        setIgnoredReplayHistoryError({ error });
      }

      if (!leavesReplayProjectionIntact) {
        setOptimisticMessages([]);
        setOptimisticThreadId(null);
        setLiveMessagesThreadId(null);
      }
      if (decision?.kind !== "ignore-history-refetch-duplicate") {
        toast.error(
          isModelOutputLimitError(error)
            ? t.conversation.modelOutputLimitDescription
            : isOutputDeliveryIncompleteError(error)
              ? t.conversation.outputDeliveryIncompleteDescription
              : isProjectRunTerminalFailure(error)
                ? t.conversation.runFailedDescription
                : getStreamErrorMessage(error),
        );
      }
      pendingUsageBaselineMessageIdsRef.current = new Set(
        messagesRef.current
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      invalidateStoppedThreadCaches(
        queryClient,
        threadIdRef.current,
        isMock,
        privateWork.scope,
      );
    },
    onFinish(state, callbackOptions) {
      const replayAttempt = preparedReplayAttemptRef.current;
      if (
        replayAttempt?.status === "submitting" &&
        replayAttempt.createdRunId === callbackOptions?.run_id
      ) {
        replayAttempt.status = "succeeded";
      }
      setIgnoredReplayHistoryError(null);
      listeners.current.onFinish?.(state.values);
      pendingUsageBaselineMessageIdsRef.current = new Set(
        messagesRef.current
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      invalidateStoppedThreadCaches(
        queryClient,
        threadIdRef.current,
        isMock,
        privateWork.scope,
      );
    },
  });
  const deferredStreamDetachRef = useRef<ReturnType<
    typeof createDeferredThreadStreamDetach
  > | null>(null);
  deferredStreamDetachRef.current ??= createDeferredThreadStreamDetach();
  const deferredStreamDetach = deferredStreamDetachRef.current;
  const switchStreamThread = thread.switchThread;
  useEffect(() => {
    deferredStreamDetach.retain();
    return () => {
      deferredStreamDetach.defer(() => {
        // switchThread clears/aborts the local SDK projection only. Unlike
        // stop(), it never sends a backend Run cancellation.
        switchStreamThread(null);
      });
    };
  }, [deferredStreamDetach, switchStreamThread]);

  const stopThread = useCallback(async () => {
    const stoppedThreadId =
      threadIdRef.current ?? displayThreadId ?? threadId ?? null;
    const replayAttempt = preparedReplayAttemptRef.current;
    const replayRollback = getPreparedReplayStopRollback(replayAttempt);
    await stopThreadAndInvalidateCaches(
      queryClient,
      () => thread.stop(),
      stoppedThreadId,
      isMock,
      privateWork.scope,
    );
    if (replayAttempt && replayRollback) {
      replayAttempt.status = "failed";
      setOptimisticMessages([]);
      setOptimisticThreadId(null);
      rollbackPreparedReplayFailure(
        replayRollback.replay,
        replayRollback.failedRunId,
      );
      if (preparedReplayAttemptRef.current === replayAttempt) {
        preparedReplayAttemptRef.current = null;
      }
    }
  }, [
    displayThreadId,
    isMock,
    privateWork.scope,
    queryClient,
    rollbackPreparedReplayFailure,
    thread,
    threadId,
  ]);

  const attachRun = useCallback(
    async (runId: string) => {
      const selectedRunId = runId.trim();
      const selectedThreadId = threadIdRef.current;
      if (
        !selectedRunId ||
        !selectedThreadId ||
        currentViewThreadIdRef.current !== selectedThreadId ||
        privateWork.isActive?.() === false
      ) {
        return false;
      }

      const attachmentKey = `${selectedThreadId}:${selectedRunId}`;
      if (attachedContinuationRunIdsRef.current.has(attachmentKey)) {
        return true;
      }
      if (thread.isLoading) {
        return currentRunIdRef.current === selectedRunId;
      }

      attachedContinuationRunIdsRef.current.add(attachmentKey);
      setIgnoredReplayHistoryError(null);
      setLiveMessagesThreadId(selectedThreadId);
      handleStreamStart(selectedThreadId, selectedRunId);
      const runQueryKey = scopedThreadQueryKey(
        privateWork.scope,
        "thread",
        selectedThreadId,
      );
      queryClient.setQueryData<Run[]>(runQueryKey, (runs) =>
        rememberActiveRun(runs, {
          threadId: selectedThreadId,
          runId: selectedRunId,
          createdAt: new Date().toISOString(),
        }),
      );
      void queryClient.invalidateQueries({
        queryKey: runQueryKey,
        exact: true,
      });

      try {
        await thread.joinStream(selectedRunId);
        return true;
      } catch {
        attachedContinuationRunIdsRef.current.delete(attachmentKey);
        retryHistory();
        return false;
      }
    },
    [handleStreamStart, privateWork, queryClient, retryHistory, thread],
  );

  const hasVisibleStreamState =
    Boolean(threadId) || liveMessagesThreadId === currentViewThreadId;
  const persistedMessages = useMemo(
    () =>
      hasVisibleStreamState
        ? thread.messages.filter(
            (message) =>
              !message.id || !pendingSupersededMessageIds.has(message.id),
          )
        : [],
    [hasVisibleStreamState, pendingSupersededMessageIds, thread.messages],
  );
  const renderMessages = useCoalescedStreamMessages(
    persistedMessages,
    thread.isLoading,
  );
  const visibleHistory = useMemo(
    () =>
      threadId
        ? history.filter(
            (message) =>
              !message.id || !pendingSupersededMessageIds.has(message.id),
          )
        : [],
    [history, pendingSupersededMessageIds, threadId],
  );
  const humanMessageCount = useMemo(
    () =>
      persistedMessages.filter((message) => message.type === "human").length,
    [persistedMessages],
  );
  const latestMessageCountsRef = useRef({ humanMessageCount });
  const sendInFlightRef = useRef(false);
  // Synchronous bridge for messages rescued from context summarization. The
  // archived-history `setState` (via appendMessages) lands on a different
  // schedule than the live thread external store, so the merge reads this buffer
  // to avoid dropping rescued messages in the render window before history
  // catches up (#3825).
  const pendingArchivedMessagesRef = useRef<Message[]>([]);
  // The thread the rescue buffer belongs to, captured when onUpdateEvent fills
  // it. The merge only overlays the buffer when this matches the viewed
  // `threadId`, so a previous thread's rescued messages can never flash into
  // another thread or the new-chat screen (#3825).
  const pendingArchiveThreadIdRef = useRef<string | null>(null);
  const summarizedRef = useRef<Set<string>>(null);
  // Track human message count before sending to prevent clearing optimistic
  // messages before the server's human message arrives (e.g. when AI messages
  // from "messages-tuple" events arrive before the input human message from
  // "values" events).
  const prevHumanMsgCountRef = useRef(humanMessageCount);

  latestMessageCountsRef.current = { humanMessageCount };
  summarizedRef.current ??= new Set<string>();

  // Reset thread-local pending UI state when switching between threads so
  // optimistic messages and in-flight guards do not leak across chat views.
  useEffect(() => {
    startedRef.current = false;
    sendInFlightRef.current = false;
    messagesRef.current = [];
    currentRunIdRef.current = null;
    currentRunThreadIdRef.current = null;
    currentRunBaselineMessageIdsRef.current = new Set();
    runBaselinePreparedRef.current = false;
    pendingArchivedMessagesRef.current = [];
    pendingArchiveThreadIdRef.current = null;
    summarizedRef.current = new Set<string>();
    pendingUsageBaselineMessageIdsRef.current = new Set();
    preparedReplayAttemptRef.current = null;
    attachedContinuationRunIdsRef.current = new Set();
    setIgnoredReplayHistoryError(null);
    setPendingSupersededRunIds(new Set());
    setPendingSupersededMessageIds(new Set());
    prevHumanMsgCountRef.current =
      latestMessageCountsRef.current.humanMessageCount;
  }, [threadId]);

  // Release archive-buffer entries once the canonical history state has absorbed
  // them, so the synchronous bridge stays transient and never resurrects a
  // message that history later filters out (e.g. a superseded run) (#3825).
  useEffect(() => {
    pendingArchivedMessagesRef.current = pruneConfirmedArchivedMessages(
      pendingArchivedMessagesRef.current,
      visibleHistory,
    );
  }, [visibleHistory]);

  useEffect(() => {
    if (optimisticThreadId && optimisticThreadId !== currentViewThreadId) {
      setOptimisticMessages([]);
      setOptimisticThreadId(null);
    }
    if (liveMessagesThreadId && liveMessagesThreadId !== currentViewThreadId) {
      setLiveMessagesThreadId(null);
    }
  }, [currentViewThreadId, liveMessagesThreadId, optimisticThreadId]);

  // When streaming starts without a baseline (e.g. reconnection, run started
  // from another client, or page reload mid-stream), snapshot the current
  // messages so only *new* messages are treated as "pending" for token usage.
  useEffect(() => {
    if (
      thread.isLoading &&
      pendingUsageBaselineMessageIdsRef.current.size === 0
    ) {
      pendingUsageBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
    }
  }, [persistedMessages, thread.isLoading]);

  // Clear optimistic when server messages arrive.
  // For messages with a human optimistic message, wait until the server's
  // human message has arrived to avoid clearing before the input message
  // appears in the stream (the input message may arrive via "values" events
  // after individual "messages-tuple" events for AI messages).
  const optimisticMessageCount = optimisticMessages.length;
  const hasHumanOptimistic = useMemo(
    () => optimisticMessages.some((message) => message.type === "human"),
    [optimisticMessages],
  );
  const optimisticHumanAcknowledged = useMemo(
    () =>
      hasAcknowledgedOptimisticHuman(optimisticMessages, [
        ...visibleHistory,
        ...persistedMessages,
      ]),
    [optimisticMessages, persistedMessages, visibleHistory],
  );
  useEffect(() => {
    if (optimisticMessageCount === 0) return;

    const newHumanMsgArrived = humanMessageCount > prevHumanMsgCountRef.current;

    if (
      !hasHumanOptimistic ||
      newHumanMsgArrived ||
      optimisticHumanAcknowledged
    ) {
      setOptimisticMessages([]);
      setOptimisticThreadId(null);
    }
  }, [
    hasHumanOptimistic,
    humanMessageCount,
    optimisticHumanAcknowledged,
    optimisticMessageCount,
  ]);

  const sendMessage = useCallback(
    async (
      threadId: string,
      message: PromptInputMessage,
      extraContext?: Record<string, unknown>,
      options?: SendMessageOptions,
    ) => {
      if (sendInFlightRef.current) {
        return;
      }
      sendInFlightRef.current = true;

      const text = message.text.trim();
      const humanMessageId = `human-${crypto.randomUUID()}`;

      // Capture the current human message count before showing optimistic
      // messages so we can wait for the server's copy of the user input.
      prevHumanMsgCountRef.current = humanMessageCount;
      pendingUsageBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      currentRunBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      runBaselinePreparedRef.current = true;

      // Build optimistic files list with uploading status
      const optimisticFiles: FileInMessage[] = (message.files ?? []).map(
        (f) => ({
          filename: f.filename ?? "",
          size: 0,
          status: "uploading" as const,
        }),
      );

      const hideFromUI = options?.additionalKwargs?.hide_from_ui === true;
      const optimisticAdditionalKwargs = {
        ...options?.additionalKwargs,
        ...(optimisticFiles.length > 0 ? { files: optimisticFiles } : {}),
      };

      const newOptimistic: Message[] = [];
      if (!hideFromUI) {
        newOptimistic.push({
          type: "human",
          id: humanMessageId,
          content: text ? [{ type: "text", text }] : "",
          additional_kwargs: optimisticAdditionalKwargs,
        });
      }

      if (optimisticFiles.length > 0 && !hideFromUI) {
        // Mock AI message while files are being uploaded
        newOptimistic.push({
          type: "ai",
          id: `opt-ai-${Date.now()}`,
          content: t.uploads.uploadingFiles,
          additional_kwargs: { element: "task" },
        });
      }
      setOptimisticThreadId(threadId);
      setLiveMessagesThreadId(threadId);
      setOptimisticMessages(newOptimistic);

      listeners.current.onSend?.(threadId);

      let uploadedFileInfo: UploadedFileInfo[] = [];

      try {
        // Upload files first if any
        if (message.files && message.files.length > 0) {
          setIsUploading(true);
          try {
            const filePromises = message.files.map((fileUIPart) =>
              promptInputFilePartToFile(fileUIPart),
            );

            const conversionResults = await Promise.all(filePromises);
            const files = conversionResults.filter(
              (file): file is File => file !== null,
            );
            const failedConversions = conversionResults.length - files.length;

            if (failedConversions > 0) {
              throw new Error(
                `Failed to prepare ${failedConversions} attachment(s) for upload. Please retry.`,
              );
            }

            if (!threadId) {
              throw new Error("Thread is not ready for file upload.");
            }

            if (files.length > 0) {
              const uploadResponse = await runPrivateWorkAbortable(
                privateWork,
                (signal) => uploadFiles(threadId, files, privateWork, signal),
              );
              uploadedFileInfo = uploadResponse.files;

              // Update optimistic human message with uploaded status + paths
              const uploadedFiles: FileInMessage[] = uploadedFileInfo.map(
                uploadedFileInfoToMessage,
              );
              setOptimisticMessages((messages) => {
                if (messages.length > 1 && messages[0]) {
                  const humanMessage: Message = messages[0];
                  return [
                    {
                      ...humanMessage,
                      additional_kwargs: { files: uploadedFiles },
                    },
                    ...messages.slice(1),
                  ];
                }
                return messages;
              });
            }
          } catch (error) {
            const errorMessage = uploadFailureMessage(error, {
              tooLarge: t.uploads.serverTooLarge,
              storageQuotaExceeded: t.uploads.storageQuotaExceeded,
              preflightRejected: t.uploads.preflightRejected,
              fallback: t.uploads.uploadFailed,
            });
            toast.error(errorMessage);
            setOptimisticMessages([]);
            setOptimisticThreadId(null);
            setLiveMessagesThreadId(null);
            throw error;
          } finally {
            setIsUploading(false);
          }
        }

        // Build files metadata for submission (included in additional_kwargs)
        const filesForSubmit: FileInMessage[] = uploadedFileInfo.map(
          uploadedFileInfoToMessage,
        );

        await thread.submit(
          {
            messages: buildThreadSubmitMessages({
              text,
              messageId: humanMessageId,
              additionalKwargs: options?.additionalKwargs,
              additionalInputMessages: options?.additionalInputMessages,
              filesForSubmit,
            }),
          },
          {
            threadId: threadId,
            ...buildThreadSubmitCheckpointOptions(
              options?.continueFromLatestCheckpoint,
            ),
            ...buildRootThreadStreamOptions(),
            context: withRunExecutionProfileContext(
              {
                ...extraContext,
                ...context,
                thread_id: threadId,
              },
              executionProfile,
            ),
          },
        );
        options?.onSent?.();
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            "threads",
            "search",
          ),
        });
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...INFINITE_THREADS_QUERY_KEY_PREFIX,
          ),
        });
      } catch (error) {
        setOptimisticMessages([]);
        setOptimisticThreadId(null);
        setLiveMessagesThreadId(null);
        setIsUploading(false);
        throw error;
      } finally {
        sendInFlightRef.current = false;
      }
    },
    [
      thread,
      t.uploads.preflightRejected,
      t.uploads.serverTooLarge,
      t.uploads.storageQuotaExceeded,
      t.uploads.uploadFailed,
      t.uploads.uploadingFiles,
      context,
      executionProfile,
      queryClient,
      humanMessageCount,
      persistedMessages,
      privateWork,
    ],
  );

  const submitPreparedReplay = useCallback(
    async <TPrepared extends RegeneratePrepareResponse>({
      threadId,
      prepare,
      getSupersededMessageIds,
      getOptimisticMessages,
      executionProfileOverride,
    }: {
      threadId: string;
      prepare: () => Promise<TPrepared>;
      getSupersededMessageIds: (prepared: TPrepared) => string[];
      getOptimisticMessages?: (prepared: TPrepared) => Message[];
      executionProfileOverride?: RunExecutionProfileRequest;
    }) => {
      // The SDK reports both its initial state-load failure and a replay
      // admission failure through the same metadata-less onError callback.
      // Do not open a replay attribution window until the initial state request
      // has settled, so those two error sources cannot be confused.
      if (
        !canStartPreparedReplay({
          threadId,
          sendInFlight: sendInFlightRef.current,
          isThreadLoading: thread.isThreadLoading,
        })
      ) {
        return false;
      }
      sendInFlightRef.current = true;
      prevHumanMsgCountRef.current = humanMessageCount;
      pendingUsageBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      currentRunBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      runBaselinePreparedRef.current = true;
      setLiveMessagesThreadId(threadId);
      listeners.current.onSend?.(threadId);
      let pendingReplay: PendingPreparedReplayMask | null = null;
      let replayAttempt: PreparedReplayAttempt | null = null;
      setIgnoredReplayHistoryError(null);

      try {
        const prepared = await prepare();
        if (privateWork.isActive && !privateWork.isActive()) {
          throw new Error("The active project changed before replay started.");
        }
        const supersededMessageIds = getSupersededMessageIds(prepared);
        prevHumanMsgCountRef.current = countHumanMessagesExcludingSuperseded(
          persistedMessages,
          supersededMessageIds,
        );
        const replacementHumanMessageId =
          "replacement_human_message_id" in prepared &&
          typeof prepared.replacement_human_message_id === "string"
            ? prepared.replacement_human_message_id
            : undefined;
        pendingReplay = {
          kind: replacementHumanMessageId ? "edit" : "regenerate",
          targetRunId: prepared.target_run_id,
          supersededMessageIds,
          replacementHumanMessageId,
        };
        replayAttempt = {
          replay: pendingReplay,
          threadId,
          createdRunId: null,
          historyRefetchError: null,
          status: "submitting",
        };
        preparedReplayAttemptRef.current = replayAttempt;
        setPendingSupersededRunIds((current) => {
          const next = new Set(current);
          next.add(prepared.target_run_id);
          return next;
        });
        setPendingSupersededMessageIds((current) => {
          const next = new Set(current);
          for (const id of supersededMessageIds) {
            next.add(id);
          }
          return next;
        });

        const nextOptimisticMessages = getOptimisticMessages?.(prepared) ?? [];
        if (nextOptimisticMessages.length > 0) {
          setOptimisticThreadId(threadId);
          setOptimisticMessages(nextOptimisticMessages);
        }

        await thread.submit(prepared.input, {
          threadId,
          checkpoint: prepared.checkpoint,
          metadata: prepared.metadata,
          ...buildRootThreadStreamOptions(),
          context: withRunExecutionProfileContext(
            {
              ...context,
              thread_id: threadId,
            },
            executionProfileOverride ?? executionProfile,
          ),
        });
        if (replayAttempt.status === "failed") {
          if (preparedReplayAttemptRef.current === replayAttempt) {
            preparedReplayAttemptRef.current = null;
          }
          return false;
        }
        replayAttempt.status = "succeeded";
        if (preparedReplayAttemptRef.current === replayAttempt) {
          preparedReplayAttemptRef.current = null;
        }
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(privateWork.scope, "thread", threadId),
        });
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            "threads",
            "search",
          ),
        });
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...INFINITE_THREADS_QUERY_KEY_PREFIX,
          ),
        });
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...threadTokenUsageQueryKey(threadId),
          ),
        });
        return true;
      } catch (error) {
        setOptimisticMessages([]);
        setOptimisticThreadId(null);
        setLiveMessagesThreadId(null);
        runBaselinePreparedRef.current = false;
        if (pendingReplay) {
          clearPreparedReplayMasks(pendingReplay);
        }
        if (replayAttempt) {
          replayAttempt.status = "failed";
        }
        if (preparedReplayAttemptRef.current === replayAttempt) {
          preparedReplayAttemptRef.current = null;
        }
        toast.error(getStreamErrorMessage(error));
        return false;
      } finally {
        sendInFlightRef.current = false;
      }
    },
    [
      clearPreparedReplayMasks,
      context,
      executionProfile,
      humanMessageCount,
      persistedMessages,
      privateWork,
      queryClient,
      thread,
    ],
  );

  const regenerateMessage = useCallback(
    async (
      threadId: string,
      messageId: string,
      supersededMessageIds: string[] = [messageId],
      options?: { withoutThinking?: boolean },
    ) => {
      if (!messageId) {
        return false;
      }
      return submitPreparedReplay({
        threadId,
        prepare: () =>
          runPrivateWorkAbortable(privateWork, async (signal) => {
            const response = await fetch(
              `${privateWork.apiBaseURL}/threads/${encodeURIComponent(
                threadId,
              )}/runs/regenerate/prepare`,
              {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                },
                credentials: "include",
                body: JSON.stringify({ message_id: messageId }),
                signal,
              },
            );
            if (!response.ok) {
              throw new Error(await readResponseErrorMessage(response));
            }
            return (await response.json()) as RegeneratePrepareResponse;
          }),
        getSupersededMessageIds: () => supersededMessageIds,
        executionProfileOverride: options?.withoutThinking
          ? buildOutputLimitRetryProfile(executionProfile)
          : undefined,
      });
    },
    [executionProfile, privateWork, submitPreparedReplay],
  );

  const editAndRegenerateMessage = useCallback(
    async (
      threadId: string,
      humanMessageId: string,
      replacementText: string,
    ) => {
      if (!humanMessageId) {
        return false;
      }
      return submitPreparedReplay<EditRegeneratePrepareResponse>({
        threadId,
        prepare: () =>
          runPrivateWorkAbortable(privateWork, async (signal) => {
            const response = await fetch(
              `${privateWork.apiBaseURL}/threads/${encodeURIComponent(
                threadId,
              )}/runs/edit-regenerate/prepare`,
              {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                },
                credentials: "include",
                body: JSON.stringify({
                  human_message_id: humanMessageId,
                  replacement_text: replacementText,
                }),
                signal,
              },
            );
            if (!response.ok) {
              throw new Error(await readResponseErrorMessage(response));
            }
            return (await response.json()) as EditRegeneratePrepareResponse;
          }),
        getSupersededMessageIds: (prepared) => prepared.source_message_ids,
        getOptimisticMessages: (prepared) => prepared.input.messages ?? [],
      });
    },
    [privateWork, submitPreparedReplay],
  );

  // Cache the latest thread messages in a ref to compare against incoming history messages for deduplication,
  // and to allow access to the full message list in onUpdateEvent without causing re-renders.
  if (persistedMessages.length >= messagesRef.current.length) {
    messagesRef.current = persistedMessages;
  }
  const previousHumanMessageCount = prevHumanMsgCountRef.current;
  const visibleOptimisticMessages = useMemo(
    () =>
      getVisibleOptimisticMessages(
        optimisticThreadId === currentViewThreadId
          ? optimisticMessages
          : EMPTY_MESSAGES,
        previousHumanMessageCount,
        humanMessageCount,
      ),
    [
      currentViewThreadId,
      humanMessageCount,
      optimisticMessages,
      optimisticThreadId,
      previousHumanMessageCount,
    ],
  );

  const explicitActiveRunId =
    currentRunThreadIdRef.current === threadId ? currentRunIdRef.current : null;
  const activeRunId = useMemo(
    () =>
      resolveActiveRunIdForMessages(
        persistedMessages,
        thread.isLoading,
        explicitActiveRunId,
      ),
    [explicitActiveRunId, persistedMessages, thread.isLoading],
  );
  // The three refs below are replaced, never mutated in place. Their identities
  // therefore form safe semantic dependencies for the projection memo.
  const pendingArchivedMessages = pendingArchivedMessagesRef.current;
  const pendingArchiveThreadId = pendingArchiveThreadIdRef.current;
  const runBaselineMessageIds = currentRunBaselineMessageIdsRef.current;
  const mergedMessages = useProjectedThreadMessages({
    threadId,
    visibleHistory,
    pendingArchivedMessages,
    pendingArchiveThreadId,
    renderMessages,
    activeRunId,
    runBaselineMessageIds,
    pendingSupersededRunIds,
    visibleOptimisticMessages,
    historyRuns,
  });
  const pendingUsageBaselineMessageIds =
    pendingUsageBaselineMessageIdsRef.current;
  const pendingUsageMessages = useMemo(
    () =>
      thread.isLoading
        ? getMessagesAfterBaseline(
            persistedMessages,
            pendingUsageBaselineMessageIds,
          )
        : EMPTY_MESSAGES,
    [pendingUsageBaselineMessageIds, persistedMessages, thread.isLoading],
  );
  const projectedThreadError =
    ignoredReplayHistoryError !== null &&
    Object.is(thread.error, ignoredReplayHistoryError.error)
      ? undefined
      : thread.error;

  // Merge history, live stream, and optimistic messages for display
  // History messages may overlap with thread.messages; thread.messages take precedence
  const mergedThread = overlayThreadProjection(thread, {
    error: projectedThreadError,
    stop: stopThread,
    values: hasVisibleStreamState ? thread.values : EMPTY_THREAD_VALUES,
    messages: mergedMessages,
  });
  const runFailureCode = resolveRunFailureCode(
    projectedThreadError,
    historyRuns,
  );
  const runFailureRunId = resolveRunFailureRunId(
    projectedThreadError,
    explicitActiveRunId,
    historyRuns,
  );

  return {
    thread: mergedThread,
    boundThreadId: onStreamThreadId ?? null,
    pendingUsageMessages,
    attachRun,
    sendMessage,
    regenerateMessage,
    editAndRegenerateMessage,
    isUploading,
    isHistoryLoading,
    hasMoreHistory,
    loadMoreHistory,
    historyError,
    retryHistory,
    runExecutionProfiles,
    hasTerminalRunFailure: latestRunHasTerminalFailure(historyRuns),
    runFailureCode,
    runFailureRunId,
  } as const;
}
