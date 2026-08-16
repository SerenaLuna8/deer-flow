"use client";

import type { Message } from "@langchain/langgraph-sdk";
import {
  CheckIcon,
  GraduationCapIcon,
  LightbulbIcon,
  MessageSquareTextIcon,
  PaperclipIcon,
  RocketIcon,
  Trash2Icon,
  XIcon,
  ZapIcon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { ConversationEmptyState } from "@/components/ai-elements/conversation";
import {
  PromptInput,
  PromptInputActionMenu,
  PromptInputActionMenuContent,
  PromptInputActionMenuItem,
  PromptInputActionMenuTrigger,
  PromptInputAttachment,
  PromptInputAttachments,
  PromptInputBody,
  PromptInputButton,
  PromptInputFooter,
  PromptInputHeader,
  PromptInputProvider,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
  usePromptInputAttachments,
  type PromptInputMessage,
} from "@/components/ai-elements/prompt-input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenuGroup,
  DropdownMenuLabel,
} from "@/components/ui/dropdown-menu";
import { useI18n } from "@/core/i18n/hooks";
import {
  buildHumanInputResponseText,
  hasOpenHumanInputRequest,
  type HumanInputRequest,
  type HumanInputResponse,
} from "@/core/messages/human-input";
import { isHiddenFromUIMessage } from "@/core/messages/utils";
import { useModels } from "@/core/models/hooks";
import type { Model } from "@/core/models/types";
import { createProjectThread } from "@/core/private-work/api-client";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { useLocalSettings } from "@/core/settings";
import { useThreadAgentModelRef } from "@/core/shared-assets";
import {
  buildParentConversationContext,
  buildReferenceMessageMetadata,
  buildSidecarContextPrompt,
  buildSidecarThreadMetadata,
  awaitAbortableSidecarPreparation,
  canClaimSidecarQueue,
  consumeSidecarQueue,
  createSidecarQueueSettlement,
  settleSidecarQueueSubmission,
  type SidecarIdentity,
  type SidecarQueueSettlement,
  type SidecarQueuedValue,
} from "@/core/sidecar";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import {
  resolveAgentExecutionModelSelection,
  resolveAgentExecutionAvailability,
  resolveAgentMode,
  type AgentMode,
} from "@/core/threads/agent-mode";
import {
  useDeleteThread,
  useThreadMetadata,
  useThreadStream,
  type ThreadStreamOptions,
} from "@/core/threads/hooks";
import {
  formatUploadSize,
  uploadFailureMessage,
  useUploadLimits,
  validateUploadLimits,
  type UploadLimits,
  type UploadLimitViolation,
} from "@/core/uploads";
import { uuid } from "@/core/utils/uuid";
import { cn } from "@/lib/utils";

import { MessageList, MESSAGE_LIST_DEFAULT_PADDING_BOTTOM } from "../messages";
import { useThread as useParentThread } from "../messages/context";
import { ModeHoverGuide } from "../mode-hover-guide";
import {
  ModelSelector,
  ModelSelectorContent,
  ModelSelectorItem,
  ModelSelectorLabel,
  ModelSelectorList,
  ModelSelectorName,
  ModelSelectorTrigger,
} from "../model-selector-popover";
import { Tooltip } from "../tooltip";

import { type SidecarReference, useSidecar } from "./context";
import { ReferenceAttachmentSummary } from "./reference-attachments";

function buildHiddenSidecarContextMessage({
  prompt,
  parentThreadId,
}: {
  prompt: string;
  parentThreadId: string;
}): Message {
  return {
    type: "human",
    content: [{ type: "text", text: prompt }],
    additional_kwargs: {
      hide_from_ui: true,
      sidecar_context: true,
      parent_thread_id: parentThreadId,
    },
  } as Message;
}

function promptMessageFiles(message: PromptInputMessage) {
  return message.files.flatMap((file) =>
    file.file instanceof File ? [file.file] : [],
  );
}

function sidecarAdmissionChangedError(message: string): Error {
  const error = new Error(message);
  error.name = "AbortError";
  return error;
}

type QueuedSidecarSubmit = SidecarQueuedValue<{
  message: PromptInputMessage;
  references: SidecarReference[];
  settlement: SidecarQueueSettlement;
}>;

type PendingSidecarSubmit = Readonly<{
  identity: SidecarIdentity;
  settlement: SidecarQueueSettlement;
  abortController: AbortController;
}>;

export function SidecarPanel({ className }: { className?: string }) {
  const { t } = useI18n();
  const privateWork = usePrivateWorkAccess();
  const sidecar = useSidecar();
  const { thread: parentThread } = useParentThread();
  const [localSettings] = useLocalSettings();
  const modelCatalog = useModels();
  const { models, tokenUsageEnabled } = modelCatalog;
  const [modelDialogOpen, setModelDialogOpen] = useState(false);
  const [creatingThread, setCreatingThread] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const { mutateAsync: deleteThread, isPending: isDeleting } =
    useDeleteThread();
  const [queuedSubmit, setQueuedSubmit] = useState<QueuedSidecarSubmit | null>(
    null,
  );
  const queuedSubmitRef = useRef(queuedSubmit);
  const pendingSubmitRef = useRef<PendingSidecarSubmit | null>(null);
  const mountedRef = useRef(false);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      const error = sidecarAdmissionChangedError(
        "The side conversation closed before admission.",
      );
      pendingSubmitRef.current?.abortController.abort(error);
      pendingSubmitRef.current?.settlement.reject(error);
      pendingSubmitRef.current = null;
      queuedSubmitRef.current?.value.settlement.reject(error);
      queuedSubmitRef.current = null;
    };
  }, []);
  const { data: uploadLimits } = useUploadLimits(
    sidecar.sidecarThreadId ?? sidecar.parentThreadId,
  );
  const agentThreadId = sidecar.sidecarThreadId ?? sidecar.parentThreadId;
  const agentThreadMetadata = useThreadMetadata(agentThreadId, {
    privateWork,
  });
  const agentModel = useThreadAgentModelRef(agentThreadMetadata.data?.metadata);
  const agentExecutionAvailability = resolveAgentExecutionAvailability({
    required: true,
    agentModelRef: agentModel.modelRef,
    agentModelLoading:
      agentThreadMetadata.isLoading ||
      agentThreadMetadata.isFetching ||
      agentModel.isLoading,
    agentModelError: agentThreadMetadata.error ?? agentModel.error,
    models,
    modelsLoading: modelCatalog.isLoading || modelCatalog.isFetching,
    modelsError: modelCatalog.error,
  });
  const agentModelBlocked = agentExecutionAvailability !== "ready";
  const agentModelUnavailable = agentExecutionAvailability === "unavailable";
  const handleAgentModelRetry = useCallback(() => {
    void Promise.all([agentModel.refetch(), modelCatalog.refetch()]);
  }, [agentModel, modelCatalog]);
  const modelSelection = useMemo(
    () =>
      resolveAgentExecutionModelSelection(
        models,
        sidecar.context.model_name,
        agentModel.modelRef,
        sidecar.context.model_selection_explicit === true,
      ),
    [
      agentModel.modelRef,
      models,
      sidecar.context.model_name,
      sidecar.context.model_selection_explicit,
    ],
  );
  const selectedModel = modelSelection.model;

  const supportThinking = selectedModel?.supports_thinking ?? false;
  const supportReasoningEffort =
    selectedModel?.supports_reasoning_effort ?? false;

  const {
    thread,
    boundThreadId,
    sendMessage,
    isUploading,
    isHistoryLoading,
    hasMoreHistory,
    loadMoreHistory,
    historyError,
    retryHistory,
  } = useThreadStream({
    threadId: sidecar.sidecarThreadId ?? undefined,
    displayThreadId: sidecar.sidecarThreadId ?? undefined,
    context: sidecar.context,
    agentModelRef: agentModel.modelRef,
  });

  const referenceCountLabel = useMemo(() => {
    const count = sidecar.activeReferences.length;
    const template =
      count === 1
        ? t.sidecar.selectedTextFragment
        : t.sidecar.selectedTextFragments;
    return template.replace("{count}", String(count));
  }, [
    sidecar.activeReferences.length,
    t.sidecar.selectedTextFragment,
    t.sidecar.selectedTextFragments,
  ]);

  const hasPendingReferences = sidecar.activeReferences.length > 0;
  const hasSidecarThread = Boolean(sidecar.sidecarThreadId);
  const hasOpenHumanInputCard = useMemo(
    () =>
      hasOpenHumanInputRequest(
        thread.messages,
        (message) => !isHiddenFromUIMessage(message),
      ),
    [thread.messages],
  );
  const tokenUsageInlineMode = tokenUsageEnabled
    ? localSettings.tokenUsage.inlineMode
    : "off";
  const disabled =
    (!hasSidecarThread && !hasPendingReferences) ||
    thread.isLoading ||
    creatingThread ||
    Boolean(queuedSubmit) ||
    isUploading ||
    agentModelBlocked ||
    hasOpenHumanInputCard ||
    (hasSidecarThread && isHistoryLoading) ||
    isStaticWebsiteOnly();

  useEffect(() => {
    if (models.length === 0) {
      return;
    }
    if (modelSelection.modelSelectionLocked) {
      setModelDialogOpen(false);
      return;
    }

    const currentModel =
      sidecar.context.model_selection_explicit === true
        ? models.find((model) => model.name === sidecar.context.model_name)
        : undefined;
    const fallbackModel = modelSelection.model ?? models[0]!;
    const nextModelName = fallbackModel.name;
    const nextMode = resolveAgentMode(
      sidecar.context.mode_selection_explicit === true
        ? sidecar.context.mode
        : undefined,
      fallbackModel.supports_thinking ?? false,
      fallbackModel.supports_reasoning_effort ?? false,
    );
    const modeChanged = sidecar.context.mode !== nextMode;
    const nextModelSelectionExplicit =
      currentModel !== undefined &&
      sidecar.context.model_selection_explicit === true;

    if (
      sidecar.context.model_name === nextModelName &&
      !modeChanged &&
      sidecar.context.model_selection_explicit === nextModelSelectionExplicit
    ) {
      return;
    }

    sidecar.setContext({
      ...sidecar.context,
      model_name: nextModelName,
      model_selection_explicit: nextModelSelectionExplicit,
      mode: nextMode,
    });
  }, [
    modelSelection.model,
    modelSelection.modelSelectionLocked,
    models,
    sidecar,
  ]);

  const reportUploadLimitViolations = useCallback(
    (violations: UploadLimitViolation[]) => {
      for (const violation of violations) {
        if (violation.code === "max_file_size") {
          toast.error(
            t.uploads.filesTooLarge(
              violation.files.map((file) => file.name).join(", "),
              formatUploadSize(violation.limit),
            ),
          );
        } else if (violation.code === "max_files") {
          toast.error(
            t.uploads.tooManyFiles(violation.files.length, violation.limit),
          );
        } else if (violation.code === "max_total_size") {
          toast.error(
            t.uploads.totalSizeTooLarge(
              violation.files.length,
              formatUploadSize(violation.limit),
            ),
          );
        } else {
          toast.error(
            t.uploads.projectStorageTooSmall(
              violation.files.length,
              formatUploadSize(violation.limit),
            ),
          );
        }
      }
    },
    [t.uploads],
  );

  const handleModelSelect = useCallback(
    (modelName: string) => {
      if (modelSelection.modelSelectionLocked) {
        return;
      }
      const model = models.find((candidate) => candidate.name === modelName);
      if (!model) {
        return;
      }
      const nextMode = resolveAgentMode(
        sidecar.context.mode_selection_explicit === true
          ? sidecar.context.mode
          : undefined,
        model.supports_thinking ?? false,
        model.supports_reasoning_effort ?? false,
      );
      sidecar.setContext({
        ...sidecar.context,
        model_name: modelName,
        model_selection_explicit: true,
        mode: nextMode,
      });
      setModelDialogOpen(false);
    },
    [modelSelection.modelSelectionLocked, models, sidecar],
  );

  const handleModeSelect = useCallback(
    (mode: AgentMode) => {
      const nextMode = resolveAgentMode(
        mode,
        supportThinking,
        supportReasoningEffort,
      );
      sidecar.setContext({
        ...sidecar.context,
        mode: nextMode,
        mode_selection_explicit: true,
      });
    },
    [sidecar, supportReasoningEffort, supportThinking],
  );

  const ensureSidecarThread = useCallback(
    async (
      identity: SidecarIdentity,
      references: SidecarReference[],
      attempt: PendingSidecarSubmit,
    ): Promise<string | null> => {
      const isCurrentAttempt = () =>
        mountedRef.current &&
        pendingSubmitRef.current === attempt &&
        !attempt.abortController.signal.aborted &&
        sidecar.isIdentityCurrent(identity);
      if (!isCurrentAttempt()) return null;
      if (sidecar.sidecarThreadId) {
        return sidecar.sidecarThreadId;
      }
      const restoredThreadId = await sidecar.restoreSidecarThread({ identity });
      if (!isCurrentAttempt()) return null;
      if (restoredThreadId) {
        return restoredThreadId;
      }
      if (references.length === 0) {
        throw new Error(t.sidecar.noContext);
      }
      if (isCurrentAttempt()) {
        setCreatingThread(true);
      }
      try {
        const contexts = references.map((reference) => reference.context);
        const projectScope = privateWork.scope;
        if (!projectScope) {
          throw new Error("Project side conversation scope is unavailable.");
        }
        const parent = await privateWork.client.threads.get(
          identity.parentThreadId,
        );
        if (!isCurrentAttempt()) return null;
        const agentAssetId = parent.metadata?.agent_asset_id;
        const agentScope = parent.metadata?.agent_scope;
        if (
          typeof agentAssetId !== "string" ||
          (agentScope !== "project" && agentScope !== "system")
        ) {
          throw new Error("Project side conversation Agent is unavailable.");
        }
        const created = await createProjectThread(projectScope, {
          threadId: uuid(),
          agentAssetId,
          agentScope,
          metadata: buildSidecarThreadMetadata(
            identity.parentThreadId,
            contexts,
          ),
        });
        if (!isCurrentAttempt()) return null;
        return sidecar.adoptSidecarThread(identity, created.thread_id)
          ? created.thread_id
          : null;
      } finally {
        if (mountedRef.current && pendingSubmitRef.current === attempt) {
          setCreatingThread(false);
        }
      }
    },
    [privateWork, sidecar, t.sidecar.noContext],
  );

  const submitToSidecarThread = useCallback(
    async (
      threadId: string,
      message: PromptInputMessage,
      references: SidecarReference[],
      onSent?: () => void,
      additionalKwargs?: Record<string, unknown>,
    ) => {
      const contexts = references.map((reference) => reference.context);
      const parentConversation = buildParentConversationContext(
        parentThread.messages,
      );
      const hiddenContextPrompt =
        contexts.length > 0 || parentConversation.length > 0
          ? buildSidecarContextPrompt(contexts, { parentConversation })
          : null;
      await sendMessage(threadId, message, undefined, {
        additionalInputMessages: hiddenContextPrompt
          ? [
              buildHiddenSidecarContextMessage({
                prompt: hiddenContextPrompt,
                parentThreadId: sidecar.parentThreadId,
              }),
            ]
          : [],
        additionalKwargs: {
          sidecar_visible_message: true,
          ...(contexts.length > 0
            ? buildReferenceMessageMetadata(contexts)
            : {}),
          ...additionalKwargs,
        },
        onSent,
      });
    },
    [parentThread.messages, sendMessage, sidecar.parentThreadId],
  );

  const handleSubmitHumanInput = useCallback(
    async (request: HumanInputRequest, response: HumanInputResponse) => {
      if (!sidecar.sidecarThreadId) {
        return false;
      }

      let sent = false;
      const pendingReferences = [...sidecar.activeReferences];
      await submitToSidecarThread(
        sidecar.sidecarThreadId,
        {
          text: buildHumanInputResponseText(request, response),
          files: [],
        },
        pendingReferences,
        () => {
          sent = true;
          if (pendingReferences.length > 0) {
            sidecar.clearActiveReferences();
          }
        },
        {
          hide_from_ui: true,
          human_input_response: response,
        },
      );
      return sent;
    },
    [sidecar, submitToSidecarThread],
  );

  useEffect(() => {
    if (
      queuedSubmit &&
      !canClaimSidecarQueue(queuedSubmitRef.current, queuedSubmit)
    ) {
      return;
    }
    const decision = consumeSidecarQueue({
      currentIdentity: sidecar.identity,
      queued: queuedSubmit,
      sidecarThreadId: sidecar.sidecarThreadId,
      boundThreadId,
    });
    if (decision.action === "drop") {
      decision.queued.value.settlement.reject(
        sidecarAdmissionChangedError(
          "The side conversation changed before admission.",
        ),
      );
      queuedSubmitRef.current = null;
      setQueuedSubmit(null);
      return;
    }
    if (decision.action === "wait" || thread.isLoading) {
      return;
    }

    const nextSubmit = decision.value;
    queuedSubmitRef.current = null;
    setQueuedSubmit(null);
    void settleSidecarQueueSubmission(nextSubmit.settlement, () =>
      submitToSidecarThread(
        boundThreadId!,
        nextSubmit.message,
        nextSubmit.references,
        // Clear references only once the send genuinely proceeds; a send dropped
        // by the in-flight guard leaves them attached instead of losing them.
        () => {
          if (nextSubmit.references.length > 0) {
            sidecar.clearActiveReferences();
          }
        },
      ),
    );
  }, [
    queuedSubmit,
    boundThreadId,
    sidecar,
    sidecar.sidecarThreadId,
    submitToSidecarThread,
    thread.isLoading,
  ]);

  const handleSubmit = useCallback(
    async (message: PromptInputMessage) => {
      const text = message.text.trim();
      const files = promptMessageFiles(message);
      if (!text && message.files.length === 0) {
        return;
      }
      if (disabled) {
        throw new Error("The side conversation cannot admit this message.");
      }
      const uploadValidation = validateUploadLimits([], files, uploadLimits);
      if (uploadValidation.violations.length > 0) {
        reportUploadLimitViolations(uploadValidation.violations);
        return Promise.reject(new Error("Attachment limits exceeded."));
      }

      const pendingReferences = [...sidecar.activeReferences];
      const operationIdentity = sidecar.captureIdentity();
      let pendingAttempt: PendingSidecarSubmit | null = null;
      try {
        if (!sidecar.sidecarThreadId) {
          const settlement = createSidecarQueueSettlement();
          const attempt = {
            identity: operationIdentity,
            settlement,
            abortController: new AbortController(),
          } satisfies PendingSidecarSubmit;
          pendingAttempt = attempt;
          pendingSubmitRef.current = attempt;
          const threadId = await awaitAbortableSidecarPreparation(
            attempt.abortController.signal,
            () =>
              ensureSidecarThread(
                operationIdentity,
                pendingReferences,
                attempt,
              ),
          );
          if (
            !threadId ||
            !mountedRef.current ||
            pendingSubmitRef.current !== attempt ||
            !sidecar.isIdentityCurrent(operationIdentity)
          ) {
            throw sidecarAdmissionChangedError(
              "The side conversation changed before admission.",
            );
          }
          const nextQueuedSubmit = {
            identity: operationIdentity,
            threadId,
            value: { message, references: pendingReferences, settlement },
          } satisfies QueuedSidecarSubmit;
          pendingSubmitRef.current = null;
          pendingAttempt = null;
          queuedSubmitRef.current = nextQueuedSubmit;
          setQueuedSubmit(nextQueuedSubmit);
          await settlement.promise;
          return;
        }

        await submitToSidecarThread(
          sidecar.sidecarThreadId,
          message,
          pendingReferences,
          () => {
            if (pendingReferences.length > 0) {
              sidecar.clearActiveReferences();
            }
          },
        );
      } catch (error) {
        if (pendingAttempt && pendingSubmitRef.current === pendingAttempt) {
          pendingSubmitRef.current = null;
          pendingAttempt.abortController.abort(error);
          pendingAttempt.settlement.reject(error);
        }
        if (!(error instanceof Error && error.name === "AbortError")) {
          toast.error(
            uploadFailureMessage(error, {
              tooLarge: t.uploads.serverTooLarge,
              storageQuotaExceeded: t.uploads.storageQuotaExceeded,
              preflightRejected: t.uploads.preflightRejected,
              fallback: t.sidecar.sendFailed,
            }),
          );
        }
        throw error;
      }
    },
    [
      disabled,
      ensureSidecarThread,
      reportUploadLimitViolations,
      sidecar,
      submitToSidecarThread,
      t.sidecar.sendFailed,
      t.uploads,
      uploadLimits,
    ],
  );

  const discardDraftAndClose = useCallback(() => {
    const identity = sidecar.captureIdentity();
    sidecar.resetSidecar(identity);
    const error = sidecarAdmissionChangedError(
      "The side conversation was closed before admission.",
    );
    pendingSubmitRef.current?.abortController.abort(error);
    pendingSubmitRef.current?.settlement.reject(error);
    pendingSubmitRef.current = null;
    setCreatingThread(false);
    queuedSubmitRef.current?.value.settlement.reject(error);
    queuedSubmitRef.current = null;
    setQueuedSubmit(null);
  }, [sidecar]);

  const handleDelete = useCallback(async () => {
    const threadId = sidecar.sidecarThreadId;
    const deleteIdentity = sidecar.captureIdentity();
    // Guard: the trash button only opens this dialog once a thread exists, so a
    // missing id here means the draft was cleared underneath us — just close.
    if (!threadId) {
      discardDraftAndClose();
      setDeleteDialogOpen(false);
      return;
    }
    try {
      await deleteThread({ threadId });
      sidecar.resetSidecar(deleteIdentity);
      queuedSubmitRef.current?.value.settlement.reject(
        sidecarAdmissionChangedError(
          "The side conversation was deleted before admission.",
        ),
      );
      queuedSubmitRef.current = null;
      setQueuedSubmit(null);
      setDeleteDialogOpen(false);
      toast.success(t.sidecar.deleteSuccess);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : t.sidecar.deleteFailed,
      );
    }
  }, [
    deleteThread,
    discardDraftAndClose,
    sidecar,
    t.sidecar.deleteFailed,
    t.sidecar.deleteSuccess,
  ]);

  return (
    <div
      className={cn("flex size-full min-h-0 flex-col", className)}
      data-testid="sidecar-panel"
    >
      <header className="border-border/70 flex h-12 shrink-0 items-center gap-2 border-b px-3">
        <MessageSquareTextIcon className="text-muted-foreground size-4" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{t.sidecar.title}</div>
          <div className="text-muted-foreground truncate text-xs">
            {sidecar.activeReferences.length > 0
              ? referenceCountLabel
              : sidecar.sidecarThreadId
                ? t.sidecar.continuing
                : t.sidecar.noContext}
          </div>
        </div>
        {hasSidecarThread ? (
          <Tooltip content={t.sidecar.delete}>
            <Button
              aria-label={t.sidecar.delete}
              className="text-muted-foreground hover:text-destructive"
              data-testid="sidecar-delete-button"
              size="icon-sm"
              variant="ghost"
              onClick={() => setDeleteDialogOpen(true)}
            >
              <Trash2Icon />
            </Button>
          </Tooltip>
        ) : (
          // No conversation yet — nothing to delete, so this just discards the
          // draft and closes the panel. A plain X (no confirm) keeps it light.
          <Tooltip content={t.common.close}>
            <Button
              aria-label={t.common.close}
              className="text-muted-foreground hover:text-foreground"
              data-testid="sidecar-close-button"
              size="icon-sm"
              variant="ghost"
              onClick={() => discardDraftAndClose()}
            >
              <XIcon />
            </Button>
          </Tooltip>
        )}
      </header>

      <div className="min-h-0 flex-1">
        {sidecar.sidecarThreadId ? (
          <MessageList
            className="size-full"
            testId="sidecar-message-list"
            threadId={sidecar.sidecarThreadId}
            thread={thread}
            paddingBottom={MESSAGE_LIST_DEFAULT_PADDING_BOTTOM / 2}
            hasMoreHistory={hasMoreHistory}
            loadMoreHistory={loadMoreHistory}
            isHistoryLoading={isHistoryLoading}
            historyError={historyError}
            retryHistory={retryHistory}
            tokenUsageInlineMode={tokenUsageInlineMode}
            sidecarSurface
            initialScroll="instant"
            resizeScroll="instant"
            onSubmitHumanInput={
              isStaticWebsiteOnly() || agentModelBlocked
                ? undefined
                : handleSubmitHumanInput
            }
          />
        ) : (
          <ConversationEmptyState
            icon={<MessageSquareTextIcon className="size-5" />}
            title={t.sidecar.emptyTitle}
            description={t.sidecar.emptyDescription}
          />
        )}
      </div>

      <div className="bg-background/95 shrink-0 px-3 pt-3 pb-4 sm:px-4">
        {agentModelUnavailable && (
          <Alert
            variant="destructive"
            className="border-destructive/30 bg-destructive/5 mb-3"
            data-testid="sidecar-agent-model-unavailable-alert"
          >
            <AlertTitle>{t.conversation.agentModelUnavailableTitle}</AlertTitle>
            <AlertDescription className="flex items-center justify-between gap-3">
              <span>{t.conversation.agentModelUnavailableDescription}</span>
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
        <PromptInputProvider key={sidecar.parentThreadId}>
          <PromptInput
            className="bg-background/85 rounded-2xl backdrop-blur-sm *:data-[slot='input-group']:rounded-2xl"
            disabled={disabled}
            multiple
            onSubmit={handleSubmit}
          >
            <PromptInputHeader className="flex-wrap px-3 pt-3 pb-0 empty:hidden">
              <PromptInputAttachments className="contents p-0">
                {(attachment) => (
                  <div className="max-w-48">
                    <PromptInputAttachment data={attachment} />
                  </div>
                )}
              </PromptInputAttachments>
              {sidecar.activeReferences.length > 0 && (
                <ReferenceAttachmentSummary
                  references={sidecar.activeReferences}
                  testId="sidecar-reference-attachment"
                  onClear={() => sidecar.clearActiveReferences()}
                />
              )}
            </PromptInputHeader>
            <PromptInputBody>
              <PromptInputTextarea
                className="max-h-36 min-h-16 text-sm"
                disabled={disabled}
                placeholder={t.sidecar.placeholder}
              />
            </PromptInputBody>
            <PromptInputFooter className="@container flex flex-nowrap gap-2">
              <PromptInputTools className="min-w-0 flex-1 flex-nowrap overflow-hidden">
                <SidecarAddAttachmentsButton uploadLimits={uploadLimits} />
                <SidecarModeMenu
                  context={sidecar.context}
                  supportReasoningEffort={supportReasoningEffort}
                  supportThinking={supportThinking}
                  onModeSelect={handleModeSelect}
                />
              </PromptInputTools>
              <PromptInputTools className="min-w-0 justify-end">
                <SidecarModelSelector
                  className="max-w-40 min-w-0 sm:max-w-56 @max-[240px]:hidden"
                  context={sidecar.context}
                  models={models}
                  open={modelDialogOpen}
                  selectedModel={selectedModel}
                  modelSelectionLocked={modelSelection.modelSelectionLocked}
                  onModelSelect={handleModelSelect}
                  onOpenChange={setModelDialogOpen}
                />
                <Tooltip content={t.sidecar.send}>
                  <PromptInputSubmit
                    className="rounded-full"
                    disabled={disabled}
                    status={
                      isUploading ||
                      thread.isLoading ||
                      creatingThread ||
                      queuedSubmit
                        ? "submitted"
                        : "ready"
                    }
                    variant="outline"
                  />
                </Tooltip>
              </PromptInputTools>
            </PromptInputFooter>
          </PromptInput>
        </PromptInputProvider>
      </div>

      <Dialog
        open={deleteDialogOpen}
        onOpenChange={(open) => {
          // While the delete is in flight the only way out is the (disabled)
          // Cancel button, so ignore overlay/Esc/close-button dismissals that
          // would otherwise hide the dialog and imply the delete was cancelled.
          if (!open && isDeleting) {
            return;
          }
          setDeleteDialogOpen(open);
        }}
      >
        <DialogContent
          showCloseButton={!isDeleting}
          onEscapeKeyDown={(event) => {
            if (isDeleting) {
              event.preventDefault();
            }
          }}
          onInteractOutside={(event) => {
            if (isDeleting) {
              event.preventDefault();
            }
          }}
        >
          <DialogHeader>
            <DialogTitle>{t.sidecar.delete}</DialogTitle>
            <DialogDescription>{t.sidecar.deleteConfirm}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteDialogOpen(false)}
              disabled={isDeleting}
            >
              {t.common.cancel}
            </Button>
            <Button
              variant="destructive"
              data-testid="sidecar-delete-confirm-button"
              onClick={() => void handleDelete()}
              disabled={isDeleting}
            >
              {isDeleting ? t.common.loading : t.common.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function SidecarAddAttachmentsButton({
  uploadLimits,
}: {
  uploadLimits?: UploadLimits;
}) {
  const { t } = useI18n();
  const attachments = usePromptInputAttachments();
  const tooltipContent = uploadLimits
    ? t.uploads.limitsHint(
        uploadLimits.max_files,
        formatUploadSize(uploadLimits.max_file_size),
        formatUploadSize(uploadLimits.max_total_size),
      )
    : t.inputBox.addAttachments;

  return (
    <Tooltip content={<span className="block max-w-80">{tooltipContent}</span>}>
      <PromptInputButton
        aria-label={t.inputBox.addAttachments}
        className="px-2!"
        data-testid="sidecar-add-attachments-button"
        onClick={() => attachments.openFileDialog()}
      >
        <PaperclipIcon className="size-3" />
      </PromptInputButton>
    </Tooltip>
  );
}

function SidecarModeMenu({
  context,
  supportReasoningEffort,
  supportThinking,
  onModeSelect,
}: {
  context: ThreadStreamOptions["context"];
  supportReasoningEffort: boolean;
  supportThinking: boolean;
  onModeSelect: (mode: AgentMode) => void;
}) {
  const { t } = useI18n();
  const mode = resolveAgentMode(
    context.mode_selection_explicit === true ? context.mode : undefined,
    supportThinking,
    supportReasoningEffort,
  );

  return (
    <PromptInputActionMenu>
      <ModeHoverGuide mode={mode}>
        <PromptInputActionMenuTrigger className="max-w-20 min-w-0 gap-1! px-2!">
          <div>
            {mode === "flash" && <ZapIcon className="size-3" />}
            {mode === "thinking" && <LightbulbIcon className="size-3" />}
            {mode === "pro" && <GraduationCapIcon className="size-3" />}
            {mode === "ultra" && (
              <RocketIcon className="size-3 text-[#dabb5e]" />
            )}
          </div>
          <div
            className={cn(
              "truncate text-xs font-normal",
              mode === "ultra" && "golden-text",
            )}
          >
            {(mode === "flash" && t.inputBox.flashMode) ||
              (mode === "thinking" && t.inputBox.reasoningMode) ||
              (mode === "pro" && t.inputBox.proMode) ||
              (mode === "ultra" && t.inputBox.ultraMode)}
          </div>
        </PromptInputActionMenuTrigger>
      </ModeHoverGuide>
      <PromptInputActionMenuContent className="w-80">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="text-muted-foreground text-xs">
            {t.inputBox.mode}
          </DropdownMenuLabel>
          <PromptInputActionMenuItem
            className={cn(
              mode === "flash"
                ? "text-accent-foreground"
                : "text-muted-foreground/65",
            )}
            onSelect={() => onModeSelect("flash")}
          >
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-1 font-bold">
                <ZapIcon
                  className={cn(
                    "mr-2 size-4",
                    mode === "flash" && "text-accent-foreground",
                  )}
                />
                {t.inputBox.flashMode}
              </div>
              <div className="pl-7 text-xs">
                {t.inputBox.flashModeDescription}
              </div>
            </div>
            {mode === "flash" ? (
              <CheckIcon className="ml-auto size-4" />
            ) : (
              <div className="ml-auto size-4" />
            )}
          </PromptInputActionMenuItem>
          {supportThinking && (
            <>
              <PromptInputActionMenuItem
                className={cn(
                  mode === "thinking"
                    ? "text-accent-foreground"
                    : "text-muted-foreground/65",
                )}
                onSelect={() => onModeSelect("thinking")}
              >
                <div className="flex flex-col gap-2">
                  <div className="flex items-center gap-1 font-bold">
                    <LightbulbIcon
                      className={cn(
                        "mr-2 size-4",
                        mode === "thinking" && "text-accent-foreground",
                      )}
                    />
                    {t.inputBox.reasoningMode}
                  </div>
                  <div className="pl-7 text-xs">
                    {t.inputBox.reasoningModeDescription}
                  </div>
                </div>
                {mode === "thinking" ? (
                  <CheckIcon className="ml-auto size-4" />
                ) : (
                  <div className="ml-auto size-4" />
                )}
              </PromptInputActionMenuItem>
              {supportReasoningEffort && (
                <>
                  <PromptInputActionMenuItem
                    className={cn(
                      mode === "pro"
                        ? "text-accent-foreground"
                        : "text-muted-foreground/65",
                    )}
                    onSelect={() => onModeSelect("pro")}
                  >
                    <div className="flex flex-col gap-2">
                      <div className="flex items-center gap-1 font-bold">
                        <GraduationCapIcon
                          className={cn(
                            "mr-2 size-4",
                            mode === "pro" && "text-accent-foreground",
                          )}
                        />
                        {t.inputBox.proMode}
                      </div>
                      <div className="pl-7 text-xs">
                        {t.inputBox.proModeDescription}
                      </div>
                    </div>
                    {mode === "pro" ? (
                      <CheckIcon className="ml-auto size-4" />
                    ) : (
                      <div className="ml-auto size-4" />
                    )}
                  </PromptInputActionMenuItem>
                  <PromptInputActionMenuItem
                    className={cn(
                      mode === "ultra"
                        ? "text-accent-foreground"
                        : "text-muted-foreground/65",
                    )}
                    onSelect={() => onModeSelect("ultra")}
                  >
                    <div className="flex flex-col gap-2">
                      <div className="flex items-center gap-1 font-bold">
                        <RocketIcon
                          className={cn(
                            "mr-2 size-4",
                            mode === "ultra" && "text-[#dabb5e]",
                          )}
                        />
                        <div className={cn(mode === "ultra" && "golden-text")}>
                          {t.inputBox.ultraMode}
                        </div>
                      </div>
                      <div className="pl-7 text-xs">
                        {t.inputBox.ultraModeDescription}
                      </div>
                    </div>
                    {mode === "ultra" ? (
                      <CheckIcon className="ml-auto size-4" />
                    ) : (
                      <div className="ml-auto size-4" />
                    )}
                  </PromptInputActionMenuItem>
                </>
              )}
            </>
          )}
        </DropdownMenuGroup>
      </PromptInputActionMenuContent>
    </PromptInputActionMenu>
  );
}

function SidecarModelSelector({
  className,
  context,
  models,
  open,
  selectedModel,
  modelSelectionLocked,
  onModelSelect,
  onOpenChange,
}: {
  className?: string;
  context: ThreadStreamOptions["context"];
  models: Model[];
  open: boolean;
  selectedModel?: Model;
  modelSelectionLocked: boolean;
  onModelSelect: (modelName: string) => void;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useI18n();
  const displayName =
    selectedModel?.display_name ?? t.conversation.agentModelUnavailableTitle;

  if (!displayName) {
    return null;
  }

  if (modelSelectionLocked) {
    return (
      <Tooltip content={t.inputBox.agentModelLocked}>
        <PromptInputButton
          className={cn("min-w-0 px-2!", className)}
          data-testid="sidecar-agent-model-locked"
          disabled
        >
          <div className="flex min-w-0 flex-col items-start text-left">
            <ModelSelectorName className="truncate text-xs font-normal">
              {displayName}
            </ModelSelectorName>
          </div>
        </PromptInputButton>
      </Tooltip>
    );
  }

  return (
    <ModelSelector open={open} onOpenChange={onOpenChange}>
      <ModelSelectorTrigger asChild>
        <PromptInputButton className={cn("min-w-0 px-2!", className)}>
          <div className="flex min-w-0 flex-col items-start text-left">
            <ModelSelectorName className="truncate text-xs font-normal">
              {displayName}
            </ModelSelectorName>
          </div>
        </PromptInputButton>
      </ModelSelectorTrigger>
      <ModelSelectorContent>
        <ModelSelectorLabel>{t.inputBox.model}</ModelSelectorLabel>
        <ModelSelectorList>
          {models.map((model) => (
            <ModelSelectorItem
              className={cn(
                model.name === context.model_name
                  ? "text-accent-foreground"
                  : "text-muted-foreground/65",
              )}
              key={model.name}
              onSelect={() => onModelSelect(model.name)}
            >
              <ModelSelectorName className="min-w-0 flex-1 truncate">
                {model.display_name}
              </ModelSelectorName>
              {model.name === context.model_name ? (
                <CheckIcon className="ml-auto size-4" />
              ) : (
                <div className="ml-auto size-4" />
              )}
            </ModelSelectorItem>
          ))}
        </ModelSelectorList>
      </ModelSelectorContent>
    </ModelSelector>
  );
}
