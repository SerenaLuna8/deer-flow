"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { type PromptInputMessage } from "@/components/ai-elements/prompt-input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
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
import {
  SidecarProvider,
  SidecarTrigger,
} from "@/components/workspace/sidecar";
import { ThreadTitle } from "@/components/workspace/thread-title";
import { TodoList } from "@/components/workspace/todo-list";
import { TokenUsageIndicator } from "@/components/workspace/token-usage-indicator";
import { useActiveGoal } from "@/components/workspace/use-active-goal";
import { Welcome } from "@/components/workspace/welcome";
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
import type { ProjectPrivateWorkScope } from "@/core/private-work/types";
import {
  CHAT_CONTENT_WIDTH_CSS_VALUES,
  useLocalSettings,
  useThreadSettings,
} from "@/core/settings";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import type { AgentThread } from "@/core/threads";
import {
  useBranchThread,
  useThreadMetadata,
  useThreadStream,
  useThreadTokenUsage,
} from "@/core/threads/hooks";
import { threadTokenUsageToTokenUsage } from "@/core/threads/token-usage";
import { textOfMessage } from "@/core/threads/utils";
import { cn } from "@/lib/utils";

import { ChatBox, useSpecificChatMode, useThreadChat } from ".";

export interface ChatRouteScope {
  privateWork: ProjectPrivateWorkScope;
  threadBasePath: string;
  newThreadPath: string;
  canCreate: boolean;
  canRun: boolean;
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
  isNewThread,
  isHistoryLoading,
  hasMoreHistory,
  visibleMessageCount,
  dismissed,
}: {
  isNewThread: boolean;
  isHistoryLoading: boolean;
  hasMoreHistory: boolean;
  visibleMessageCount: number;
  dismissed: boolean;
}) {
  if (dismissed) return false;
  if (isNewThread) return true;
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

export function ScopedChatPage({
  scope,
  missingThreadFallback = null,
  renderHeaderAccessory,
}: {
  scope: ScopedChatRouteScope;
  missingThreadFallback?: React.ReactNode;
  renderHeaderAccessory?: (
    thread: AgentThread | null | undefined,
  ) => React.ReactNode;
}) {
  const { t } = useI18n();
  const router = useRouter();
  const { threadId, setThreadId, isNewThread, setIsNewThread } = useThreadChat({
    allowNewThread: scope.canCreate && scope.newThreadPath.endsWith("/new"),
  });
  const privateWork = scope.privateWork;
  const isMock = false;
  // `isNewThread` tracks whether the backend has the thread yet — gates the
  // SDK's history fetch (see issue #2746).  `isWelcomeMode` is the visual
  // welcome layout (centered input, hero, quick actions); we flip it to false
  // the moment the user submits so the UI animates immediately, even though
  // `isNewThread` stays true until the backend actually creates the thread.
  const [isWelcomeMode, setIsWelcomeMode] = useState(isNewThread);
  const welcomeDismissedThreadIdsRef = useRef(new Set<string>());
  const [settings, setSettings] = useThreadSettings(threadId);
  const [localSettings, setLocalSettings] = useLocalSettings();
  const { tokenUsageEnabled } = useModels();
  const threadTokenUsage = useThreadTokenUsage(
    isNewThread || isMock ? undefined : threadId,
    { enabled: tokenUsageEnabled && !isMock, privateWork },
  );
  const threadMetadata = useThreadMetadata(threadId, {
    enabled: !isNewThread && !isMock,
    isMock,
    privateWork,
  });
  const branchThread = useBranchThread(privateWork);
  const backendTokenUsage = threadTokenUsageToTokenUsage(threadTokenUsage.data);
  const mountedRef = useRef(false);
  useSpecificChatMode(scope.newThreadPath.endsWith("/new"));

  useEffect(() => {
    mountedRef.current = true;
  }, []);

  const { showNotification } = useNotification();

  const {
    thread,
    pendingUsageMessages,
    sendMessage,
    regenerateMessage,
    isUploading,
    isHistoryLoading,
    hasMoreHistory,
    loadMoreHistory,
    historyError,
    retryHistory,
    hasTerminalRunFailure,
  } = useThreadStream({
    threadId: isNewThread ? undefined : threadId,
    displayThreadId: threadId,
    context: settings.context,
    isMock,
    privateWork,
    // onSend only animates the UI; do NOT flip `isNewThread` here — the
    // LangGraph SDK eagerly fetches /history the moment it receives a
    // thread id and assumes the thread exists on the backend (issue #2746).
    onSend: () => {
      welcomeDismissedThreadIdsRef.current.add(threadId);
      setIsWelcomeMode(false);
    },
    onStart: (createdThreadId) => {
      welcomeDismissedThreadIdsRef.current.add(createdThreadId);
      // ! Important: Never use next.js router for navigation in this case, otherwise it will cause the thread to re-mount and lose all states. Use native history API instead.
      history.replaceState(
        null,
        "",
        `${scope.threadBasePath}/${createdThreadId}`,
      );
      setThreadId(createdThreadId);
      setIsNewThread(false);
    },
    onFinish: (state) => {
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

  const hasThreadMessages = thread.messages.length > 0;
  const visibleMessageCount = useMemo(
    () =>
      thread.messages.filter((message) => !isHiddenFromUIMessage(message))
        .length,
    [thread.messages],
  );
  const metadataSettled =
    !threadMetadata.isLoading && !threadMetadata.isFetching;
  const historySettled =
    !isHistoryLoading && !hasMoreHistory && historyError === null;
  const hasUsableThreadState = hasThreadMessages || hasMoreHistory;
  const threadMissing =
    !isNewThread &&
    !isMock &&
    threadMetadata.data === null &&
    !threadMetadata.error &&
    metadataSettled &&
    historySettled &&
    !hasUsableThreadState;

  const shouldWelcome = shouldShowThreadWelcome({
    isNewThread,
    isHistoryLoading:
      isHistoryLoading || !metadataSettled || historyError !== null,
    hasMoreHistory,
    visibleMessageCount,
    dismissed: welcomeDismissedThreadIdsRef.current.has(threadId),
  });

  useEffect(() => {
    setIsWelcomeMode(shouldWelcome);
  }, [shouldWelcome]);
  const threadMetadataFailed =
    !isNewThread &&
    !isMock &&
    Boolean(threadMetadata.error) &&
    metadataSettled &&
    historySettled &&
    !hasUsableThreadState;

  useEffect(() => {
    if (threadMissing && missingThreadFallback == null) {
      router.replace(scope.newThreadPath);
    }
  }, [missingThreadFallback, router, scope.newThreadPath, threadMissing]);

  const handleSubmit = useCallback(
    (message: PromptInputMessage, options?: InputBoxSubmitOptions) => {
      if (!scope.canRun || (!scope.canUpload && message.files.length > 0)) {
        return;
      }
      const sendPromise = sendMessage(threadId, message, undefined, options);
      if (message.files.length > 0) {
        return sendPromise;
      }
      void sendPromise;
    },
    [scope.canRun, scope.canUpload, sendMessage, threadId],
  );
  const handleSubmitHumanInput = useCallback(
    async (request: HumanInputRequest, response: HumanInputResponse) => {
      if (!scope.canRun) return false;
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
    [scope.canRun, sendMessage, threadId],
  );
  const handleStop = useCallback(async () => {
    if (!scope.canRun) return;
    await thread.stop();
  }, [scope.canRun, thread]);
  const handleRegenerate = useCallback(
    (messageId: string, supersededMessageIds: string[]) =>
      regenerateMessage(threadId, messageId, supersededMessageIds),
    [regenerateMessage, threadId],
  );
  const handleBranchTurn = useCallback(
    async (messageId: string, messageIds: string[]) => {
      if (
        !scope.branchVisible ||
        isNewThread ||
        isMock ||
        isStaticWebsiteOnly()
      ) {
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
    [branchThread, isMock, isNewThread, router, scope, t, threadId],
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
  const hasRunFailure = Boolean(thread.error) || hasTerminalRunFailure;

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
                <ThreadTitle threadId={threadId} thread={thread} />
                {renderHeaderAccessory?.(threadMetadata.data)}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <TokenUsageIndicator
                  threadId={isNewThread ? undefined : threadId}
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
                  initialScroll="instant"
                  paddingBottom={MESSAGE_LIST_DEFAULT_PADDING_BOTTOM}
                  hasMoreHistory={hasMoreHistory}
                  loadMoreHistory={loadMoreHistory}
                  isHistoryLoading={isHistoryLoading}
                  historyError={historyError}
                  retryHistory={retryHistory}
                  tokenUsageInlineMode={tokenUsageInlineMode}
                  canSubmitFeedback={scope.canRun}
                  canDeleteFiles={scope.canDeleteFiles === true}
                  canRegenerate={
                    scope.regenerateVisible !== false &&
                    scope.canRun &&
                    !isNewThread &&
                    !isMock &&
                    !isStaticWebsiteOnly() &&
                    !isUploading &&
                    !thread.isLoading
                  }
                  onRegenerateMessage={
                    scope.regenerateVisible === false
                      ? undefined
                      : handleRegenerate
                  }
                  onSubmitHumanInput={
                    !scope.canRun || isMock || isStaticWebsiteOnly()
                      ? undefined
                      : handleSubmitHumanInput
                  }
                  canBranch={
                    scope.branchVisible !== false &&
                    scope.canCreate &&
                    !isNewThread &&
                    !isMock &&
                    !isStaticWebsiteOnly() &&
                    !isUploading &&
                    !thread.isLoading &&
                    !branchThread.isPending
                  }
                  onBranchTurn={
                    scope.branchVisible === false ? undefined : handleBranchTurn
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
                  {hasRunFailure && !thread.isLoading && (
                    <Alert
                      variant="destructive"
                      className="border-destructive/30 bg-destructive/5 mb-3"
                      data-testid="run-failure-alert"
                    >
                      <AlertTitle>{t.conversation.runFailedTitle}</AlertTitle>
                      <AlertDescription>
                        {t.conversation.runFailedDescription}
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
                      draftConversationScope={isNewThread ? "new" : threadId}
                      autoFocus={isWelcomeMode}
                      status={
                        thread.error
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
                        hasOpenHumanInputCard ||
                        (!isNewThread && isHistoryLoading)
                      }
                      onContextChange={(context) =>
                        setSettings("context", context)
                      }
                      goalCommandsEnabled={scope.goalVisible !== false}
                      compactCommandEnabled={scope.compactVisible !== false}
                      uploadsEnabled={scope.canUpload}
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
