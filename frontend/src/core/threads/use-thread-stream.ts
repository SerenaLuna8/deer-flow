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
  DEFAULT_RUN_WORKLOAD_PROFILE,
  resolveDisplayedRunWorkloadProfile,
  withRunWorkloadProfileContext,
} from "@/core/private-work/workload-profile";
import {
  buildRunExecutionProfileRequest,
  resolveAgentExecutionModelSelection,
} from "@/core/threads/agent-mode";

import { fetch } from "../api/fetcher";
import { useI18n } from "../i18n/hooks";
import type { FileInMessage } from "../messages/utils";
import {
  isProjectAgentArchivedError,
  isProjectRunTerminalFailure,
  projectRunFailureCode,
  projectRunTerminalFailureEventToError,
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
import type {
  AttachmentUploadCandidate,
  AttachmentUploadStatus,
  PromptInputFilePart,
  UploadedFileInfo,
} from "../uploads";
import {
  AttachmentUploadCoordinator,
  deleteUploadedFile,
  isReadyPromptInputFilePart,
  promptInputFilePartToFile,
  readyPromptInputFileToMessage,
  uploadFailureMessage,
  uploadFiles,
} from "../uploads";

import { useCoalescedStreamMessages } from "./coalesce";
import {
  attachRunIdToNewMessages,
  captureTerminalRunMessages,
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
  retainOptimisticHumanMessagesAfterFailure,
  retainUnacknowledgedOptimisticHumanMessages,
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
import { resolveRunFailureCopy } from "./run-failure-presentation";
import {
  latestRunHasTerminalFailure,
  rememberActiveRun,
  resolveRunFailureCode,
  resolveRunFailureRunId,
} from "./run-history";
import {
  admitRunAndNotify,
  buildThreadSubmitCheckpointOptions,
  buildThreadSubmitMessages,
  createMessageSendAttempt,
  createRunAdmissionLatch,
  isCurrentMessageSendAttempt,
  isRunAdmissionNotConfirmedError,
  isCurrentThreadCallback,
  monitorRunAdmissionLifecycle,
  shouldIgnoreMetadataLessStreamError,
  shouldIgnoreAttributedThreadCallback,
  staleMessageSendError,
  type MessageSendAttempt,
  type RunAdmissionLatch,
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
import {
  emptyRunControlProgress,
  fetchRunControlObservations,
  mergeRunControlObservations,
  parseRunControlLiveEvent,
} from "./tool-call-control-events";
import type { AgentThread, AgentThreadState } from "./types";
import { useThreadHistory } from "./use-thread-history";

export type ToolEndEvent = {
  name: string;
  data: unknown;
};

type ThreadStreamCallbackMetadata = {
  thread_id?: string;
  run_id?: string;
};

export type ThreadStreamOptions = {
  threadId?: string | null | undefined;
  displayThreadId?: string | null | undefined;
  context: LocalSettings["context"];
  agentModelRef?: string | null;
  enabled?: boolean;
  isMock?: boolean;
  privateWork?: ProjectPrivateWorkScope;
  onSend?: (threadId: string) => void;
  onStart?: (threadId: string, runId: string) => void;
  onFinish?: (state: AgentThreadState) => void;
  onToolEnd?: (event: ToolEndEvent) => void;
};

type PendingMessageAdmission = {
  threadId: string;
  uploadStatusScopeKey: string;
  attempt: MessageSendAttempt;
  serverAdmission: RunAdmissionLatch;
  composerAdmission: RunAdmissionLatch;
  optimisticMessages: Message[];
  uploadedClientIds: string[];
  onSent?: () => void;
};

type AttachmentUploadClaim = {
  threadId: string;
  coordinatorScopeKey: string;
  clientIds: string[];
  cleanup: (uploaded: UploadedFileInfo) => void;
};

function attachmentUploadCoordinatorScopeKey(
  accountId: string,
  projectId: string,
  threadId: string,
): string {
  return JSON.stringify([accountId, projectId, threadId]);
}

async function preparePromptAttachments(
  fileParts: readonly PromptInputFilePart[],
  fallbackClientId: (index: number) => string,
): Promise<AttachmentUploadCandidate[]> {
  const prepared = await Promise.all(
    fileParts.map(async (filePart, index) => ({
      clientId: filePart.clientId ?? fallbackClientId(index),
      file: await promptInputFilePartToFile(filePart),
    })),
  );
  const failedConversions = prepared.filter(({ file }) => file === null).length;
  if (failedConversions > 0) {
    throw new Error(
      `Failed to prepare ${failedConversions} attachment(s) for upload. Please retry.`,
    );
  }
  return prepared as AttachmentUploadCandidate[];
}

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
  enabled = true,
  isMock,
  privateWork: explicitPrivateWork,
  onSend,
  onStart,
  onFinish,
  onToolEnd,
}: ThreadStreamOptions) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const streamEnabled = enabled && !isMock;
  const uploadScopeKey = `${privateWork.scope.accountId}:${privateWork.scope.projectId}`;
  const { t } = useI18n();
  const { models: executionModels } = useModels({ enabled: streamEnabled });
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
  const currentUploadStatusScopeKeyRef = useRef(uploadScopeKey);
  currentUploadStatusScopeKeyRef.current = uploadScopeKey;
  // Optimistic messages shown before the server stream responds.
  const [optimisticMessages, setOptimisticMessages] = useState<Message[]>([]);
  const optimisticMessagesRef = useRef<Message[]>(optimisticMessages);
  optimisticMessagesRef.current = optimisticMessages;
  const [optimisticThreadId, setOptimisticThreadId] = useState<string | null>(
    null,
  );
  const optimisticRunIdRef = useRef<string | null>(null);
  const [failedOptimisticMessages, setFailedOptimisticMessages] = useState<
    Message[]
  >([]);
  const [failedOptimisticThreadId, setFailedOptimisticThreadId] = useState<
    string | null
  >(null);
  const [liveMessagesThreadId, setLiveMessagesThreadId] = useState<
    string | null
  >(null);
  const [pendingSupersededRunIds, setPendingSupersededRunIds] = useState<
    ReadonlySet<string>
  >(() => new Set());
  const [pendingSupersededMessageIds, setPendingSupersededMessageIds] =
    useState<ReadonlySet<string>>(() => new Set());
  const [isUploading, setIsUploading] = useState(false);
  const [attachmentUploadStatusState, setAttachmentUploadStatusState] =
    useState<
      Record<
        string,
        {
          scopeKey: string;
          threadId: string;
          status: AttachmentUploadStatus;
        }
      >
    >({});
  // Track the thread ID that is currently streaming to handle thread changes during streaming
  const [onStreamThreadId, setOnStreamThreadId] = useState(() => threadId);
  // Ref to track current thread ID across async callbacks without causing re-renders,
  // and to allow access to the current thread id in onUpdateEvent
  const threadIdRef = useRef<string | null>(threadId ?? null);
  const messagesRef = useRef<Message[]>([]);
  const currentRunIdRef = useRef<string | null>(null);
  const currentRunThreadIdRef = useRef<string | null>(null);
  const [runControlProgress, setRunControlProgress] = useState(
    emptyRunControlProgress,
  );
  const runControlReplayGenerationRef = useRef(0);
  const expectedTerminalFailureKeysRef = useRef<Set<string>>(new Set());
  const currentRunBaselineMessageIdsRef = useRef<Set<string>>(new Set());
  const runBaselinePreparedRef = useRef(false);
  const startedRef = useRef(false);
  const pendingUsageBaselineMessageIdsRef = useRef<Set<string>>(new Set());
  const preparedReplayAttemptRef = useRef<PreparedReplayAttempt | null>(null);
  const messageSendGenerationRef = useRef(0);
  const activeMessageSendRef = useRef<MessageSendAttempt | null>(null);
  const [attachmentUploadCoordinator] = useState(
    () => new AttachmentUploadCoordinator(),
  );
  const cleanupUploadedAttachment = useCallback(
    (targetThreadId: string, uploaded: UploadedFileInfo) => {
      if (!uploaded.id) return;
      void deleteUploadedFile(
        targetThreadId,
        uploaded.id,
        privateWork,
        undefined,
        { onlyIfUnreferenced: true },
      ).catch((error: unknown) => {
        console.error("Failed to clean up an unused attachment upload.", error);
        if (privateWork.isActive?.() ?? true) {
          toast.error(t.uploads.cleanupFailed);
        }
      });
    },
    [privateWork, t.uploads.cleanupFailed],
  );
  const preuploadControllerRef = useRef<{
    statusScopeKey: string;
    coordinatorScopeKey: string;
    threadId: string;
    controller: AbortController;
    cleanup: (uploaded: UploadedFileInfo) => void;
  } | null>(null);
  const attachmentUploadClaimsRef = useRef<
    Map<MessageSendAttempt, AttachmentUploadClaim>
  >(new Map());
  const pendingMessageAdmissionRef = useRef<PendingMessageAdmission | null>(
    null,
  );
  const detachedMessageAdmissionsRef = useRef<Set<PendingMessageAdmission>>(
    new Set(),
  );
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
  const streamOwnerMountedRef = useRef(false);
  useEffect(() => {
    streamOwnerMountedRef.current = true;
    return () => {
      streamOwnerMountedRef.current = false;
    };
  }, []);

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
    enabled: streamEnabled,
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
    runControlReplayGenerationRef.current += 1;
    setRunControlProgress(emptyRunControlProgress());
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
    runControlReplayGenerationRef.current += 1;
    setRunControlProgress((current) =>
      current.runId === _runId ? current : { runId: _runId, observations: [] },
    );
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
      try {
        listeners.current.onStart?.(_threadId, _runId);
      } catch {
        // A presentation observer cannot veto a server-admitted Run.
      }
      startedRef.current = true;
    }
    setOnStreamThreadId(_threadId);
  }, []);

  const latestHistoryRunId = historyRuns?.[0]?.run_id ?? null;
  useEffect(() => {
    const targetThreadId = onStreamThreadId ?? null;
    const targetRunId = latestHistoryRunId;
    if (!streamEnabled || !targetThreadId || !targetRunId) {
      return;
    }
    const activeRunId =
      currentRunThreadIdRef.current === targetThreadId
        ? currentRunIdRef.current
        : null;
    if (activeRunId && activeRunId !== targetRunId) {
      return;
    }

    const controller = new AbortController();
    const generation = ++runControlReplayGenerationRef.current;
    setRunControlProgress((current) =>
      current.runId === targetRunId
        ? current
        : { runId: targetRunId, observations: [] },
    );
    void fetchRunControlObservations(
      privateWork,
      targetThreadId,
      targetRunId,
      controller.signal,
    )
      .then((observations) => {
        if (
          controller.signal.aborted ||
          generation !== runControlReplayGenerationRef.current ||
          threadIdRef.current !== targetThreadId
        ) {
          return;
        }
        setRunControlProgress((current) =>
          mergeRunControlObservations(current, observations),
        );
      })
      .catch(() => {
        // Progress replay is supplementary. Message history and the terminal
        // Run remain authoritative when this bounded diagnostic read fails.
      });
    return () => controller.abort();
  }, [latestHistoryRunId, onStreamThreadId, privateWork, streamEnabled]);

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

  const releaseAttachmentUploadClaim = useCallback(
    (attempt: MessageSendAttempt) => {
      const claim = attachmentUploadClaimsRef.current.get(attempt);
      if (!claim) return;
      attachmentUploadClaimsRef.current.delete(attempt);
      attachmentUploadCoordinator.release(
        claim.coordinatorScopeKey,
        claim.clientIds,
      );
    },
    [attachmentUploadCoordinator],
  );
  const consumeAttachmentUploadClaim = useCallback(
    (attempt: MessageSendAttempt) => {
      const claim = attachmentUploadClaimsRef.current.get(attempt);
      if (!claim) return;
      attachmentUploadClaimsRef.current.delete(attempt);
      attachmentUploadCoordinator.consume(
        claim.coordinatorScopeKey,
        claim.clientIds,
      );
    },
    [attachmentUploadCoordinator],
  );

  const handleStreamFailure = (
    error: unknown,
    callbackOptions?: ThreadStreamCallbackMetadata,
  ) => {
    if (!streamOwnerMountedRef.current) return;
    const replayAttempt = preparedReplayAttemptRef.current;
    // SDK history refresh failures are metadata-less, including late errors
    // from an old A view after the hook has switched to B. Without an
    // explicit prepared-replay attribution window they cannot safely mutate
    // or toast any projection. Message admission failures are authoritative
    // through the submit lifecycle monitor below.
    if (
      shouldIgnoreMetadataLessStreamError(
        callbackOptions?.thread_id,
        replayAttempt?.status === "submitting",
      )
    ) {
      return;
    }
    if (
      shouldIgnoreAttributedThreadCallback(
        callbackOptions?.thread_id,
        currentViewThreadIdRef.current,
      )
    ) {
      return;
    }
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
      rollbackPreparedReplayFailure(replayAttempt.replay, decision.failedRunId);
    } else if (decision?.kind === "history-refetch-failure" && replayAttempt) {
      replayAttempt.historyRefetchError = error;
      setIgnoredReplayHistoryError({ error });
    }

    if (!leavesReplayProjectionIntact) {
      const failedRunId =
        callbackOptions?.run_id ?? currentRunIdRef.current ?? null;
      const failedThreadId =
        callbackOptions?.thread_id ?? threadIdRef.current ?? null;
      const terminalMessages = captureTerminalRunMessages(
        messagesRef.current,
        failedRunId,
        currentRunBaselineMessageIdsRef.current,
      );
      if (failedThreadId && terminalMessages.length > 0) {
        // The SDK may release its live messages before the durable Run
        // journal refetch completes. Reuse the same transient history bridge
        // as summarization so the terminal frame cannot disappear in between.
        pendingArchivedMessagesRef.current = dedupeMessagesByIdentity([
          ...pendingArchivedMessagesRef.current,
          ...terminalMessages,
        ]);
        pendingArchiveThreadIdRef.current = failedThreadId;
      }
      const retainsAdmittedHuman =
        failedRunId !== null && optimisticRunIdRef.current === failedRunId;
      if (retainsAdmittedHuman) {
        setFailedOptimisticThreadId(
          callbackOptions?.thread_id ?? threadIdRef.current,
        );
        setFailedOptimisticMessages((current) =>
          dedupeMessagesByIdentity([
            ...current,
            ...retainOptimisticHumanMessagesAfterFailure(
              optimisticMessagesRef.current,
              failedRunId,
            ),
          ]),
        );
      }
      optimisticRunIdRef.current = null;
      optimisticMessagesRef.current = [];
      setOptimisticMessages([]);
      setOptimisticThreadId(null);
      setLiveMessagesThreadId(null);
    }
    if (decision?.kind !== "ignore-history-refetch-duplicate") {
      const classifiedFailure = projectRunFailureCode(error);
      toast.error(
        isProjectAgentArchivedError(error)
          ? t.conversation.agentArchivedDescription
          : classifiedFailure
            ? resolveRunFailureCopy(t.conversation, classifiedFailure)
                .description
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
  };

  const thread = useStream<AgentThreadState>({
    client: privateWork.client,
    assistantId: "lead_agent",
    threadId: streamEnabled ? onStreamThreadId : null,
    reconnectOnMount: privateWork.reconnectOnMount,
    fetchStateHistory: { limit: 1 },
    throttle: true,
    onCreated(meta) {
      if (!streamOwnerMountedRef.current) return;
      const pendingScope = pendingMessageAdmissionRef.current;
      if (
        pendingScope?.threadId === meta.thread_id &&
        pendingScope.uploadStatusScopeKey !==
          currentUploadStatusScopeKeyRef.current
      ) {
        return;
      }
      // A submit started for a previously viewed thread may finish after the
      // hook has switched. It must not bind that old stream back into the new
      // projection or notify the new composer.
      if (
        !isCurrentThreadCallback(meta.thread_id, currentViewThreadIdRef.current)
      ) {
        return;
      }
      const replayAttempt = preparedReplayAttemptRef.current;
      if (
        replayAttempt?.status === "submitting" &&
        replayAttempt.threadId === meta.thread_id
      ) {
        replayAttempt.createdRunId = meta.run_id;
      }
      setIgnoredReplayHistoryError(null);
      handleStreamStart(meta.thread_id, meta.run_id);
      const pendingAdmission = pendingMessageAdmissionRef.current;
      if (
        pendingAdmission?.threadId === meta.thread_id &&
        pendingAdmission.uploadStatusScopeKey ===
          currentUploadStatusScopeKeyRef.current &&
        pendingAdmission.attempt === activeMessageSendRef.current &&
        pendingAdmission.serverAdmission.isPending()
      ) {
        pendingMessageAdmissionRef.current = null;
        pendingAdmission.serverAdmission.admit();
        consumeAttachmentUploadClaim(pendingAdmission.attempt);
        const consumedClientIds = new Set(pendingAdmission.uploadedClientIds);
        setAttachmentUploadStatusState((current) =>
          Object.fromEntries(
            Object.entries(current).filter(
              ([clientId, entry]) =>
                entry.scopeKey !== pendingAdmission.uploadStatusScopeKey ||
                entry.threadId !== meta.thread_id ||
                !consumedClientIds.has(clientId),
            ),
          ),
        );
        setLiveMessagesThreadId(meta.thread_id);
        if (pendingAdmission.optimisticMessages.length > 0) {
          optimisticRunIdRef.current = meta.run_id;
          optimisticMessagesRef.current = pendingAdmission.optimisticMessages;
          setOptimisticThreadId(meta.thread_id);
          setOptimisticMessages(pendingAdmission.optimisticMessages);
        } else {
          optimisticRunIdRef.current = null;
        }
        // `onCreated` is the server admission boundary. Resolve the composer
        // in this same React batch so the accepted user turn replaces, rather
        // than duplicates, its recoverable draft and attachments.
        setIsUploading(false);
        admitRunAndNotify(
          pendingAdmission.composerAdmission,
          pendingAdmission.onSent,
          () => listeners.current.onSend?.(meta.thread_id),
        );
      }
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
      if (!streamOwnerMountedRef.current) return;
      if (event.event === "on_tool_end") {
        listeners.current.onToolEnd?.({
          name: event.name,
          data: event.data,
        });
      }
    },
    onUpdateEvent(data) {
      if (!streamOwnerMountedRef.current) return;
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
      if (!streamOwnerMountedRef.current) return;
      if (!isRootStreamCallback(callbackOptions)) return;

      const terminalFailure = projectRunTerminalFailureEventToError(event);
      if (terminalFailure) {
        const failedThreadId =
          currentRunThreadIdRef.current ?? threadIdRef.current;
        const failedRunId = currentRunIdRef.current;
        if (failedThreadId && failedRunId) {
          expectedTerminalFailureKeysRef.current.add(
            `${failedThreadId}:${failedRunId}`,
          );
        }
        handleStreamFailure(terminalFailure, {
          thread_id: failedThreadId ?? undefined,
          run_id: failedRunId ?? undefined,
        });
        return;
      }

      const runControlObservation = parseRunControlLiveEvent(event);
      if (runControlObservation) {
        const activeRunId =
          currentRunThreadIdRef.current === threadIdRef.current
            ? currentRunIdRef.current
            : null;
        if (activeRunId && activeRunId !== runControlObservation.run_id) {
          return;
        }
        setRunControlProgress((current) =>
          mergeRunControlObservations(current, [runControlObservation]),
        );
        return;
      }

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
      handleStreamFailure(error, callbackOptions);
    },
    onFinish(state, callbackOptions) {
      if (!streamOwnerMountedRef.current) return;
      const terminalFailureKey =
        callbackOptions?.thread_id && callbackOptions.run_id
          ? `${callbackOptions.thread_id}:${callbackOptions.run_id}`
          : null;
      const expectedTerminalFailure = terminalFailureKey
        ? expectedTerminalFailureKeysRef.current.delete(terminalFailureKey)
        : false;
      if (
        shouldIgnoreAttributedThreadCallback(
          callbackOptions?.thread_id,
          currentViewThreadIdRef.current,
        )
      ) {
        return;
      }
      if (expectedTerminalFailure) {
        // The custom terminal event already ran the failure lifecycle. The SDK
        // sees a clean transport end, but this must not notify consumers that
        // the business Run completed successfully.
        return;
      }
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
  // Synchronous bridge for messages waiting to enter canonical history. This
  // covers both context-summarization rescue and the terminal live-to-journal
  // handoff, whose state updates can land on a different schedule than the SDK
  // external store. The projection reads this buffer so neither transition can
  // briefly drop visible messages (#3825).
  const pendingArchivedMessagesRef = useRef<Message[]>([]);
  // The thread the bridge belongs to. The merge only overlays the buffer when
  // this matches the viewed `threadId`, so messages can never flash into another
  // thread or the new-chat screen (#3825).
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
    const effectThreadId = threadId ?? null;
    const detachedAdmissions = detachedMessageAdmissionsRef.current;
    const resetAttemptUploads = (attempt: MessageSendAttempt | null) => {
      if (!attempt) return;
      const claim = attachmentUploadClaimsRef.current.get(attempt);
      if (claim) {
        attachmentUploadCoordinator.resetScope(
          claim.coordinatorScopeKey,
          claim.cleanup,
        );
      }
    };
    const stalePreupload = preuploadControllerRef.current;
    if (
      stalePreupload &&
      (stalePreupload.statusScopeKey !== uploadScopeKey ||
        stalePreupload.threadId !== effectThreadId)
    ) {
      stalePreupload.controller.abort();
      attachmentUploadCoordinator.resetScope(
        stalePreupload.coordinatorScopeKey,
        stalePreupload.cleanup,
      );
      preuploadControllerRef.current = null;
    }
    setAttachmentUploadStatusState((current) =>
      Object.fromEntries(
        Object.entries(current).filter(
          ([, entry]) =>
            entry.scopeKey === uploadScopeKey &&
            entry.threadId === effectThreadId,
        ),
      ),
    );
    messageSendGenerationRef.current += 1;
    const activeAttempt = activeMessageSendRef.current;
    activeAttempt?.abortController.abort();
    resetAttemptUploads(activeAttempt);
    activeMessageSendRef.current = null;
    const pendingAdmission = pendingMessageAdmissionRef.current;
    if (pendingAdmission?.serverAdmission.isPending()) {
      pendingMessageAdmissionRef.current = null;
      detachedAdmissions.add(pendingAdmission);
      pendingAdmission.composerAdmission.reject(
        new Error("The active thread changed before the Run was admitted."),
      );
    }
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
    setIsUploading(false);
    prevHumanMsgCountRef.current =
      latestMessageCountsRef.current.humanMessageCount;
    return () => {
      messageSendGenerationRef.current += 1;
      const activeAttempt = activeMessageSendRef.current;
      if (activeAttempt?.threadId === effectThreadId) {
        activeAttempt.abortController.abort();
        resetAttemptUploads(activeAttempt);
        activeMessageSendRef.current = null;
        sendInFlightRef.current = false;
      }
      const pending = pendingMessageAdmissionRef.current;
      if (pending?.threadId === effectThreadId) {
        pendingMessageAdmissionRef.current = null;
        detachedAdmissions.add(pending);
        pending.composerAdmission.reject(staleMessageSendError());
      }
      if (effectThreadId) {
        const preupload = preuploadControllerRef.current;
        if (
          preupload?.statusScopeKey === uploadScopeKey &&
          preupload.threadId === effectThreadId
        ) {
          preupload.controller.abort();
          attachmentUploadCoordinator.resetScope(
            preupload.coordinatorScopeKey,
            preupload.cleanup,
          );
          preuploadControllerRef.current = null;
        }
      }
    };
  }, [attachmentUploadCoordinator, threadId, uploadScopeKey]);

  // Release bridge entries once canonical history has absorbed them, so the
  // buffer stays transient and never resurrects a message that history later
  // filters out (e.g. a superseded run) (#3825).
  useEffect(() => {
    pendingArchivedMessagesRef.current = pruneConfirmedArchivedMessages(
      pendingArchivedMessagesRef.current,
      visibleHistory,
    );
  }, [visibleHistory]);

  useEffect(() => {
    if (optimisticThreadId && optimisticThreadId !== currentViewThreadId) {
      optimisticRunIdRef.current = null;
      optimisticMessagesRef.current = [];
      setOptimisticMessages([]);
      setOptimisticThreadId(null);
    }
    if (
      failedOptimisticThreadId &&
      failedOptimisticThreadId !== currentViewThreadId
    ) {
      setFailedOptimisticMessages([]);
      setFailedOptimisticThreadId(null);
    }
    if (liveMessagesThreadId && liveMessagesThreadId !== currentViewThreadId) {
      setLiveMessagesThreadId(null);
    }
  }, [
    currentViewThreadId,
    failedOptimisticThreadId,
    liveMessagesThreadId,
    optimisticThreadId,
  ]);

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
  const unacknowledgedFailedOptimisticMessages = useMemo(
    () =>
      retainUnacknowledgedOptimisticHumanMessages(failedOptimisticMessages, [
        ...visibleHistory,
        ...persistedMessages,
      ]),
    [failedOptimisticMessages, persistedMessages, visibleHistory],
  );
  useEffect(() => {
    if (optimisticMessageCount === 0) return;

    const newHumanMsgArrived = humanMessageCount > prevHumanMsgCountRef.current;

    if (
      !hasHumanOptimistic ||
      newHumanMsgArrived ||
      optimisticHumanAcknowledged
    ) {
      optimisticRunIdRef.current = null;
      optimisticMessagesRef.current = [];
      setOptimisticMessages([]);
      setOptimisticThreadId(null);
    }
  }, [
    hasHumanOptimistic,
    humanMessageCount,
    optimisticHumanAcknowledged,
    optimisticMessageCount,
  ]);
  useEffect(() => {
    if (
      unacknowledgedFailedOptimisticMessages.length ===
      failedOptimisticMessages.length
    ) {
      return;
    }
    setFailedOptimisticMessages(unacknowledgedFailedOptimisticMessages);
    if (unacknowledgedFailedOptimisticMessages.length === 0) {
      setFailedOptimisticThreadId(null);
    }
  }, [failedOptimisticMessages, unacknowledgedFailedOptimisticMessages]);

  const updateAttachmentUploadStatus = useCallback(
    (
      statusThreadId: string,
      clientId: string,
      status: AttachmentUploadStatus,
    ) => {
      setAttachmentUploadStatusState((current) => {
        const previous = current[clientId];
        if (
          previous?.scopeKey === uploadScopeKey &&
          previous.threadId === statusThreadId &&
          previous.status === status
        ) {
          return current;
        }
        return {
          ...current,
          [clientId]: {
            scopeKey: uploadScopeKey,
            threadId: statusThreadId,
            status,
          },
        };
      });
    },
    [uploadScopeKey],
  );

  const ensureAttachmentCandidates = useCallback(
    async ({
      targetThreadId,
      coordinatorScopeKey,
      candidates,
      retryPendingFailure,
      signal,
    }: {
      targetThreadId: string;
      coordinatorScopeKey: string;
      candidates: AttachmentUploadCandidate[];
      retryPendingFailure: boolean;
      signal?: AbortSignal;
    }) =>
      attachmentUploadCoordinator.ensure({
        scopeKey: coordinatorScopeKey,
        candidates,
        retryPendingFailure,
        signal,
        onStatusChange: (clientId, status) =>
          updateAttachmentUploadStatus(targetThreadId, clientId, status),
        upload: async (files, onFileUploaded) => {
          // View and private-work teardown abort the waiter, not this POST. The
          // Gateway's ready-file finalization is cancellation-safe, so the
          // response must return with its opaque id before abandoned-upload
          // cleanup can safely DELETE it from the exact old scope.
          await uploadFiles(
            targetThreadId,
            files,
            privateWork,
            undefined,
            (uploaded, _file, index) => onFileUploaded(uploaded, index),
          );
        },
      }),
    [attachmentUploadCoordinator, privateWork, updateAttachmentUploadStatus],
  );

  const prepareAttachments = useCallback(
    async (targetThreadId: string, fileParts: PromptInputFilePart[]) => {
      if (fileParts.length === 0) return;
      if (
        targetThreadId !== currentViewThreadIdRef.current ||
        uploadScopeKey !== currentUploadStatusScopeKeyRef.current ||
        privateWork.isActive?.() === false
      ) {
        throw staleMessageSendError();
      }
      const cacheableParts = fileParts.filter(
        (filePart): filePart is PromptInputFilePart & { clientId: string } =>
          typeof filePart.clientId === "string" && filePart.clientId.length > 0,
      );
      if (cacheableParts.length === 0) return;

      const prepared = await preparePromptAttachments(
        cacheableParts,
        (index) => `preupload-${index}`,
      );
      if (
        targetThreadId !== currentViewThreadIdRef.current ||
        uploadScopeKey !== currentUploadStatusScopeKeyRef.current ||
        privateWork.isActive?.() === false
      ) {
        return;
      }

      const coordinatorScopeKey = attachmentUploadCoordinatorScopeKey(
        privateWork.scope.accountId,
        privateWork.scope.projectId,
        targetThreadId,
      );

      let preupload = preuploadControllerRef.current;
      if (
        preupload?.statusScopeKey !== uploadScopeKey ||
        preupload.threadId !== targetThreadId ||
        preupload.controller.signal.aborted
      ) {
        preupload?.controller.abort();
        preupload = {
          statusScopeKey: uploadScopeKey,
          coordinatorScopeKey,
          threadId: targetThreadId,
          controller: new AbortController(),
          cleanup: (uploaded) =>
            cleanupUploadedAttachment(targetThreadId, uploaded),
        };
        preuploadControllerRef.current = preupload;
      }
      await ensureAttachmentCandidates({
        targetThreadId,
        coordinatorScopeKey,
        candidates: prepared,
        retryPendingFailure: false,
        signal: preupload.controller.signal,
      });
    },
    [
      cleanupUploadedAttachment,
      ensureAttachmentCandidates,
      privateWork,
      uploadScopeKey,
    ],
  );

  const discardAttachment = useCallback(
    (targetThreadId: string, clientId: string) => {
      if (
        targetThreadId !== currentViewThreadIdRef.current ||
        uploadScopeKey !== currentUploadStatusScopeKeyRef.current ||
        privateWork.isActive?.() === false
      ) {
        return false;
      }
      const coordinatorScopeKey = attachmentUploadCoordinatorScopeKey(
        privateWork.scope.accountId,
        privateWork.scope.projectId,
        targetThreadId,
      );
      const discarded = attachmentUploadCoordinator.discard(
        coordinatorScopeKey,
        clientId,
        (uploaded) => cleanupUploadedAttachment(targetThreadId, uploaded),
      );
      if (!discarded) return false;
      setAttachmentUploadStatusState((current) => {
        const entry = current[clientId];
        if (
          entry?.scopeKey !== uploadScopeKey ||
          entry.threadId !== targetThreadId
        ) {
          return current;
        }
        const next = { ...current };
        delete next[clientId];
        return next;
      });
      return true;
    },
    [
      attachmentUploadCoordinator,
      cleanupUploadedAttachment,
      privateWork,
      uploadScopeKey,
    ],
  );

  const sendMessage = useCallback(
    async (
      threadId: string,
      message: PromptInputMessage,
      extraContext?: Record<string, unknown>,
      options?: SendMessageOptions,
    ) => {
      if (sendInFlightRef.current) {
        throw new Error("A message submission is already in progress.");
      }
      if (
        [...detachedMessageAdmissionsRef.current].some(
          (candidate) =>
            candidate.threadId === threadId &&
            candidate.uploadStatusScopeKey === uploadScopeKey &&
            candidate.serverAdmission.isPending(),
        )
      ) {
        throw new Error(
          "The previous message submission is still settling for this thread.",
        );
      }
      const attempt = createMessageSendAttempt(
        messageSendGenerationRef.current + 1,
        threadId,
      );
      messageSendGenerationRef.current = attempt.generation;
      if (
        threadId !== currentViewThreadIdRef.current ||
        uploadScopeKey !== currentUploadStatusScopeKeyRef.current
      ) {
        attempt.abortController.abort();
        throw staleMessageSendError();
      }
      sendInFlightRef.current = true;
      activeMessageSendRef.current = attempt;

      const text = message.text.trim();
      const humanMessageId = `human-${crypto.randomUUID()}`;
      let uploadedFileInfo: UploadedFileInfo[] = [];
      let uploadedClientIds: string[] = [];
      const attachmentClientIds = message.files.map(
        (filePart, index) =>
          filePart.clientId ?? `send-${attempt.generation}-${index}`,
      );
      const uploadEntries = message.files.flatMap((filePart, index) =>
        isReadyPromptInputFilePart(filePart)
          ? []
          : [
              {
                filePart,
                clientId: attachmentClientIds[index]!,
              },
            ],
      );
      let lifecycleStarted = false;
      const uploadCoordinatorScopeKey = attachmentUploadCoordinatorScopeKey(
        privateWork.scope.accountId,
        privateWork.scope.projectId,
        threadId,
      );
      const requireCurrentAttempt = () => {
        if (
          !isCurrentMessageSendAttempt(
            activeMessageSendRef.current,
            attempt,
            currentViewThreadIdRef.current,
          ) ||
          uploadScopeKey !== currentUploadStatusScopeKeyRef.current
        ) {
          throw staleMessageSendError();
        }
      };

      try {
        // Upload files first if any
        if (uploadEntries.length > 0) {
          setIsUploading(true);
          try {
            uploadedClientIds = uploadEntries.map((entry) => entry.clientId);
            if (
              !attachmentUploadCoordinator.claim(
                uploadCoordinatorScopeKey,
                uploadedClientIds,
              )
            ) {
              throw new Error(
                "An attachment is already being submitted or was removed.",
              );
            }
            attachmentUploadClaimsRef.current.set(attempt, {
              threadId,
              coordinatorScopeKey: uploadCoordinatorScopeKey,
              clientIds: uploadedClientIds,
              cleanup: (uploaded) =>
                cleanupUploadedAttachment(threadId, uploaded),
            });
            const prepared = await preparePromptAttachments(
              uploadEntries.map((entry) => entry.filePart),
              (index) => uploadEntries[index]!.clientId,
            );
            requireCurrentAttempt();
            uploadedFileInfo = await ensureAttachmentCandidates({
              targetThreadId: threadId,
              coordinatorScopeKey: uploadCoordinatorScopeKey,
              candidates: prepared,
              retryPendingFailure: true,
              signal: attempt.abortController.signal,
            });
            requireCurrentAttempt();
          } catch (error) {
            if (
              isCurrentMessageSendAttempt(
                activeMessageSendRef.current,
                attempt,
                currentViewThreadIdRef.current,
              )
            ) {
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
            }
            throw error;
          }
        }

        requireCurrentAttempt();

        let uploadedIndex = 0;
        const filesForSubmit: FileInMessage[] = message.files.map(
          (filePart) => {
            if (isReadyPromptInputFilePart(filePart)) {
              return readyPromptInputFileToMessage(filePart);
            }
            const uploaded = uploadedFileInfo[uploadedIndex];
            uploadedIndex += 1;
            if (!uploaded) {
              throw new Error("An uploaded attachment reference is missing.");
            }
            return uploadedFileInfoToMessage(uploaded);
          },
        );
        const submitMessages = buildThreadSubmitMessages({
          text,
          messageId: humanMessageId,
          additionalKwargs: options?.additionalKwargs,
          additionalInputMessages: options?.additionalInputMessages,
          filesForSubmit,
        });
        const hideFromUI = options?.additionalKwargs?.hide_from_ui === true;
        const visibleHumanMessage = submitMessages.at(-1);
        const serverAdmission = createRunAdmissionLatch();
        // This latch is an internal state gate. Lifecycle callbacks observe its
        // state, while this handler prevents a rejected gate from surfacing as
        // an unhandled Promise rejection.
        void serverAdmission.promise.catch(() => undefined);
        const composerAdmission = createRunAdmissionLatch();
        // `thread.submit()` may throw synchronously before control reaches the
        // await below. Observe the latch immediately so that rejection still
        // follows the outer send promise without becoming unhandled.
        void composerAdmission.promise.catch(() => undefined);
        const pendingAdmission: PendingMessageAdmission = {
          threadId,
          uploadStatusScopeKey: uploadScopeKey,
          attempt,
          serverAdmission,
          composerAdmission,
          optimisticMessages:
            !hideFromUI && visibleHumanMessage ? [visibleHumanMessage] : [],
          uploadedClientIds,
          onSent: options?.onSent,
        };

        // Capture the baseline immediately before dispatch. Uploading is not a
        // conversation turn and therefore must not alter the message projection.
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
        pendingMessageAdmissionRef.current = pendingAdmission;
        requireCurrentAttempt();

        const lifecycle = thread.submit(
          { messages: submitMessages },
          {
            threadId: threadId,
            ...buildThreadSubmitCheckpointOptions(
              options?.continueFromLatestCheckpoint,
            ),
            ...buildRootThreadStreamOptions(),
            context: withRunWorkloadProfileContext(
              withRunExecutionProfileContext(
                {
                  ...extraContext,
                  ...context,
                  thread_id: threadId,
                },
                executionProfile,
              ),
              options?.workloadProfile ?? DEFAULT_RUN_WORKLOAD_PROFILE,
            ),
          },
        );
        lifecycleStarted = true;
        void monitorRunAdmissionLifecycle({
          admission: serverAdmission,
          lifecycle,
          onAdmissionFailure: (error) => {
            detachedMessageAdmissionsRef.current.delete(pendingAdmission);
            // The Gateway serializes this conditional discard against Run
            // admission on the Thread lock. It deletes rejected inputs and
            // retains files already frozen into an admitted Run snapshot.
            releaseAttachmentUploadClaim(attempt);
            pendingAdmission.composerAdmission.reject(error);
            if (pendingMessageAdmissionRef.current === pendingAdmission) {
              pendingMessageAdmissionRef.current = null;
              setIsUploading(false);
              if (
                streamOwnerMountedRef.current &&
                isCurrentMessageSendAttempt(
                  activeMessageSendRef.current,
                  attempt,
                  currentViewThreadIdRef.current,
                )
              ) {
                const classifiedFailure = projectRunFailureCode(error);
                toast.error(
                  isProjectAgentArchivedError(error)
                    ? t.conversation.agentArchivedDescription
                    : isRunAdmissionNotConfirmedError(error)
                      ? t.conversation.runAdmissionNotConfirmedDescription
                      : classifiedFailure
                        ? resolveRunFailureCopy(
                            t.conversation,
                            classifiedFailure,
                          ).description
                        : isProjectRunTerminalFailure(error)
                          ? t.conversation.runFailedDescription
                          : getStreamErrorMessage(error),
                );
              }
            }
          },
          onSettled: () => {
            detachedMessageAdmissionsRef.current.delete(pendingAdmission);
            // A view change can intentionally detach the local admission latch
            // before the server lifecycle settles. If no definitive admission
            // failure released the claim, preserve the server file as consumed.
            consumeAttachmentUploadClaim(attempt);
            if (activeMessageSendRef.current === attempt) {
              activeMessageSendRef.current = null;
              sendInFlightRef.current = false;
            }
          },
        });

        // `thread.submit()` settles at the Run terminal. The composer instead
        // waits only for `useStream.onCreated`, the durable admission boundary.
        await composerAdmission.promise;
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
        if (!lifecycleStarted) {
          releaseAttachmentUploadClaim(attempt);
        }
        if (activeMessageSendRef.current === attempt) {
          const pendingAdmission = pendingMessageAdmissionRef.current;
          if (
            pendingAdmission?.attempt === attempt &&
            pendingAdmission.serverAdmission.isPending()
          ) {
            pendingMessageAdmissionRef.current = null;
            pendingAdmission.serverAdmission.reject(error);
            pendingAdmission.composerAdmission.reject(error);
          }
          setOptimisticMessages([]);
          setOptimisticThreadId(null);
          setLiveMessagesThreadId(null);
          setIsUploading(false);
          if (!lifecycleStarted) {
            activeMessageSendRef.current = null;
            sendInFlightRef.current = false;
          }
        }
        throw error;
      }
    },
    [
      thread,
      t.uploads.preflightRejected,
      t.uploads.serverTooLarge,
      t.uploads.storageQuotaExceeded,
      t.uploads.uploadFailed,
      t.conversation,
      context,
      attachmentUploadCoordinator,
      cleanupUploadedAttachment,
      consumeAttachmentUploadClaim,
      ensureAttachmentCandidates,
      executionProfile,
      queryClient,
      humanMessageCount,
      persistedMessages,
      privateWork,
      releaseAttachmentUploadClaim,
      uploadScopeKey,
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
        }) ||
        [...detachedMessageAdmissionsRef.current].some(
          (candidate) =>
            candidate.threadId === threadId &&
            candidate.uploadStatusScopeKey === uploadScopeKey &&
            candidate.serverAdmission.isPending(),
        )
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
        toast.error(
          isProjectAgentArchivedError(error)
            ? t.conversation.agentArchivedDescription
            : getStreamErrorMessage(error),
        );
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
      t.conversation.agentArchivedDescription,
      thread,
      uploadScopeKey,
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
  const visibleOptimisticMessages = useMemo(() => {
    const pendingMessages = getVisibleOptimisticMessages(
      optimisticThreadId === currentViewThreadId
        ? optimisticMessages
        : EMPTY_MESSAGES,
      previousHumanMessageCount,
      humanMessageCount,
    );
    const failedMessages =
      failedOptimisticThreadId === currentViewThreadId
        ? failedOptimisticMessages
        : EMPTY_MESSAGES;
    return dedupeMessagesByIdentity([...failedMessages, ...pendingMessages]);
  }, [
    currentViewThreadId,
    failedOptimisticMessages,
    failedOptimisticThreadId,
    humanMessageCount,
    optimisticMessages,
    optimisticThreadId,
    previousHumanMessageCount,
  ]);

  const explicitActiveRunId =
    currentRunThreadIdRef.current === threadId ? currentRunIdRef.current : null;
  const effectiveRunWorkloadProfile = useMemo(
    () =>
      resolveDisplayedRunWorkloadProfile(
        historyRuns ?? [],
        explicitActiveRunId,
      ),
    [explicitActiveRunId, historyRuns],
  );
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
  const attachmentUploadStatuses = useMemo(() => {
    const statuses: Record<string, AttachmentUploadStatus> = {};
    for (const [clientId, entry] of Object.entries(
      attachmentUploadStatusState,
    )) {
      if (
        entry.scopeKey === uploadScopeKey &&
        entry.threadId === currentViewThreadId
      ) {
        statuses[clientId] = entry.status;
      }
    }
    return statuses;
  }, [attachmentUploadStatusState, currentViewThreadId, uploadScopeKey]);

  return {
    thread: mergedThread,
    boundThreadId: onStreamThreadId ?? null,
    pendingUsageMessages,
    attachRun,
    prepareAttachments,
    discardAttachment,
    attachmentUploadStatuses,
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
    effectiveRunWorkloadProfile,
    hasTerminalRunFailure: latestRunHasTerminalFailure(historyRuns),
    runFailureCode,
    runFailureRunId,
    runControlObservations: runControlProgress.observations,
  } as const;
}
