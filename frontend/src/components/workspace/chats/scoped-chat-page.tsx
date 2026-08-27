"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { type PromptInputMessage } from "@/components/ai-elements/prompt-input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { AgentArchivedAlert } from "@/components/workspace/agent-archived-alert";
import { ArtifactTrigger } from "@/components/workspace/artifacts";
import { ExportTrigger } from "@/components/workspace/export-trigger";
import { GoalStatus } from "@/components/workspace/goal-status";
import {
  InputBox,
  type InputBoxSubmitOptions,
} from "@/components/workspace/input-box";
import {
  MessageList,
  MESSAGE_LIST_DEFAULT_PADDING_BOTTOM,
} from "@/components/workspace/messages";
import { ThreadContext } from "@/components/workspace/messages/context";
import { ExecutionApprovalCard } from "@/components/workspace/messages/execution-approval-card";
import { RunControlProgress } from "@/components/workspace/run-control-progress";
import {
  canReplayRunFailure,
  canRestoreRunFailureInput,
  canRetryModelOutputLimit,
  RunFailureAlert,
  shouldShowRunFailureAlert,
} from "@/components/workspace/run-failure-alert";
import {
  SidecarProvider,
  SidecarTrigger,
} from "@/components/workspace/sidecar";
import { ThreadDocumentTitle } from "@/components/workspace/thread-title";
import { TodoList } from "@/components/workspace/todo-list";
import { TokenUsageIndicator } from "@/components/workspace/token-usage-indicator";
import { useActiveGoal } from "@/components/workspace/use-active-goal";
import { Welcome } from "@/components/workspace/welcome";
import {
  commitExecutionApprovalDecisionResponse,
  executionApprovalActiveQueryKey,
  executionApprovalBlocksSending,
  executionApprovalContinuationRunId,
  executionApprovalDecisionSurface,
  executionApprovalNeedsAdmissionRecovery,
  findLatestExecutionApprovalArtifact,
  submitExecutionApprovalDecision,
  type ExecutionApprovalDecision,
  useThreadExecutionApproval,
} from "@/core/execution-approvals";
import { useI18n } from "@/core/i18n/hooks";
import {
  buildHumanInputResponseText,
  hasOpenHumanInputRequest,
  type HumanInputRequest,
  type HumanInputResponse,
} from "@/core/messages/human-input";
import { isHiddenFromUIMessage } from "@/core/messages/utils";
import { useModels } from "@/core/models/hooks";
import { useNotification } from "@/core/notification/hooks";
import { isProjectAgentArchivedError } from "@/core/private-work/api-client";
import { commitProjectMemoryCacheChange } from "@/core/private-work/memory-freshness";
import {
  runPrivateWorkAbortable,
  type ProjectPrivateWorkScope,
} from "@/core/private-work/types";
import {
  CHAT_CONTENT_WIDTH_CSS_VALUES,
  useLocalSettings,
  useThreadSettings,
} from "@/core/settings";
import { useThreadAgentModelRef } from "@/core/shared-assets";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import type { AgentThread } from "@/core/threads";
import { resolveAgentExecutionAvailability } from "@/core/threads/agent-mode";
import {
  getLatestRegenerationTarget,
  resolveFailedRunComposerInput,
  resolveThreadAvailability,
  useBranchThread,
  useThreadMetadata,
  useThreadStream,
  useThreadTokenUsage,
} from "@/core/threads/hooks";
import {
  fetchRunExecutionState,
  isTerminalRunExecutionState,
  runExecutionStateObserverQueryKey,
  runExecutionStatePollInterval,
  runExecutionStateQueryEnabled,
  runExecutionStateRetryDelay,
  selectObservedRunExecutionState,
  shouldRetryRunExecutionState,
  type RunExecutionState,
} from "@/core/threads/run-execution-state";
import { threadTokenUsageToTokenUsage } from "@/core/threads/token-usage";
import { textOfMessage } from "@/core/threads/utils";
import { cn } from "@/lib/utils";

import { ChatBox } from ".";

export interface ChatRouteScope {
  privateWork: ProjectPrivateWorkScope;
  threadBasePath: string;
  threadListPath: string;
  canCreate: boolean;
  canRun: boolean;
  canApproveHostExecution: boolean;
  canUpload: boolean;
  canDelete: boolean;
}

export type ScopedChatRouteScope = ChatRouteScope & {
  goalVisible?: boolean;
  compactVisible?: boolean;
  branchVisible?: boolean;
  regenerateVisible?: boolean;
  sidecarVisible?: boolean;
  artifactsVisible?: boolean;
  followupSuggestionsEnabled?: boolean;
  canDeleteFiles?: boolean;
};

export function shouldShowThreadWelcome({
  isHistoryLoading,
  hasMoreHistory,
  visibleMessageCount,
  dismissed,
}: {
  isHistoryLoading: boolean;
  hasMoreHistory: boolean;
  visibleMessageCount: number;
  dismissed: boolean;
}) {
  if (dismissed) return false;
  return !isHistoryLoading && !hasMoreHistory && visibleMessageCount === 0;
}

function OptionalSidecarProvider({
  enabled,
  children,
  ...props
}: React.ComponentProps<typeof SidecarProvider> & { enabled: boolean }) {
  if (!enabled) return children;
  return (
    <SidecarProvider key={props.parentThreadId} {...props}>
      {children}
    </SidecarProvider>
  );
}

function currentDocumentVisibility(): DocumentVisibilityState {
  return typeof document === "undefined" ? "hidden" : document.visibilityState;
}

function useDocumentVisibility(): DocumentVisibilityState {
  const [visibility, setVisibility] = useState(currentDocumentVisibility);
  useEffect(() => {
    const updateVisibility = () => {
      setVisibility(currentDocumentVisibility());
    };
    document.addEventListener("visibilitychange", updateVisibility);
    return () => {
      document.removeEventListener("visibilitychange", updateVisibility);
    };
  }, []);
  return visibility;
}

export function ScopedChatPage({
  scope,
  missingThreadFallback = null,
  renderHeaderAccessory,
  onStartNewAgentChat,
}: {
  scope: ScopedChatRouteScope;
  missingThreadFallback?: React.ReactNode;
  renderHeaderAccessory?: (
    thread: AgentThread | null | undefined,
  ) => React.ReactNode;
  onStartNewAgentChat?: (thread: AgentThread | null | undefined) => void;
}) {
  const { t } = useI18n();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { thread_id: threadId } = useParams<{ thread_id: string }>();
  const privateWork = scope.privateWork;
  const documentVisibility = useDocumentVisibility();
  const isMock = false;
  // Project chat creation is server-first, so every dynamic chat route owns an
  // already-persisted thread. Welcome mode is now purely a visual projection of
  // a settled empty thread rather than a second client-side creation state.
  const [isWelcomeMode, setIsWelcomeMode] = useState(false);
  const welcomeDismissedThreadIdsRef = useRef(new Set<string>());
  const [settings, setSettings] = useThreadSettings(threadId);
  const [localSettings, setLocalSettings] = useLocalSettings();
  const threadMetadata = useThreadMetadata(threadId, {
    enabled: !isMock,
    isMock,
    privateWork,
  });
  const threadAvailability = resolveThreadAvailability(threadMetadata);
  const threadReady = threadAvailability === "available";
  const threadMissing = threadAvailability === "not-found";
  const modelCatalog = useModels({ enabled: threadReady });
  const { models, tokenUsageEnabled } = modelCatalog;
  const threadTokenUsage = useThreadTokenUsage(threadId, {
    enabled: tokenUsageEnabled && !isMock && threadReady,
    privateWork,
  });
  const agentModel = useThreadAgentModelRef(threadMetadata.data?.metadata, {
    enabled: threadReady,
  });
  const agentExecutionAvailability = resolveAgentExecutionAvailability({
    required: threadReady,
    agentModelRef: agentModel.modelRef,
    agentModelLoading:
      threadMetadata.isLoading ||
      threadMetadata.isFetching ||
      agentModel.isLoading,
    agentModelError: threadMetadata.error ?? agentModel.error,
    models,
    modelsLoading: modelCatalog.isLoading || modelCatalog.isFetching,
    modelsError: modelCatalog.error,
  });
  const agentModelBlocked =
    !threadReady || agentExecutionAvailability !== "ready";
  const agentArchived = threadReady && agentModel.agentArchived;
  const agentSuspended = threadReady && agentModel.agentSuspended;
  const agentModelUnavailable =
    threadReady &&
    !agentArchived &&
    !agentSuspended &&
    agentExecutionAvailability === "unavailable";
  const handleAgentModelRetry = useCallback(() => {
    void Promise.all([agentModel.refetch(), modelCatalog.refetch()]);
  }, [agentModel, modelCatalog]);
  const branchThread = useBranchThread(privateWork);
  const backendTokenUsage = threadTokenUsageToTokenUsage(threadTokenUsage.data);
  const mountedRef = useRef(false);
  const [composerRestoreRequest, setComposerRestoreRequest] = useState<{
    threadId: string;
    id: string;
    text: string;
    files: NonNullable<
      ReturnType<typeof resolveFailedRunComposerInput>
    >["files"];
  } | null>(null);

  useEffect(() => {
    mountedRef.current = true;
  }, []);

  const { showNotification } = useNotification();

  const {
    thread,
    terminalDisplayLatched,
    activeRunId,
    activeRunResolverGeneration,
    reconcileTerminalRun,
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
    hasTerminalRunFailure,
    runFailureCode,
    runFailureRunId,
    runControlObservations,
  } = useThreadStream({
    threadId,
    displayThreadId: threadId,
    context: settings.context,
    agentModelRef: agentModel.modelRef,
    enabled: threadReady,
    isMock,
    privateWork,
    onSend: () => {
      welcomeDismissedThreadIdsRef.current.add(threadId);
      setIsWelcomeMode(false);
    },
    onFinish: (state) => {
      void queryClient.invalidateQueries({
        queryKey: executionApprovalActiveQueryKey(privateWork.scope, threadId),
        exact: true,
      });
      // A completed Run may have appended explicit `remember` or automatic
      // SNIP history. The hint carries no Run or Memory content; receivers
      // re-read the owner-scoped server APIs.
      void commitProjectMemoryCacheChange(
        queryClient,
        privateWork.scope,
        "pending",
      ).catch(() => undefined);
      if (document.hidden || !document.hasFocus()) {
        let body = "Conversation finished";
        const lastMessage = state.messages.at(-1);
        if (lastMessage) {
          const textContent = textOfMessage(lastMessage);
          if (textContent) {
            body =
              textContent.length > 200
                ? textContent.substring(0, 200) + "..."
                : textContent;
          }
        }
        showNotification(state.title, { body });
      }
    },
  });

  const executionStateQueryKey = useMemo(
    () =>
      activeRunId && activeRunResolverGeneration !== null
        ? runExecutionStateObserverQueryKey(
            privateWork.scope,
            threadId,
            activeRunId,
            activeRunResolverGeneration,
          )
        : ([
            ...privateWork.queryKeyPrefix,
            "thread",
            threadId,
            "run-execution-state",
            "inactive",
          ] as const),
    [
      activeRunId,
      activeRunResolverGeneration,
      privateWork.queryKeyPrefix,
      privateWork.scope,
      threadId,
    ],
  );
  const executionStateQueryEnabled =
    threadReady &&
    runExecutionStateQueryEnabled(
      activeRunId,
      activeRunResolverGeneration,
      documentVisibility,
    );
  const executionStateQuery = useQuery<RunExecutionState>({
    queryKey: executionStateQueryKey,
    queryFn: ({ signal }) => {
      if (!activeRunId || activeRunResolverGeneration === null) {
        throw new Error("An exact active Run is required");
      }
      return fetchRunExecutionState(privateWork, threadId, activeRunId, signal);
    },
    enabled: executionStateQueryEnabled,
    retry: shouldRetryRunExecutionState,
    retryDelay: runExecutionStateRetryDelay,
    refetchInterval: (query) =>
      runExecutionStatePollInterval(
        currentDocumentVisibility(),
        query.state.status === "error" ? undefined : query.state.data,
      ),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
  useEffect(() => {
    const queryKey = executionStateQueryKey;
    if (!executionStateQueryEnabled) {
      void queryClient.cancelQueries({ queryKey, exact: true });
    }
    return () => {
      void queryClient.cancelQueries({ queryKey, exact: true });
    };
  }, [executionStateQueryEnabled, executionStateQueryKey, queryClient]);
  const runExecutionState: RunExecutionState | "unavailable" | null =
    activeRunId && activeRunResolverGeneration !== null
      ? selectObservedRunExecutionState(
          executionStateQuery.data,
          executionStateQuery.isError,
        )
      : null;
  useEffect(() => {
    if (
      !activeRunId ||
      activeRunResolverGeneration === null ||
      !executionStateQuery.data ||
      !isTerminalRunExecutionState(executionStateQuery.data)
    ) {
      return;
    }
    void reconcileTerminalRun(activeRunId, activeRunResolverGeneration);
  }, [
    activeRunId,
    activeRunResolverGeneration,
    executionStateQuery.data,
    reconcileTerminalRun,
  ]);

  const persistedExecutionApproval = useMemo(
    () => findLatestExecutionApprovalArtifact(thread.messages),
    [thread.messages],
  );
  const executionApproval = useThreadExecutionApproval({
    privateWork,
    threadId,
    persistedApprovalId: persistedExecutionApproval?.approval_id,
    enabled: !isMock && threadReady,
  });

  const approval = executionApproval.approval;
  const approvalDecision = executionApprovalDecisionSurface(approval);
  const approvalBlocksSending =
    executionApprovalBlocksSending(approval) ||
    executionApproval.isPreparing ||
    (!isMock && executionApproval.active.isPending);
  const [pendingExecutionDecision, setPendingExecutionDecision] =
    useState<ExecutionApprovalDecision | null>(null);
  const [executionDecisionError, setExecutionDecisionError] = useState<
    string | null
  >(null);
  type ExecutionDecisionAttempt = {
    threadId: string;
    approvalId: string;
    expectedVersion: string;
    decision: ExecutionApprovalDecision;
    idempotencyKey: string;
  };
  const executionDecisionInFlightRef = useRef<ExecutionDecisionAttempt | null>(
    null,
  );
  const executionDecisionAttemptRef = useRef<ExecutionDecisionAttempt | null>(
    null,
  );
  const executionDecisionViewRef = useRef({
    threadId,
    approvalId: approval?.approval_id ?? null,
  });
  executionDecisionViewRef.current = {
    threadId,
    approvalId: approval?.approval_id ?? null,
  };

  useEffect(() => {
    const attempt = executionDecisionAttemptRef.current;
    const preserveApprovedRecovery =
      attempt?.decision === "allow_once" &&
      attempt.threadId === threadId &&
      attempt.approvalId === approval?.approval_id &&
      approval.status === "approved" &&
      approval.continuation_run === null;
    if (
      attempt &&
      !preserveApprovedRecovery &&
      (attempt.threadId !== threadId ||
        attempt.approvalId !== approval?.approval_id ||
        attempt.expectedVersion !== approval.version ||
        approval.status !== "pending")
    ) {
      executionDecisionAttemptRef.current = null;
      setPendingExecutionDecision(null);
      setExecutionDecisionError(null);
    }
  }, [
    approval?.approval_id,
    approval?.continuation_run,
    approval?.status,
    approval?.version,
    threadId,
  ]);

  const handleExecutionApprovalDecision = useCallback(
    async (decision: ExecutionApprovalDecision) => {
      const isPendingDecision =
        approval?.status === "pending" && approval.can_decide;
      const isApprovedRecovery =
        decision === "allow_once" &&
        executionApprovalNeedsAdmissionRecovery(approval);
      if (
        executionDecisionInFlightRef.current?.threadId === threadId ||
        !scope.canRun ||
        !scope.canApproveHostExecution ||
        (!isPendingDecision && !isApprovedRecovery) ||
        !approval
      ) {
        return;
      }

      const existingAttempt = executionDecisionAttemptRef.current;
      const attempt =
        existingAttempt?.threadId === threadId &&
        existingAttempt.approvalId === approval.approval_id &&
        existingAttempt.decision === decision &&
        (isApprovedRecovery ||
          existingAttempt.expectedVersion === approval.version)
          ? existingAttempt
          : {
              threadId,
              approvalId: approval.approval_id,
              expectedVersion: approval.version,
              decision,
              idempotencyKey: crypto.randomUUID(),
            };
      executionDecisionAttemptRef.current = attempt;
      executionDecisionInFlightRef.current = attempt;
      setPendingExecutionDecision(decision);
      setExecutionDecisionError(null);

      try {
        const response = await runPrivateWorkAbortable(privateWork, (signal) =>
          submitExecutionApprovalDecision(
            privateWork,
            threadId,
            approval.source_run_id,
            approval.approval_id,
            {
              schema_version: 1,
              decision,
              expected_version: attempt.expectedVersion,
              idempotency_key: attempt.idempotencyKey,
            },
            signal,
          ),
        );
        if (privateWork.isActive?.() === false) return;
        commitExecutionApprovalDecisionResponse(
          queryClient,
          privateWork.scope,
          threadId,
          approval.approval_id,
          response,
        );
        if (executionDecisionAttemptRef.current === attempt) {
          executionDecisionAttemptRef.current = null;
        }
        void Promise.all([
          executionApproval.active.refetch(),
          executionApproval.byId.refetch(),
        ]);
      } catch (error) {
        const currentView = executionDecisionViewRef.current;
        if (
          currentView.threadId === attempt.threadId &&
          currentView.approvalId === attempt.approvalId
        ) {
          setExecutionDecisionError(
            isProjectAgentArchivedError(error)
              ? t.conversation.agentArchivedDescription
              : error instanceof Error
                ? error.message
                : t.executionApproval.decisionFailed,
          );
        }
        void Promise.all([
          executionApproval.active.refetch(),
          executionApproval.byId.refetch(),
        ]);
      } finally {
        if (executionDecisionInFlightRef.current === attempt) {
          executionDecisionInFlightRef.current = null;
        }
        const currentView = executionDecisionViewRef.current;
        if (
          currentView.threadId === attempt.threadId &&
          currentView.approvalId === attempt.approvalId
        ) {
          setPendingExecutionDecision(null);
        }
      }
    },
    [
      approval,
      executionApproval.active,
      executionApproval.byId,
      privateWork,
      queryClient,
      scope.canApproveHostExecution,
      scope.canRun,
      t.conversation.agentArchivedDescription,
      t.executionApproval.decisionFailed,
      threadId,
    ],
  );

  useEffect(() => {
    if (
      !executionApprovalNeedsAdmissionRecovery(approval) ||
      !scope.canRun ||
      !scope.canApproveHostExecution
    ) {
      return;
    }
    const timeout = window.setTimeout(() => {
      void handleExecutionApprovalDecision("allow_once");
    }, 1_500);
    return () => window.clearTimeout(timeout);
  }, [
    approval,
    handleExecutionApprovalDecision,
    scope.canApproveHostExecution,
    scope.canRun,
  ]);

  const continuationRunId = executionApprovalContinuationRunId(approval);
  useEffect(() => {
    if (!continuationRunId) return;
    void attachRun(continuationRunId);
  }, [attachRun, continuationRunId, thread.isLoading]);

  const visibleMessageCount = useMemo(
    () =>
      thread.messages.filter((message) => !isHiddenFromUIMessage(message))
        .length,
    [thread.messages],
  );
  const metadataSettled = threadAvailability !== "loading";

  const shouldWelcome = shouldShowThreadWelcome({
    isHistoryLoading:
      isHistoryLoading || !metadataSettled || historyError !== null,
    hasMoreHistory,
    visibleMessageCount,
    dismissed: welcomeDismissedThreadIdsRef.current.has(threadId),
  });

  useEffect(() => {
    setIsWelcomeMode(shouldWelcome);
  }, [shouldWelcome]);
  const threadMetadataFailed = !isMock && threadAvailability === "error";

  useEffect(() => {
    if (threadMissing && missingThreadFallback == null) {
      router.replace(scope.threadListPath);
    }
  }, [missingThreadFallback, router, scope.threadListPath, threadMissing]);

  const handleSubmit = useCallback(
    (message: PromptInputMessage, options?: InputBoxSubmitOptions) => {
      if (
        !threadReady ||
        !scope.canRun ||
        approvalBlocksSending ||
        (!scope.canUpload && message.files.length > 0)
      ) {
        return Promise.reject(
          new Error("The current project cannot admit this message."),
        );
      }
      return sendMessage(threadId, message, undefined, options);
    },
    [
      approvalBlocksSending,
      threadReady,
      scope.canRun,
      scope.canUpload,
      sendMessage,
      threadId,
    ],
  );
  const handleSubmitHumanInput = useCallback(
    async (request: HumanInputRequest, response: HumanInputResponse) => {
      if (!threadReady || !scope.canRun || approvalBlocksSending) return false;
      let sent = false;
      await sendMessage(
        threadId,
        {
          text: buildHumanInputResponseText(request, response),
          files: [],
        },
        undefined,
        {
          additionalKwargs: {
            hide_from_ui: true,
            human_input_response: response,
          },
          onSent: () => {
            sent = true;
          },
        },
      );
      return sent;
    },
    [approvalBlocksSending, scope.canRun, sendMessage, threadId, threadReady],
  );
  const handleStop = useCallback(async () => {
    if (!threadReady || !scope.canRun) return;
    await thread.stop();
  }, [scope.canRun, thread, threadReady]);
  const handleRegenerate = useCallback(
    (messageId: string, supersededMessageIds: string[]) =>
      regenerateMessage(threadId, messageId, supersededMessageIds),
    [regenerateMessage, threadId],
  );
  const handleEditAndRegenerate = useCallback(
    (messageId: string, replacementText: string) =>
      editAndRegenerateMessage(threadId, messageId, replacementText),
    [editAndRegenerateMessage, threadId],
  );
  const handleBranchTurn = useCallback(
    async (messageId: string, messageIds: string[]) => {
      if (!scope.branchVisible || isMock || isStaticWebsiteOnly()) {
        return;
      }

      try {
        const response = await branchThread.mutateAsync({
          threadId,
          messageId,
          messageIds,
        });
        toast.success(t.conversation.branchCreated);
        router.push(`${scope.threadBasePath}/${response.thread_id}`);
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : t.conversation.branchFailed,
        );
      }
    },
    [branchThread, isMock, router, scope, t, threadId],
  );

  const tokenUsageInlineMode = tokenUsageEnabled
    ? localSettings.tokenUsage.inlineMode
    : "off";
  const hasTodos = (thread.values.todos?.length ?? 0) > 0;
  const { activeGoal, hasGoal, setLocalGoal } = useActiveGoal(
    threadId,
    thread.values.goal,
  );
  const hasOpenHumanInputCard = useMemo(
    () =>
      hasOpenHumanInputRequest(
        thread.messages,
        (message) => !isHiddenFromUIMessage(message),
      ),
    [thread.messages],
  );
  const hasRunFailure = shouldShowRunFailureAlert({
    hasTerminalRunFailure,
    streamError: thread.error,
  });
  const messageReplayAllowed = canReplayRunFailure(runFailureCode);
  const recoverableFailedInput = useMemo(() => {
    if (
      !hasRunFailure ||
      !runFailureRunId ||
      !canRestoreRunFailureInput(runFailureCode)
    ) {
      return null;
    }
    return resolveFailedRunComposerInput(thread.messages, runFailureRunId);
  }, [hasRunFailure, runFailureCode, runFailureRunId, thread.messages]);
  const handleRestoreFailedInput = useCallback(() => {
    if (!recoverableFailedInput) return;
    setComposerRestoreRequest({
      threadId,
      id: crypto.randomUUID(),
      text: recoverableFailedInput.text,
      files: recoverableFailedInput.files,
    });
  }, [recoverableFailedInput, threadId]);
  const failedRunRegenerationTarget = useMemo(
    () =>
      runFailureRunId
        ? getLatestRegenerationTarget(thread.messages, runFailureRunId)
        : null,
    [runFailureRunId, thread.messages],
  );
  const canRetryFailedRun = canRetryModelOutputLimit({
    canRun: scope.canRun && !approvalBlocksSending,
    isRunLoading: thread.isLoading,
    hasRegenerationTarget: failedRunRegenerationTarget !== null,
    retrySurfaceAvailable:
      scope.regenerateVisible !== false &&
      !isMock &&
      !isStaticWebsiteOnly() &&
      !isUploading &&
      !agentModelBlocked,
  });
  const handleRetryWithoutThinking = useCallback(async () => {
    if (!canRetryFailedRun || !failedRunRegenerationTarget) {
      return false;
    }
    return regenerateMessage(
      threadId,
      failedRunRegenerationTarget.messageId,
      failedRunRegenerationTarget.supersededMessageIds,
      { withoutThinking: true },
    );
  }, [
    canRetryFailedRun,
    failedRunRegenerationTarget,
    regenerateMessage,
    threadId,
  ]);

  if (threadMissing && missingThreadFallback != null) {
    return missingThreadFallback;
  }

  if (threadMetadataFailed) {
    return (
      <main className="flex min-h-[50vh] flex-col items-center justify-center px-6 text-center">
        <h1 className="text-2xl font-semibold">无法加载这个对话</h1>
        <p className="text-muted-foreground mt-3 text-sm">
          服务暂时不可用，请稍后重试。
        </p>
        <Button
          type="button"
          className="mt-6"
          onClick={() => void threadMetadata.refetch()}
        >
          重试
        </Button>
      </main>
    );
  }

  return (
    <ThreadContext.Provider value={{ thread, isMock }}>
      <OptionalSidecarProvider
        enabled={scope.sidecarVisible !== false}
        parentThreadId={threadId}
        context={settings.context}
      >
        <ChatBox
          threadId={threadId}
          canDeleteFiles={scope.canDeleteFiles === true}
        >
          <ThreadDocumentTitle thread={thread} />
          <div
            className="relative flex size-full min-h-0 justify-between"
            data-chat-content-width={localSettings.appearance.chatContentWidth}
            style={
              {
                "--chat-content-width":
                  CHAT_CONTENT_WIDTH_CSS_VALUES[
                    localSettings.appearance.chatContentWidth
                  ],
              } as React.CSSProperties
            }
          >
            <header
              className={cn(
                "absolute top-0 right-0 left-0 z-30 flex h-12 shrink-0 items-center gap-2 pr-2 pl-12 sm:pr-4 sm:pl-12 xl:pl-4",
                isWelcomeMode
                  ? "bg-background/0 backdrop-blur-none"
                  : "bg-background/80 shadow-xs backdrop-blur",
              )}
            >
              <div className="flex min-w-0 flex-1 items-center gap-2 text-sm font-medium">
                {renderHeaderAccessory?.(threadMetadata.data)}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <TokenUsageIndicator
                  threadId={threadId}
                  backendUsage={backendTokenUsage}
                  enabled={tokenUsageEnabled}
                  messages={thread.messages}
                  pendingMessages={pendingUsageMessages}
                  preferences={localSettings.tokenUsage}
                  onPreferencesChange={(preferences) =>
                    setLocalSettings("tokenUsage", preferences)
                  }
                />
                {scope.sidecarVisible !== false && <SidecarTrigger />}
                <ExportTrigger threadId={threadId} />
                {scope.artifactsVisible !== false && <ArtifactTrigger />}
              </div>
            </header>
            <main className="flex min-h-0 max-w-full grow flex-col">
              <div className="flex min-h-0 flex-1 justify-center">
                <MessageList
                  key={threadId}
                  className={cn("size-full", !isWelcomeMode && "pt-10")}
                  testId="main-message-list"
                  threadId={threadId}
                  thread={thread}
                  activeRunId={activeRunId}
                  runExecutionState={runExecutionState}
                  terminalDisplayLatched={terminalDisplayLatched}
                  initialScroll="instant"
                  resizeScroll={thread.isLoading ? "smooth" : "instant"}
                  paddingBottom={MESSAGE_LIST_DEFAULT_PADDING_BOTTOM}
                  hasMoreHistory={hasMoreHistory}
                  loadMoreHistory={loadMoreHistory}
                  isHistoryLoading={isHistoryLoading}
                  historyError={historyError}
                  retryHistory={retryHistory}
                  suspendLoadingIndicators={approvalDecision !== null}
                  executionApproval={approval}
                  observedExecutionApprovalId={
                    executionApproval.observedApprovalId
                  }
                  tokenUsageInlineMode={tokenUsageInlineMode}
                  canSubmitFeedback={scope.canRun}
                  canDeleteFiles={scope.canDeleteFiles === true}
                  canRegenerate={
                    scope.regenerateVisible !== false &&
                    scope.canRun &&
                    !approvalBlocksSending &&
                    !isMock &&
                    !isStaticWebsiteOnly() &&
                    !isUploading &&
                    !agentModelBlocked &&
                    messageReplayAllowed &&
                    !thread.isLoading
                  }
                  onRegenerateMessage={
                    scope.regenerateVisible === false || !messageReplayAllowed
                      ? undefined
                      : handleRegenerate
                  }
                  canEdit={
                    scope.regenerateVisible !== false &&
                    scope.canRun &&
                    !approvalBlocksSending &&
                    !isMock &&
                    !isStaticWebsiteOnly() &&
                    !isUploading &&
                    !agentModelBlocked &&
                    messageReplayAllowed &&
                    !thread.isLoading &&
                    !branchThread.isPending &&
                    !hasGoal &&
                    !hasOpenHumanInputCard
                  }
                  onEditAndRegenerateMessage={
                    scope.regenerateVisible === false || !messageReplayAllowed
                      ? undefined
                      : handleEditAndRegenerate
                  }
                  onSubmitHumanInput={
                    !scope.canRun ||
                    approvalBlocksSending ||
                    agentModelBlocked ||
                    isMock ||
                    isStaticWebsiteOnly()
                      ? undefined
                      : handleSubmitHumanInput
                  }
                  canBranch={
                    scope.branchVisible !== false &&
                    scope.canCreate &&
                    !isMock &&
                    !isStaticWebsiteOnly() &&
                    !isUploading &&
                    !thread.isLoading &&
                    !branchThread.isPending
                  }
                  onBranchTurn={
                    scope.branchVisible === false ? undefined : handleBranchTurn
                  }
                  trailingContent={
                    approvalDecision ? (
                      <ExecutionApprovalCard
                        approval={approvalDecision}
                        decisionError={executionDecisionError}
                        disabled={
                          !scope.canRun || !scope.canApproveHostExecution
                        }
                        pendingDecision={pendingExecutionDecision}
                        onDecision={
                          scope.canRun && scope.canApproveHostExecution
                            ? handleExecutionApprovalDecision
                            : undefined
                        }
                      />
                    ) : undefined
                  }
                />
              </div>
              <div
                className={cn(
                  "right-0 bottom-0 left-0 z-30 flex justify-center px-3 sm:px-4",
                  isWelcomeMode ? "absolute" : "relative shrink-0 pb-4",
                )}
              >
                <div
                  className={cn(
                    "relative w-full max-w-(--chat-content-width)",
                    isWelcomeMode &&
                      "-translate-y-[calc(50vh-48px)] sm:-translate-y-[calc(50vh-96px)]",
                  )}
                  data-testid="chat-composer-width"
                >
                  {((scope.goalVisible !== false && hasGoal) || hasTodos) && (
                    <div
                      className={cn(
                        "right-0 left-0 z-0",
                        isWelcomeMode ? "absolute -top-4" : "relative",
                      )}
                    >
                      <div
                        className={cn(
                          "right-0 bottom-0 left-0 flex flex-col",
                          isWelcomeMode ? "absolute" : "relative",
                        )}
                      >
                        {scope.goalVisible !== false && activeGoal && (
                          <GoalStatus goal={activeGoal} />
                        )}
                        {hasTodos && (
                          <TodoList
                            className="bg-background/5"
                            todos={thread.values.todos ?? []}
                            hidden={false}
                          />
                        )}
                      </div>
                    </div>
                  )}
                  <RunControlProgress observations={runControlObservations} />
                  {hasRunFailure && (
                    <RunFailureAlert
                      failureCode={runFailureCode}
                      retryDisabled={!canRetryFailedRun}
                      onRetryWithoutThinking={handleRetryWithoutThinking}
                      onRestoreInput={
                        recoverableFailedInput
                          ? handleRestoreFailedInput
                          : undefined
                      }
                    />
                  )}
                  {agentArchived && (
                    <AgentArchivedAlert
                      onStartNewChat={
                        onStartNewAgentChat
                          ? () => onStartNewAgentChat(threadMetadata.data)
                          : undefined
                      }
                    />
                  )}
                  {agentModelUnavailable && (
                    <Alert
                      variant="destructive"
                      className="border-destructive/30 bg-destructive/5 mb-3"
                      data-testid="agent-model-unavailable-alert"
                    >
                      <AlertTitle>
                        {t.conversation.agentModelUnavailableTitle}
                      </AlertTitle>
                      <AlertDescription className="flex items-center justify-between gap-3">
                        <span>
                          {t.conversation.agentModelUnavailableDescription}
                        </span>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={handleAgentModelRetry}
                        >
                          {t.common.retry}
                        </Button>
                      </AlertDescription>
                    </Alert>
                  )}
                  {agentSuspended && (
                    <Alert
                      variant="destructive"
                      className="border-destructive/30 bg-destructive/5 mb-3"
                      data-testid="agent-suspended-alert"
                    >
                      <AlertTitle>
                        {t.conversation.agentSuspendedTitle}
                      </AlertTitle>
                      <AlertDescription className="flex items-center justify-between gap-3">
                        <span>{t.conversation.agentSuspendedDescription}</span>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={handleAgentModelRetry}
                        >
                          {t.common.retry}
                        </Button>
                      </AlertDescription>
                    </Alert>
                  )}
                  {mountedRef.current ? (
                    <InputBox
                      className={cn(
                        "bg-background/5 w-full",
                        isWelcomeMode && "-translate-y-2 sm:-translate-y-4",
                      )}
                      isWelcomeMode={isWelcomeMode}
                      threadId={threadId}
                      threadExists
                      agentMetadata={threadMetadata.data?.metadata}
                      agentModelRef={agentModel.modelRef}
                      draftConversationScope={threadId}
                      restoreRequest={
                        composerRestoreRequest?.threadId === threadId
                          ? composerRestoreRequest
                          : null
                      }
                      autoFocus={isWelcomeMode}
                      status={
                        isUploading
                          ? "submitted"
                          : thread.error
                            ? "error"
                            : thread.isLoading
                              ? "streaming"
                              : "ready"
                      }
                      context={settings.context}
                      extraHeader={
                        isWelcomeMode &&
                        !hasGoal &&
                        !hasTodos && <Welcome mode={settings.context.mode} />
                      }
                      disabled={
                        !scope.canRun ||
                        isMock ||
                        isStaticWebsiteOnly() ||
                        isUploading ||
                        agentModelBlocked ||
                        isHistoryLoading ||
                        approvalBlocksSending
                      }
                      onContextChange={(context) =>
                        setSettings("context", context)
                      }
                      goalCommandsEnabled={scope.goalVisible !== false}
                      compactCommandEnabled={scope.compactVisible !== false}
                      memoryRoutePath={
                        scope.threadBasePath.endsWith("/chats")
                          ? `${scope.threadBasePath.slice(0, -"/chats".length)}/memory`
                          : undefined
                      }
                      uploadsEnabled={scope.canUpload}
                      attachmentUploadStatuses={attachmentUploadStatuses}
                      onDiscardAttachment={discardAttachment}
                      onPrepareAttachments={prepareAttachments}
                      followupSuggestionsEnabled={
                        scope.followupSuggestionsEnabled !== false
                      }
                      onGoalChange={
                        scope.goalVisible !== false ? setLocalGoal : undefined
                      }
                      onSubmit={handleSubmit}
                      onStop={handleStop}
                    />
                  ) : (
                    <div
                      aria-hidden="true"
                      className={cn(
                        "bg-background/5 h-32 w-full rounded-2xl",
                        isWelcomeMode && "-translate-y-2 sm:-translate-y-4",
                      )}
                    />
                  )}
                  {isStaticWebsiteOnly() && (
                    <div className="text-muted-foreground/67 w-full translate-y-12 text-center text-xs">
                      {t.common.notAvailableInDemoMode}
                    </div>
                  )}
                </div>
              </div>
            </main>
          </div>
        </ChatBox>
      </OptionalSidecarProvider>
    </ThreadContext.Provider>
  );
}
