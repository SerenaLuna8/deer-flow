"use client";

import type { Message } from "@langchain/langgraph-sdk";
import type { ChatStatus } from "ai";
import {
  CheckIcon,
  Loader2Icon,
  SparklesIcon,
  Undo2Icon,
  XIcon,
} from "lucide-react";
import { useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { toast } from "sonner";

import {
  PromptInput,
  PromptInputAttachment,
  PromptInputAttachments,
  PromptInputButton,
  PromptInputFooter,
  PromptInputHeader,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
  getPromptInputEnterAction,
  usePromptInputController,
  type PromptInputMessage,
} from "@/components/ai-elements/prompt-input";
import { Button } from "@/components/ui/button";
import { fetch } from "@/core/api/fetcher";
import { useI18n } from "@/core/i18n/hooks";
import { polishInputDraft } from "@/core/input-polish/api";
import { hasOpenHumanInputRequest } from "@/core/messages/human-input";
import { isHiddenFromUIMessage } from "@/core/messages/utils";
import { useModels } from "@/core/models/hooks";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { useProjectRuntimeSlashSkills } from "@/core/shared-assets";
import { buildReferenceMessageMetadata } from "@/core/sidecar";
import { useSuggestionsConfig } from "@/core/suggestions/hooks";
import type { AgentThreadContext, GoalState } from "@/core/threads";
import {
  resolveAgentExecutionModelSelection,
  resolveAgentMode,
  type AgentMode,
} from "@/core/threads/agent-mode";
import {
  buildComposerDraftKey,
  clearComposerDraft,
  getSessionComposerDraftStorage,
  readComposerDraft,
  resolveComposerDraft,
  writeComposerDraft,
  type ComposerDraft,
} from "@/core/threads/composer-draft";
import { useThreadContextUsage } from "@/core/threads/hooks";
import { textOfMessage } from "@/core/threads/utils";
import {
  formatUploadSize,
  splitUnsupportedUploadFiles,
  useUploadLimits,
  validateUploadLimits,
  type UploadLimitViolation,
} from "@/core/uploads";
import { isIMEComposing } from "@/lib/ime";
import { cn } from "@/lib/utils";

import { ContextWindowIndicator } from "./context-window-indicator";
import {
  AddAttachmentsButton,
  SuggestionList,
  VoiceInputButton,
} from "./input-box-controls";
import {
  DreamRestoreConfirmDialog,
  FollowupConfirmDialog,
} from "./input-box-dialogs";
import {
  focusContentEditableEnd,
  insertPlainTextAtSelection,
} from "./input-box-dom";
import { FollowupSuggestions } from "./input-box-followups";
import {
  canPolishInput,
  completeLatestCheckpointContinuation,
  createLatestCheckpointContinuationState,
  findSuggestionTemplatePlaceholder,
  getInputSubmitAction,
  getLeadingSlashSkillQuery,
  getMatchingSkillSuggestions,
  isAbortError,
  markLatestCheckpointContinuation,
  resetLatestCheckpointContinuation,
  shouldContinueFromLatestCheckpoint,
  type SlashSuggestion,
} from "./input-box-helpers";
import { InputBoxModeChooser } from "./input-box-mode-chooser";
import { InputBoxModelChooser } from "./input-box-model-chooser";
import { buildHiddenConversationQuoteMessage } from "./input-box-quote";
import { SlashSkillSuggestionsListbox } from "./input-box-skill-suggestions";
import { memoryDreamPreparationCanCancel } from "./memory-dream-preparation-view-model";
import { useThread } from "./messages/context";
import { ReferenceAttachmentSummary, useMaybeSidecar } from "./sidecar";
import { SlashSkillChip } from "./slash-skill-chip";
import { Tooltip } from "./tooltip";
import { useInputBoxCommands } from "./use-input-box-commands";
import { useInputBoxVoice } from "./use-input-box-voice";

const COMPOSER_DRAFT_SAVE_DELAY_MS = 300;

export type InputBoxSubmitOptions = {
  additionalKwargs?: Record<string, unknown>;
  additionalInputMessages?: Message[];
  continueFromLatestCheckpoint?: boolean;
  onSent?: () => void;
};

export function InputBox({
  className,
  disabled,
  autoFocus,
  status = "ready",
  context,
  extraHeader,
  isWelcomeMode,
  threadId,
  threadExists = true,
  agentMetadata,
  agentModelRef,
  draftConversationScope = threadId,
  initialValue,
  onContextChange,
  onFollowupsVisibilityChange,
  onGoalChange,
  goalCommandsEnabled = true,
  compactCommandEnabled = true,
  memoryRoutePath,
  uploadsEnabled = true,
  followupSuggestionsEnabled = true,
  onSubmit,
  onStop,
  ...props
}: Omit<ComponentProps<typeof PromptInput>, "onSubmit"> & {
  assistantId?: string | null;
  status?: ChatStatus;
  disabled?: boolean;
  context: Omit<
    AgentThreadContext,
    | "thread_id"
    | "is_plan_mode"
    | "thinking_enabled"
    | "subagent_enabled"
    | "reasoning_effort"
  > & {
    model_name?: string;
    model_selection_explicit?: boolean;
    mode: AgentMode | undefined;
    mode_selection_explicit?: boolean;
  };
  extraHeader?: React.ReactNode;
  /**
   * Whether to render the input in welcome layout (vertically centered,
   * with hero + quick action suggestions).  This is purely a visual flag,
   * decoupled from "the backend has created the thread" — see issue #2746.
   */
  isWelcomeMode?: boolean;
  threadId: string;
  threadExists?: boolean;
  agentMetadata?: Record<string, unknown> | null;
  agentModelRef?: string | null;
  draftConversationScope?: string;
  initialValue?: string;
  onContextChange?: (
    context: Omit<
      AgentThreadContext,
      | "thread_id"
      | "is_plan_mode"
      | "thinking_enabled"
      | "subagent_enabled"
      | "reasoning_effort"
    > & {
      model_name?: string;
      model_selection_explicit?: boolean;
      mode: AgentMode | undefined;
      mode_selection_explicit?: boolean;
    },
  ) => void;
  onFollowupsVisibilityChange?: (visible: boolean) => void;
  onGoalChange?: (goal: GoalState | null) => void;
  goalCommandsEnabled?: boolean;
  compactCommandEnabled?: boolean;
  memoryRoutePath?: string;
  uploadsEnabled?: boolean;
  followupSuggestionsEnabled?: boolean;
  onSubmit?: (
    message: PromptInputMessage,
    options?: InputBoxSubmitOptions,
  ) => void | Promise<void>;
  onStop?: () => void;
}) {
  const { locale, t } = useI18n();
  const searchParams = useSearchParams();
  const [modelDialogOpen, setModelDialogOpen] = useState(false);
  const { models } = useModels();
  const { thread, isMock } = useThread();
  const privateWork = usePrivateWorkAccess();
  const contextUsage = useThreadContextUsage(
    threadExists && !isMock ? threadId : undefined,
    {
      enabled: compactCommandEnabled && threadExists && !isMock,
      privateWork,
    },
  );
  const { attachments, textInput } = usePromptInputController();
  const setTextInput = textInput.setInput;
  const sidecar = useMaybeSidecar();
  const attachmentParts = attachments.files;
  const removeAttachment = attachments.remove;
  const { skills, isLoading: skillsLoading } =
    useProjectRuntimeSlashSkills(agentMetadata);
  const { data: uploadLimits } = useUploadLimits(threadId);
  const draftKey = useMemo(
    () =>
      buildComposerDraftKey({
        accountId: privateWork.scope.accountId,
        projectId: privateWork.scope.projectId,
        agentName:
          typeof context.agent_name === "string" ? context.agent_name : null,
        conversationScope: draftConversationScope,
      }),
    [
      context.agent_name,
      draftConversationScope,
      privateWork.scope.accountId,
      privateWork.scope.projectId,
    ],
  );
  const promptRootRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const inlineSkillTextRef = useRef<HTMLSpanElement | null>(null);
  const inlineSkillComposingRef = useRef(false);
  const latestCheckpointContinuationRef = useRef(
    createLatestCheckpointContinuationState(),
  );
  const inputPolishRequestRef = useRef<{
    controller: AbortController | null;
    sequence: number;
  }>({
    controller: null,
    sequence: 0,
  });
  const promptHistoryIndexRef = useRef<number | null>(null);
  const promptHistoryDraftRef = useRef("");
  const latestDraftRef = useRef<{
    key: string;
    draft: ComposerDraft;
  } | null>(null);
  const draftSaveTimerRef = useRef<number | null>(null);
  const commandRequestsCleanupRef = useRef<() => void>(() => undefined);

  const [followups, setFollowups] = useState<string[]>([]);
  const { data: suggestionsConfig } = useSuggestionsConfig();
  const suggestionsConfigLoaded = suggestionsConfig !== undefined;
  const suggestionsEnabled = suggestionsConfig?.enabled;
  const [followupsHidden, setFollowupsHidden] = useState(false);
  const [followupsLoading, setFollowupsLoading] = useState(false);
  const [polishingInput, setPolishingInput] = useState(false);
  const composerLocked = disabled === true || polishingInput;
  const [inputPolishUndo, setInputPolishUndo] = useState<{
    originalText: string;
    rewrittenText: string;
  } | null>(null);
  const [textareaFocused, setTextareaFocused] = useState(false);
  const [skillSuggestionIndex, setSkillSuggestionIndex] = useState(0);
  const [selectedSlashSkill, setSelectedSlashSkill] =
    useState<SlashSuggestion | null>(null);
  const [hydratedDraftKey, setHydratedDraftKey] = useState<string | null>(null);
  const [dismissedSkillSuggestionValue, setDismissedSkillSuggestionValue] =
    useState<string | null>(null);
  const clearMemoryCommandInput = useCallback(() => {
    promptHistoryIndexRef.current = null;
    promptHistoryDraftRef.current = "";
    latestDraftRef.current = null;
    if (draftSaveTimerRef.current !== null) {
      window.clearTimeout(draftSaveTimerRef.current);
      draftSaveTimerRef.current = null;
    }
    clearComposerDraft(getSessionComposerDraftStorage(), draftKey);
    setTextInput("");
    setFollowups([]);
    setFollowupsHidden(false);
    setFollowupsLoading(false);
  }, [draftKey, setTextInput]);
  const markLatestCheckpoint = useCallback(() => {
    markLatestCheckpointContinuation(
      latestCheckpointContinuationRef.current,
      threadId,
    );
  }, [threadId]);
  const latestAiId = useMemo(() => {
    const id = [...thread.messages]
      .reverse()
      .find((message) => message.type === "ai")?.id;
    return typeof id === "string" && id ? id : null;
  }, [thread.messages]);
  const lastGeneratedForAiIdRef = useRef<string | null>(null);
  const wasStreamingRef = useRef(false);
  const pendingFollowupRunRef = useRef<{ baseAiId: string | null } | null>(
    null,
  );
  const followupScopeKey = `${privateWork.scope.accountId}:${privateWork.scope.projectId}:${threadId}`;
  const followupScopeKeyRef = useRef(followupScopeKey);
  const messagesRef = useRef(thread.messages);

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pendingSuggestion, setPendingSuggestion] = useState<string | null>(
    null,
  );
  const builtinSlashCommands = useMemo<SlashSuggestion[]>(
    () => [
      ...(goalCommandsEnabled
        ? [
            {
              name: "goal" as const,
              description: t.inputBox.goalCommandDescription,
              kind: "builtin" as const,
            },
          ]
        : []),
      ...(compactCommandEnabled
        ? [
            {
              name: "compact" as const,
              description: t.inputBox.compactCommandDescription,
              kind: "builtin" as const,
            },
          ]
        : []),
      {
        name: "dream" as const,
        description: t.inputBox.dreamCommandDescription,
        kind: "builtin" as const,
      },
      {
        name: "dream-log" as const,
        description: t.inputBox.dreamLogCommandDescription,
        kind: "builtin" as const,
      },
      {
        name: "dream-restore" as const,
        description: t.inputBox.dreamRestoreCommandDescription,
        kind: "builtin" as const,
      },
    ],
    [
      compactCommandEnabled,
      goalCommandsEnabled,
      t.inputBox.compactCommandDescription,
      t.inputBox.dreamCommandDescription,
      t.inputBox.dreamLogCommandDescription,
      t.inputBox.dreamRestoreCommandDescription,
      t.inputBox.goalCommandDescription,
    ],
  );

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

  useEffect(() => {
    if (!uploadLimits) {
      return;
    }

    const attachmentEntries = attachmentParts.flatMap((attachment) =>
      attachment.file instanceof File
        ? [{ id: attachment.id, file: attachment.file }]
        : [],
    );
    const validation = validateUploadLimits(
      [],
      attachmentEntries.map(({ file }) => file),
      uploadLimits,
    );
    if (validation.rejected.length === 0) {
      return;
    }

    const rejected = new Set(validation.rejected);
    for (const entry of attachmentEntries) {
      if (rejected.has(entry.file)) {
        removeAttachment(entry.id);
      }
    }
    reportUploadLimitViolations(validation.violations);
  }, [
    attachmentParts,
    removeAttachment,
    reportUploadLimitViolations,
    uploadLimits,
  ]);

  const modelSelection = useMemo(
    () =>
      resolveAgentExecutionModelSelection(
        models,
        context.model_name,
        agentModelRef,
        context.model_selection_explicit === true,
      ),
    [
      agentModelRef,
      context.model_name,
      context.model_selection_explicit,
      models,
    ],
  );
  const selectedModel = modelSelection.model;
  const resolvedModelName = modelSelection.modelName;
  const modelSelectionLocked = modelSelection.modelSelectionLocked;

  useEffect(() => {
    if (models.length === 0) {
      return;
    }
    if (modelSelectionLocked) {
      setModelDialogOpen(false);
      return;
    }
    const currentModel =
      context.model_selection_explicit === true
        ? models.find((m) => m.name === context.model_name)
        : undefined;
    const fallbackModel = modelSelection.model ?? models[0]!;
    const supportsThinking = fallbackModel.supports_thinking ?? false;
    const supportsReasoningEffort =
      fallbackModel.supports_reasoning_effort ?? false;
    const nextModelName = fallbackModel.name;
    const nextMode = resolveAgentMode(
      context.mode_selection_explicit === true ? context.mode : undefined,
      supportsThinking,
      supportsReasoningEffort,
    );
    const nextModelSelectionExplicit =
      currentModel !== undefined && context.model_selection_explicit === true;

    if (
      context.model_name === nextModelName &&
      context.mode === nextMode &&
      context.model_selection_explicit === nextModelSelectionExplicit
    ) {
      return;
    }

    onContextChange?.({
      ...context,
      model_name: nextModelName,
      model_selection_explicit: nextModelSelectionExplicit,
      mode: nextMode,
    });
  }, [
    context,
    modelSelection.model,
    modelSelectionLocked,
    models,
    onContextChange,
  ]);

  const supportThinking = useMemo(
    () => selectedModel?.supports_thinking ?? false,
    [selectedModel],
  );
  const supportReasoningEffort = useMemo(
    () => selectedModel?.supports_reasoning_effort ?? false,
    [selectedModel],
  );
  const effectiveMode = useMemo(
    () =>
      resolveAgentMode(
        context.mode_selection_explicit === true ? context.mode : undefined,
        supportThinking,
        supportReasoningEffort,
      ),
    [
      context.mode,
      context.mode_selection_explicit,
      supportReasoningEffort,
      supportThinking,
    ],
  );
  const modelDisplayName =
    selectedModel?.display_name ?? t.conversation.agentModelUnavailableTitle;

  const enabledSkillNames = useMemo(
    () =>
      new Set(
        skills.filter((skill) => skill.enabled).map((skill) => skill.name),
      ),
    [skills],
  );
  const flushLatestDraft = useCallback(() => {
    if (draftSaveTimerRef.current !== null) {
      window.clearTimeout(draftSaveTimerRef.current);
      draftSaveTimerRef.current = null;
    }
    const latest = latestDraftRef.current;
    if (latest) {
      writeComposerDraft(
        getSessionComposerDraftStorage(),
        latest.key,
        latest.draft,
      );
    }
  }, []);

  const promptHistory = useMemo(() => {
    const history: string[] = [];
    for (const message of thread.messages) {
      if (message.type !== "human") {
        continue;
      }
      const additionalKwargs = message.additional_kwargs;
      if (
        additionalKwargs &&
        typeof additionalKwargs === "object" &&
        Reflect.get(additionalKwargs, "hide_from_ui") === true
      ) {
        continue;
      }
      const text = textOfMessage(message)?.trim();
      if (!text) {
        continue;
      }
      if (history.at(-1) !== text) {
        history.push(text);
      }
    }
    return history;
  }, [thread.messages]);

  useEffect(() => {
    if (skillsLoading || hydratedDraftKey === draftKey) return;
    const saved = readComposerDraft(getSessionComposerDraftStorage(), draftKey);
    if (!saved) {
      if (!textInput.value && initialValue) {
        setTextInput(initialValue);
      }
      setHydratedDraftKey(draftKey);
      return;
    }

    const resolved = resolveComposerDraft(saved, enabledSkillNames);
    setTextInput(resolved.text);
    const restoredSkill = resolved.skillName
      ? skills.find(
          (skill) => skill.enabled && skill.name === resolved.skillName,
        )
      : undefined;
    setSelectedSlashSkill(
      restoredSkill
        ? {
            name: restoredSkill.name,
            description: restoredSkill.description,
            kind: "skill",
          }
        : null,
    );
    setHydratedDraftKey(draftKey);
  }, [
    draftKey,
    enabledSkillNames,
    hydratedDraftKey,
    initialValue,
    skills,
    skillsLoading,
    setTextInput,
    textInput.value,
  ]);

  useEffect(() => {
    if (hydratedDraftKey !== draftKey) return;
    const draft: ComposerDraft = {
      text: textInput.value ?? "",
      skillName:
        selectedSlashSkill?.kind === "skill" ? selectedSlashSkill.name : null,
    };
    latestDraftRef.current = { key: draftKey, draft };
    if (draftSaveTimerRef.current !== null) {
      window.clearTimeout(draftSaveTimerRef.current);
    }
    draftSaveTimerRef.current = window.setTimeout(() => {
      draftSaveTimerRef.current = null;
      writeComposerDraft(getSessionComposerDraftStorage(), draftKey, draft);
    }, COMPOSER_DRAFT_SAVE_DELAY_MS);
  }, [draftKey, hydratedDraftKey, selectedSlashSkill, textInput.value]);

  useEffect(() => {
    const handlePageHide = () => flushLatestDraft();
    window.addEventListener("pagehide", handlePageHide);
    return () => {
      window.removeEventListener("pagehide", handlePageHide);
      flushLatestDraft();
    };
  }, [flushLatestDraft]);

  useEffect(() => {
    resetLatestCheckpointContinuation(latestCheckpointContinuationRef.current);
    return () => commandRequestsCleanupRef.current();
  }, [threadId]);

  const abortInputPolishRequest = useCallback(() => {
    inputPolishRequestRef.current.controller?.abort();
    inputPolishRequestRef.current.controller = null;
    inputPolishRequestRef.current.sequence += 1;
    setPolishingInput(false);
  }, []);

  useEffect(() => {
    return () => abortInputPolishRequest();
  }, [abortInputPolishRequest, threadId]);

  const focusVoiceInput = useCallback(() => {
    if (selectedSlashSkill) {
      focusContentEditableEnd(inlineSkillTextRef.current);
    } else {
      textareaRef.current?.focus();
    }
  }, [selectedSlashSkill]);
  const setVoiceInputText = useCallback(
    (value: string) => {
      textInput.setInput(value);
    },
    [textInput],
  );
  const prepareForVoiceInput = useCallback(() => {
    abortInputPolishRequest();
    setInputPolishUndo(null);
    promptHistoryIndexRef.current = null;
    promptHistoryDraftRef.current = "";
  }, [abortInputPolishRequest]);
  const {
    abort: abortVoiceInput,
    listening: voiceListening,
    supported: voiceInputSupported,
    toggle: toggleVoiceInput,
  } = useInputBoxVoice({
    callbacks: {
      focusInput: focusVoiceInput,
      onBeforeStart: prepareForVoiceInput,
      setInput: setVoiceInputText,
    },
    composerLocked,
    draftKey,
    locale,
    messages: {
      failed: t.inputBox.voiceInputFailed,
      microphoneUnavailable: t.inputBox.voiceInputMicrophoneUnavailable,
      networkError: t.inputBox.voiceInputNetworkError,
      noSpeech: t.inputBox.voiceInputNoSpeech,
      permissionDenied: t.inputBox.voiceInputPermissionDenied,
      unsupported: t.inputBox.voiceInputUnsupported,
      unsupportedLanguage: t.inputBox.voiceInputUnsupportedLanguage,
    },
    text: textInput.value ?? "",
    threadId,
  });

  useLayoutEffect(() => {
    abortVoiceInput();
    flushLatestDraft();
    promptHistoryIndexRef.current = null;
    promptHistoryDraftRef.current = "";
    setTextInput("");
    setSelectedSlashSkill(null);
    setInputPolishUndo(null);
    setHydratedDraftKey(null);
    latestDraftRef.current = null;
  }, [abortVoiceInput, draftKey, flushLatestDraft, setTextInput]);

  useEffect(() => {
    const currentIndex = promptHistoryIndexRef.current;
    if (currentIndex !== null && currentIndex >= promptHistory.length) {
      promptHistoryIndexRef.current = null;
      promptHistoryDraftRef.current = "";
    }
  }, [promptHistory.length]);

  const handleModelSelect = useCallback(
    (model_name: string) => {
      if (disabled || polishingInput || modelSelectionLocked) {
        return;
      }
      const model = models.find((m) => m.name === model_name);
      if (!model) {
        return;
      }
      onContextChange?.({
        ...context,
        model_name,
        model_selection_explicit: true,
        mode: resolveAgentMode(
          context.mode_selection_explicit === true ? context.mode : undefined,
          model.supports_thinking ?? false,
          model.supports_reasoning_effort ?? false,
        ),
      });
      setModelDialogOpen(false);
    },
    [
      disabled,
      onContextChange,
      context,
      models,
      modelSelectionLocked,
      polishingInput,
    ],
  );

  const handleModeSelect = useCallback(
    (mode: AgentMode) => {
      if (disabled || polishingInput) {
        return;
      }
      onContextChange?.({
        ...context,
        mode: resolveAgentMode(mode, supportThinking, supportReasoningEffort),
        mode_selection_explicit: true,
      });
    },
    [
      disabled,
      onContextChange,
      context,
      polishingInput,
      supportThinking,
      supportReasoningEffort,
    ],
  );

  const {
    cleanupCommandRequests,
    confirmDreamRestore,
    dismissDreamRestore,
    dreamPreparation,
    dreamPreparationCancel,
    dreamPreparationCancelling,
    dreamPreparationLabel,
    handleCompactCommand,
    handleDreamCommand,
    handleDreamLogCommand,
    handleDreamRestoreCommand,
    handleGoalCommand,
    pendingDreamRestoreVersion,
    restoringMemoryVersion,
  } = useInputBoxCommands({
    clearMemoryCommandInput,
    compactCommandEnabled,
    isMock,
    markLatestCheckpoint,
    memoryRoutePath,
    onGoalChange,
    privateWork,
    threadExists,
    threadId,
  });
  useLayoutEffect(() => {
    commandRequestsCleanupRef.current = cleanupCommandRequests;
  }, [cleanupCommandRequests]);

  const submitThreadMessage = useCallback(
    (message: PromptInputMessage) => {
      const files = message.files.flatMap((file) =>
        file.file instanceof File ? [file.file] : [],
      );
      const uploadValidation = validateUploadLimits([], files, uploadLimits);
      if (uploadValidation.violations.length > 0) {
        reportUploadLimitViolations(uploadValidation.violations);
        return Promise.reject(new Error("Attachment limits exceeded."));
      }
      const placeholder = findSuggestionTemplatePlaceholder(message.text);
      if (placeholder) {
        toast.warning(t.inputBox.suggestionPlaceholderRequired);
        requestAnimationFrame(() => {
          const textarea = textareaRef.current;
          if (!textarea) {
            return;
          }
          textarea.focus();
          textarea.setSelectionRange(placeholder.start, placeholder.end);
        });
        return Promise.reject(
          new Error("Suggestion template placeholder is unresolved."),
        );
      }
      promptHistoryIndexRef.current = null;
      promptHistoryDraftRef.current = "";
      setInputPolishUndo(null);
      setFollowups([]);
      setFollowupsHidden(false);
      setFollowupsLoading(false);
      const quotes = sidecar?.conversationQuotes ?? [];
      const quoteIds = quotes.map((quote) => quote.id);
      const quoteContexts = quotes.map((quote) => quote.context);
      const continueFromLatestCheckpoint = shouldContinueFromLatestCheckpoint(
        latestCheckpointContinuationRef.current,
        threadId,
      );
      const submitOptions: InputBoxSubmitOptions = {
        continueFromLatestCheckpoint,
        ...(quotes.length
          ? {
              additionalKwargs: buildReferenceMessageMetadata(quoteContexts),
              additionalInputMessages: [
                buildHiddenConversationQuoteMessage({
                  contexts: quoteContexts,
                }),
              ],
            }
          : {}),
        // Clear one-time state only after the stream accepts the send.
        onSent: () => {
          completeLatestCheckpointContinuation(
            latestCheckpointContinuationRef.current,
            threadId,
          );
          latestDraftRef.current = null;
          if (draftSaveTimerRef.current !== null) {
            window.clearTimeout(draftSaveTimerRef.current);
            draftSaveTimerRef.current = null;
          }
          clearComposerDraft(getSessionComposerDraftStorage(), draftKey);
          sidecar?.clearConversationQuotes(quoteIds);
        },
      };
      const submit = () => onSubmit?.(message, submitOptions);

      // Guard against submitting before the initial model auto-selection
      // effect has flushed thread settings to storage/state.
      if (
        !modelSelectionLocked &&
        resolvedModelName &&
        context.model_name !== resolvedModelName
      ) {
        onContextChange?.({
          ...context,
          model_name: resolvedModelName,
          model_selection_explicit: false,
          mode: resolveAgentMode(
            context.mode_selection_explicit === true ? context.mode : undefined,
            selectedModel?.supports_thinking ?? false,
            selectedModel?.supports_reasoning_effort ?? false,
          ),
        });
        return new Promise<void>((resolve, reject) => {
          setTimeout(() => {
            Promise.resolve(submit()).then(resolve).catch(reject);
          }, 0);
        });
      }

      return submit();
    },
    [
      context,
      draftKey,
      onContextChange,
      onSubmit,
      reportUploadLimitViolations,
      resolvedModelName,
      modelSelectionLocked,
      selectedModel?.supports_reasoning_effort,
      selectedModel?.supports_thinking,
      sidecar,
      t.inputBox.suggestionPlaceholderRequired,
      uploadLimits,
      threadId,
    ],
  );

  const submitThreadMessageWithFollowup = useCallback(
    async (message: PromptInputMessage) => {
      const pendingRun = { baseAiId: latestAiId };
      pendingFollowupRunRef.current = pendingRun;
      try {
        await submitThreadMessage(message);
      } catch (error) {
        if (pendingFollowupRunRef.current === pendingRun) {
          pendingFollowupRunRef.current = null;
        }
        throw error;
      }
    },
    [latestAiId, submitThreadMessage],
  );

  const handleSubmit = useCallback(
    async (message: PromptInputMessage) => {
      abortVoiceInput();
      if (status === "streaming") {
        toast.info(t.inputBox.pleaseWaitStreaming);
        return Promise.reject(new Error("streaming"));
      }
      const messageWithSlashSkill = selectedSlashSkill
        ? {
            ...message,
            text: `/${selectedSlashSkill.name} ${message.text}`,
          }
        : message;
      const submitAction = getInputSubmitAction({
        text: messageWithSlashSkill.text,
        fileCount: messageWithSlashSkill.files.length,
        status,
      });
      if (submitAction.kind === "goal" && goalCommandsEnabled) {
        promptHistoryIndexRef.current = null;
        promptHistoryDraftRef.current = "";
        setFollowups([]);
        setFollowupsHidden(false);
        setFollowupsLoading(false);
        const saved = await handleGoalCommand(submitAction.command);
        // Only start a run when a goal was actually saved; status/clear never run.
        if (saved && submitAction.command.kind === "set") {
          return submitThreadMessageWithFollowup({
            ...message,
            text: submitAction.command.objective,
            files: [],
          });
        }
        return;
      }
      if (submitAction.kind === "compact" && compactCommandEnabled) {
        return handleCompactCommand();
      }
      if (submitAction.kind === "dream") {
        return handleDreamCommand();
      }
      if (submitAction.kind === "dream-log") {
        handleDreamLogCommand(submitAction.version);
        return;
      }
      if (submitAction.kind === "dream-restore") {
        handleDreamRestoreCommand(submitAction.version);
        return;
      }
      if (submitAction.kind === "dream-invalid") {
        const errorMessage =
          submitAction.reason === "attachments"
            ? t.inputBox.dreamAttachmentsUnsupported
            : submitAction.command === "dream-log"
              ? t.inputBox.dreamLogInvalidArguments
              : submitAction.command === "dream-restore"
                ? t.inputBox.dreamRestoreInvalidArguments
                : t.inputBox.dreamInvalidArguments;
        toast.error(errorMessage);
        return Promise.reject(new Error(errorMessage));
      }
      if (submitAction.kind === "stop") {
        onStop?.();
        return;
      }
      if (submitAction.kind === "empty") {
        return;
      }
      await submitThreadMessageWithFollowup(messageWithSlashSkill);
      if (selectedSlashSkill) {
        setSelectedSlashSkill(null);
      }
    },
    [
      abortVoiceInput,
      handleCompactCommand,
      handleDreamCommand,
      handleDreamLogCommand,
      handleDreamRestoreCommand,
      handleGoalCommand,
      compactCommandEnabled,
      goalCommandsEnabled,
      onStop,
      selectedSlashSkill,
      status,
      submitThreadMessageWithFollowup,
      t.inputBox.dreamAttachmentsUnsupported,
      t.inputBox.dreamInvalidArguments,
      t.inputBox.dreamLogInvalidArguments,
      t.inputBox.dreamRestoreInvalidArguments,
      t.inputBox.pleaseWaitStreaming,
    ],
  );

  const requestFormSubmit = useCallback(() => {
    const form = promptRootRef.current?.querySelector("form");
    form?.requestSubmit();
  }, []);

  const handleFollowupClick = useCallback(
    (suggestion: string) => {
      if (status === "streaming") {
        return;
      }
      const current = (textInput.value ?? "").trim();
      if (current) {
        setPendingSuggestion(suggestion);
        setConfirmOpen(true);
        return;
      }
      textInput.setInput(suggestion);
      setFollowupsHidden(true);
      setTimeout(() => requestFormSubmit(), 0);
    },
    [requestFormSubmit, status, textInput],
  );

  const confirmReplaceAndSend = useCallback(() => {
    if (!pendingSuggestion) {
      setConfirmOpen(false);
      return;
    }
    textInput.setInput(pendingSuggestion);
    setFollowupsHidden(true);
    setConfirmOpen(false);
    setPendingSuggestion(null);
    setTimeout(() => requestFormSubmit(), 0);
  }, [pendingSuggestion, requestFormSubmit, textInput]);

  const confirmAppendAndSend = useCallback(() => {
    if (!pendingSuggestion) {
      setConfirmOpen(false);
      return;
    }
    const current = (textInput.value ?? "").trim();
    const next = current
      ? `${current}\n${pendingSuggestion}`
      : pendingSuggestion;
    textInput.setInput(next);
    setFollowupsHidden(true);
    setConfirmOpen(false);
    setPendingSuggestion(null);
    setTimeout(() => requestFormSubmit(), 0);
  }, [pendingSuggestion, requestFormSubmit, textInput]);

  const slashSkillQuery = useMemo(
    () => getLeadingSlashSkillQuery(textInput.value ?? ""),
    [textInput.value],
  );
  const skillSuggestions = useMemo(
    () =>
      slashSkillQuery === null
        ? []
        : getMatchingSkillSuggestions(
            skills,
            slashSkillQuery,
            builtinSlashCommands,
          ),
    [builtinSlashCommands, skills, slashSkillQuery],
  );
  const showSkillSuggestions =
    !disabled &&
    textareaFocused &&
    !selectedSlashSkill &&
    slashSkillQuery !== null &&
    skillSuggestions.length > 0 &&
    dismissedSkillSuggestionValue !== textInput.value;
  const isComposerDisabled = disabled === true;
  const isMockThread = isMock === true;
  const hasOpenHumanInputCard = useMemo(
    () =>
      hasOpenHumanInputRequest(
        thread.messages,
        (message) => !isHiddenFromUIMessage(message),
      ),
    [thread.messages],
  );
  const inputPolishUndoAvailable =
    !polishingInput &&
    inputPolishUndo !== null &&
    (textInput.value ?? "") === inputPolishUndo.rewrittenText;
  const inputPolishDisabled =
    isComposerDisabled ||
    isMockThread ||
    hasOpenHumanInputCard ||
    polishingInput ||
    (!inputPolishUndoAvailable &&
      (status === "streaming" ||
        slashSkillQuery !== null ||
        !canPolishInput(textInput.value ?? "")));
  useEffect(() => {
    setSkillSuggestionIndex(0);
  }, [slashSkillQuery, skillSuggestions.length]);

  const applySkillSuggestion = useCallback(
    (suggestion: SlashSuggestion) => {
      abortVoiceInput();
      if (suggestion.kind === "skill") {
        setSelectedSlashSkill(suggestion);
        textInput.setInput("");
        setDismissedSkillSuggestionValue(null);
        requestAnimationFrame(() => {
          focusContentEditableEnd(inlineSkillTextRef.current);
        });
        return;
      }

      const nextValue = `/${suggestion.name} `;
      textInput.setInput(nextValue);
      setDismissedSkillSuggestionValue(nextValue);
      requestAnimationFrame(() => {
        const textarea = textareaRef.current;
        if (!textarea) {
          return;
        }
        textarea.focus();
        textarea.setSelectionRange(nextValue.length, nextValue.length);
      });
    },
    [abortVoiceInput, textInput],
  );

  const handleSkillSuggestionKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      if (!showSkillSuggestions) {
        return;
      }

      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSkillSuggestionIndex(
          (index) => (index + 1) % skillSuggestions.length,
        );
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();
        setSkillSuggestionIndex(
          (index) =>
            (index - 1 + skillSuggestions.length) % skillSuggestions.length,
        );
        return;
      }

      if (event.key === "Enter" || event.key === "Tab") {
        if (event.shiftKey) {
          return;
        }
        event.preventDefault();
        const selectedSkill = skillSuggestions[skillSuggestionIndex];
        if (selectedSkill) {
          applySkillSuggestion(selectedSkill);
        }
        return;
      }

      if (event.key === "Escape") {
        event.preventDefault();
        setDismissedSkillSuggestionValue(textInput.value);
      }
    },
    [
      applySkillSuggestion,
      showSkillSuggestions,
      skillSuggestionIndex,
      skillSuggestions,
      textInput.value,
    ],
  );

  const setPromptHistoryValue = useCallback(
    (value: string) => {
      textInput.setInput(value);
      requestAnimationFrame(() => {
        const textarea = textareaRef.current;
        if (!textarea) {
          return;
        }
        textarea.focus();
        textarea.setSelectionRange(value.length, value.length);
      });
    },
    [textInput],
  );

  const handlePolishInput = useCallback(async () => {
    if (inputPolishDisabled) {
      return;
    }

    const originalText = textInput.value ?? "";
    const controller = new AbortController();
    inputPolishRequestRef.current.controller?.abort();
    const sequence = inputPolishRequestRef.current.sequence + 1;
    inputPolishRequestRef.current = {
      controller,
      sequence,
    };
    setPolishingInput(true);

    try {
      const result = await polishInputDraft(
        privateWork,
        {
          text: originalText,
          locale,
          thread_id: threadId,
        },
        { signal: controller.signal },
      );

      const isCurrentRequest =
        inputPolishRequestRef.current.controller === controller &&
        inputPolishRequestRef.current.sequence === sequence &&
        !controller.signal.aborted;
      if (!isCurrentRequest || (textInput.value ?? "") !== originalText) {
        return;
      }

      const rewrittenText = result.rewritten_text.trim();
      if (!rewrittenText || !result.changed) {
        toast.info(t.inputBox.inputPolishNoChanges);
        return;
      }

      // Applying the rewrite replaces the draft outside the textarea change
      // handler, so clear any in-progress history browse state; otherwise a
      // stale index would let the next ArrowDown overwrite the rewrite.
      promptHistoryIndexRef.current = null;
      promptHistoryDraftRef.current = "";
      setPromptHistoryValue(rewrittenText);
      setInputPolishUndo({
        originalText,
        rewrittenText,
      });
    } catch (error) {
      const isCurrentRequest =
        inputPolishRequestRef.current.controller === controller &&
        inputPolishRequestRef.current.sequence === sequence;
      if (isAbortError(error) || !isCurrentRequest) {
        return;
      }
      toast.error(
        error instanceof Error ? error.message : t.inputBox.inputPolishFailed,
      );
    } finally {
      if (
        inputPolishRequestRef.current.controller === controller &&
        inputPolishRequestRef.current.sequence === sequence
      ) {
        inputPolishRequestRef.current.controller = null;
        setPolishingInput(false);
      }
    }
  }, [
    inputPolishDisabled,
    locale,
    privateWork,
    setPromptHistoryValue,
    t.inputBox.inputPolishFailed,
    t.inputBox.inputPolishNoChanges,
    textInput,
    threadId,
  ]);

  const handleUndoInputPolish = useCallback(() => {
    if (!inputPolishUndoAvailable || inputPolishUndo === null) {
      return;
    }
    promptHistoryIndexRef.current = null;
    promptHistoryDraftRef.current = "";
    setPromptHistoryValue(inputPolishUndo.originalText);
    setInputPolishUndo(null);
  }, [inputPolishUndo, inputPolishUndoAvailable, setPromptHistoryValue]);

  const handlePromptHistoryKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      if (
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        isIMEComposing(event) ||
        selectedSlashSkill ||
        promptHistory.length === 0 ||
        (event.key !== "ArrowUp" && event.key !== "ArrowDown")
      ) {
        return;
      }

      const currentValue = textInput.value ?? "";
      const currentHistoryIndex = promptHistoryIndexRef.current;
      const isBrowsingHistory = currentHistoryIndex !== null;

      if (!isBrowsingHistory && currentValue.length > 0) {
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();
        const nextIndex = isBrowsingHistory
          ? Math.max(currentHistoryIndex - 1, 0)
          : promptHistory.length - 1;
        if (!isBrowsingHistory) {
          promptHistoryDraftRef.current = currentValue;
        }
        promptHistoryIndexRef.current = nextIndex;
        setPromptHistoryValue(promptHistory[nextIndex] ?? "");
        return;
      }

      if (!isBrowsingHistory) {
        return;
      }

      event.preventDefault();
      if (currentHistoryIndex >= promptHistory.length - 1) {
        promptHistoryIndexRef.current = null;
        setPromptHistoryValue(promptHistoryDraftRef.current);
        promptHistoryDraftRef.current = "";
        return;
      }

      const nextIndex = currentHistoryIndex + 1;
      promptHistoryIndexRef.current = nextIndex;
      setPromptHistoryValue(promptHistory[nextIndex] ?? "");
    },
    [promptHistory, selectedSlashSkill, setPromptHistoryValue, textInput.value],
  );

  const handleSelectedSlashSkillKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      if (
        event.key !== "Backspace" ||
        !selectedSlashSkill ||
        textInput.value.length > 0 ||
        isIMEComposing(event)
      ) {
        return;
      }

      event.preventDefault();
      setSelectedSlashSkill(null);
      requestAnimationFrame(() => {
        textareaRef.current?.focus();
      });
    },
    [selectedSlashSkill, textInput.value],
  );

  const handlePromptTextareaKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      handleSkillSuggestionKeyDown(event);
      if (event.defaultPrevented) {
        return;
      }
      handleSelectedSlashSkillKeyDown(event);
      if (event.defaultPrevented) {
        return;
      }
      handlePromptHistoryKeyDown(event);
    },
    [
      handlePromptHistoryKeyDown,
      handleSelectedSlashSkillKeyDown,
      handleSkillSuggestionKeyDown,
    ],
  );

  const handlePromptTextareaChange = useCallback(() => {
    if (voiceListening) {
      abortVoiceInput();
    }
    abortInputPolishRequest();
    setInputPolishUndo(null);
    promptHistoryIndexRef.current = null;
    promptHistoryDraftRef.current = "";
  }, [abortInputPolishRequest, abortVoiceInput, voiceListening]);

  const updateInlineSkillTextInput = useCallback(
    (element: HTMLElement) => {
      if (voiceListening) {
        abortVoiceInput();
      }
      promptHistoryIndexRef.current = null;
      promptHistoryDraftRef.current = "";
      textInput.setInput(element.textContent ?? "");
    },
    [abortVoiceInput, textInput, voiceListening],
  );

  useEffect(() => {
    if (!selectedSlashSkill) {
      return;
    }

    const element = inlineSkillTextRef.current;
    if (element && element.textContent !== textInput.value) {
      element.textContent = textInput.value;
    }
  }, [selectedSlashSkill, textInput.value]);

  const handleInlineSkillInput = useCallback(
    (event: FormEvent<HTMLSpanElement>) => {
      updateInlineSkillTextInput(event.currentTarget);
    },
    [updateInlineSkillTextInput],
  );

  const handleInlineSkillPaste = useCallback(
    (event: ClipboardEvent<HTMLSpanElement>) => {
      const pastedFiles = Array.from(event.clipboardData.items)
        .filter((item) => item.kind === "file")
        .flatMap((item) => {
          const file = item.getAsFile();
          return file ? [file] : [];
        });

      if (pastedFiles.length > 0) {
        event.preventDefault();
        const { accepted, message } = splitUnsupportedUploadFiles(pastedFiles);
        if (message) {
          toast.error(message);
        }
        if (accepted.length > 0) {
          attachments.add(accepted);
        }
        return;
      }

      const text = event.clipboardData.getData("text/plain");
      if (!text) {
        return;
      }

      event.preventDefault();
      if (insertPlainTextAtSelection(event.currentTarget, text)) {
        updateInlineSkillTextInput(event.currentTarget);
      }
    },
    [attachments, updateInlineSkillTextInput],
  );

  const handleInlineSkillKeyDown = useCallback(
    (event: KeyboardEvent<HTMLSpanElement>) => {
      handleSelectedSlashSkillKeyDown(event);
      if (event.defaultPrevented) {
        return;
      }

      const action = getPromptInputEnterAction({
        key: event.key,
        shiftKey: event.shiftKey,
        isComposing: isIMEComposing(event, inlineSkillComposingRef.current),
      });
      if (action === "ignore") {
        return;
      }

      event.preventDefault();

      if (action === "newline") {
        if (insertPlainTextAtSelection(event.currentTarget, "\n")) {
          updateInlineSkillTextInput(event.currentTarget);
        }
        return;
      }

      event.currentTarget.closest("form")?.requestSubmit();
    },
    [handleSelectedSlashSkillKeyDown, updateInlineSkillTextInput],
  );

  const clearSelectedSlashSkill = useCallback(() => {
    setSelectedSlashSkill(null);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });
  }, []);

  const showFollowups =
    !disabled &&
    !isWelcomeMode &&
    !showSkillSuggestions &&
    !selectedSlashSkill &&
    !followupsHidden &&
    (followupsLoading || followups.length > 0);

  useEffect(() => {
    onFollowupsVisibilityChange?.(showFollowups);
  }, [onFollowupsVisibilityChange, showFollowups]);

  useEffect(() => {
    return () => onFollowupsVisibilityChange?.(false);
  }, [onFollowupsVisibilityChange]);

  useEffect(() => {
    messagesRef.current = thread.messages;
  }, [thread.messages]);

  useEffect(() => {
    if (followupScopeKeyRef.current === followupScopeKey) {
      return;
    }
    followupScopeKeyRef.current = followupScopeKey;
    lastGeneratedForAiIdRef.current = null;
    wasStreamingRef.current = false;
    pendingFollowupRunRef.current = null;
    setFollowups([]);
    setFollowupsHidden(false);
    setFollowupsLoading(false);
  }, [followupScopeKey]);

  useEffect(() => {
    const streaming = status === "streaming";
    const wasStreaming = wasStreamingRef.current;
    if (status === "error") {
      wasStreamingRef.current = false;
      pendingFollowupRunRef.current = null;
      return;
    }
    if (streaming) {
      wasStreamingRef.current = true;
      return;
    }
    const pendingRun = pendingFollowupRunRef.current;
    if (!wasStreaming && pendingRun === null) {
      return;
    }
    if (!suggestionsConfigLoaded) {
      return;
    }

    if (!followupSuggestionsEnabled) {
      wasStreamingRef.current = false;
      pendingFollowupRunRef.current = null;
      setFollowups([]);
      setFollowupsLoading(false);
      return;
    }

    if (disabled || isMock) {
      return;
    }

    if (
      !latestAiId ||
      (pendingRun !== null && latestAiId === pendingRun.baseAiId)
    ) {
      return;
    }
    wasStreamingRef.current = false;
    pendingFollowupRunRef.current = null;
    if (latestAiId === lastGeneratedForAiIdRef.current) {
      return;
    }
    lastGeneratedForAiIdRef.current = latestAiId;

    if (!suggestionsEnabled) {
      setFollowups([]);
      return;
    }

    const controller = new AbortController();
    setFollowupsHidden(false);
    setFollowupsLoading(true);
    setFollowups([]);

    fetch(
      `${privateWork.apiBaseURL}/threads/${encodeURIComponent(threadId)}/suggestions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          n: 3,
        }),
        signal: controller.signal,
      },
    )
      .then(async (res) => {
        if (!res.ok) {
          return { suggestions: [] as string[] };
        }
        return (await res.json()) as { suggestions?: string[] };
      })
      .then((data) => {
        const suggestions = (data.suggestions ?? [])
          .map((s) => (typeof s === "string" ? s.trim() : ""))
          .filter((s) => s.length > 0)
          .slice(0, 5);
        setFollowups(suggestions);
      })
      .catch(() => {
        setFollowups([]);
      })
      .finally(() => {
        setFollowupsLoading(false);
      });

    return () => controller.abort();
  }, [
    disabled,
    followupSuggestionsEnabled,
    isMock,
    latestAiId,
    privateWork.apiBaseURL,
    status,
    suggestionsConfigLoaded,
    suggestionsEnabled,
    threadId,
  ]);

  const onSelectPlaceholder = useCallback((newText: string) => {
    const placeholder = findSuggestionTemplatePlaceholder(newText);
    if (placeholder) {
      requestAnimationFrame(() => {
        const textarea = textareaRef.current;
        if (!textarea) return;
        textarea.focus();
        textarea.setSelectionRange(placeholder.start, placeholder.end);
      });
    }
  }, []);

  return (
    <div ref={promptRootRef} className="relative flex min-w-0 flex-col gap-2">
      {showFollowups && (
        <FollowupSuggestions
          closeLabel={t.common.close}
          loading={followupsLoading}
          loadingLabel={t.inputBox.followupLoading}
          suggestions={followups}
          onClose={() => setFollowupsHidden(true)}
          onSelect={handleFollowupClick}
        />
      )}
      {showSkillSuggestions && (
        <SlashSkillSuggestionsListbox
          selectedIndex={skillSuggestionIndex}
          suggestions={skillSuggestions}
          onApply={applySkillSuggestion}
          onHighlight={setSkillSuggestionIndex}
        />
      )}
      <PromptInput
        className={cn(
          "bg-background/85 relative z-10 rounded-2xl backdrop-blur-sm transition-all duration-300 ease-out *:data-[slot='input-group']:rounded-2xl",
          polishingInput &&
            "shadow-primary/10 ring-primary/25 shadow-lg ring-1",
          className,
        )}
        disabled={composerLocked}
        globalDrop
        multiple
        onSubmit={handleSubmit}
        {...props}
      >
        {polishingInput && (
          <div
            aria-hidden="true"
            className="border-primary/30 bg-primary/5 pointer-events-auto absolute inset-0 z-20 animate-pulse cursor-wait rounded-2xl border opacity-80"
          />
        )}
        {extraHeader && (
          <div className="absolute top-0 right-0 left-0 z-10">
            <div className="absolute right-0 bottom-0 left-0 flex items-center justify-center">
              {extraHeader}
            </div>
          </div>
        )}
        <PromptInputHeader className="flex-wrap px-3 pt-3 pb-0 empty:hidden">
          {dreamPreparation && dreamPreparationLabel && (
            <div
              aria-live="polite"
              className="bg-muted/70 text-muted-foreground flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-xs"
              data-testid="dream-preparation-status"
              role="status"
            >
              {dreamPreparation.status === "queued" ||
              dreamPreparation.status === "running" ? (
                <Loader2Icon className="size-3.5 shrink-0 animate-spin" />
              ) : dreamPreparation.status === "succeeded" ? (
                <CheckIcon className="size-3.5 shrink-0" />
              ) : (
                <XIcon className="size-3.5 shrink-0" />
              )}
              <span className="min-w-0 flex-1">
                {dreamPreparationLabel}
                {dreamPreparation.compactedPasses > 0 && (
                  <span className="ml-1">
                    {t.inputBox.dreamPreparationPasses.replace(
                      "{count}",
                      String(dreamPreparation.compactedPasses),
                    )}
                  </span>
                )}
              </span>
              {(dreamPreparation.status === "queued" ||
                dreamPreparation.status === "running") && (
                <Button
                  className="h-6 px-2 text-xs"
                  disabled={
                    dreamPreparationCancelling ||
                    !memoryDreamPreparationCanCancel(dreamPreparation)
                  }
                  onClick={() => {
                    void dreamPreparationCancel().catch((error) => {
                      toast.error(
                        error instanceof Error
                          ? error.message
                          : t.inputBox.dreamPreparationFailed,
                      );
                    });
                  }}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  {dreamPreparation.cancelRequested
                    ? t.inputBox.dreamPreparationCancelRequested
                    : t.inputBox.dreamPreparationCancel}
                </Button>
              )}
            </div>
          )}
          <PromptInputAttachments className="contents p-0">
            {(attachment) => (
              <div className="max-w-60">
                <PromptInputAttachment data={attachment} />
              </div>
            )}
          </PromptInputAttachments>
          {polishingInput && (
            <div
              aria-live="polite"
              className="text-primary bg-primary/10 border-primary/20 relative z-30 flex h-7 items-center gap-1.5 rounded-full border py-0 pr-1 pl-2.5 text-xs font-medium"
              role="status"
            >
              <Loader2Icon className="size-3 animate-spin" />
              {t.inputBox.inputPolishing}
              <button
                aria-label={t.inputBox.inputPolishCancel}
                className="hover:bg-primary/20 focus-visible:ring-primary/40 -mr-0.5 ml-0.5 flex size-5 shrink-0 cursor-pointer items-center justify-center rounded-full transition-colors focus-visible:ring-2 focus-visible:outline-none"
                data-testid="cancel-polish-input-button"
                onClick={abortInputPolishRequest}
                type="button"
              >
                <XIcon className="size-3" />
              </button>
            </div>
          )}
          {sidecar && sidecar.conversationQuotes.length > 0 && (
            <ReferenceAttachmentSummary
              references={sidecar.conversationQuotes}
              testId="conversation-quote-attachment"
              onClear={() => sidecar.clearConversationQuotes()}
            />
          )}
        </PromptInputHeader>
        <div className="min-h-16 w-full min-w-0 px-3 py-3">
          {selectedSlashSkill ? (
            <div
              className="max-h-48 min-h-6 w-full min-w-0 cursor-text overflow-y-auto text-base leading-6 break-all whitespace-pre-wrap md:text-sm"
              onClick={(event) => {
                if (event.target === event.currentTarget) {
                  focusContentEditableEnd(inlineSkillTextRef.current);
                }
              }}
            >
              <SlashSkillChip
                name={selectedSlashSkill.name}
                className="mr-2 max-w-[min(11rem,45%)] align-top"
                onRemove={clearSelectedSlashSkill}
              />
              <span
                aria-label={t.inputBox.placeholder}
                aria-multiline="true"
                contentEditable={!composerLocked}
                data-empty={textInput.value.length === 0}
                data-placeholder={t.inputBox.placeholder}
                data-slot="input-group-control"
                onBlur={() => setTextareaFocused(false)}
                onCompositionEnd={() => {
                  inlineSkillComposingRef.current = false;
                }}
                onCompositionStart={() => {
                  inlineSkillComposingRef.current = true;
                }}
                onFocus={() => setTextareaFocused(true)}
                onInput={handleInlineSkillInput}
                onKeyDown={handleInlineSkillKeyDown}
                onPaste={handleInlineSkillPaste}
                aria-placeholder={t.inputBox.placeholder}
                ref={inlineSkillTextRef}
                role="textbox"
                suppressContentEditableWarning
                className={cn(
                  "outline-none",
                  "before:text-muted-foreground before:pointer-events-none",
                  "data-[empty=true]:before:content-[attr(data-placeholder)]",
                  composerLocked && "cursor-not-allowed opacity-50",
                )}
                tabIndex={composerLocked ? -1 : 0}
              />
            </div>
          ) : (
            <PromptInputTextarea
              className="min-h-6! w-full min-w-0 p-0! leading-6!"
              disabled={composerLocked}
              placeholder={t.inputBox.placeholder}
              autoFocus={autoFocus}
              defaultValue={initialValue}
              onBlur={() => setTextareaFocused(false)}
              onChange={handlePromptTextareaChange}
              onFocus={() => setTextareaFocused(true)}
              onKeyDown={handlePromptTextareaKeyDown}
              ref={textareaRef}
            />
          )}
        </div>
        <PromptInputFooter className="flex flex-wrap gap-2 sm:flex-nowrap">
          <PromptInputTools className="min-w-0 flex-1 flex-wrap">
            {/* TODO: Add more connectors here
          <PromptInputActionMenu>
            <PromptInputActionMenuTrigger className="px-2!" />
            <PromptInputActionMenuContent>
              <PromptInputActionAddAttachments
                label={t.inputBox.addAttachments}
              />
            </PromptInputActionMenuContent>
          </PromptInputActionMenu> */}
            {uploadsEnabled && (
              <AddAttachmentsButton
                className="px-2!"
                disabled={composerLocked}
                uploadLimits={uploadLimits}
              />
            )}
            <VoiceInputButton
              disabled={composerLocked}
              listening={voiceListening}
              supported={voiceInputSupported}
              onToggle={toggleVoiceInput}
            />
            <Tooltip
              content={
                polishingInput
                  ? t.inputBox.inputPolishing
                  : inputPolishUndoAvailable
                    ? t.inputBox.inputPolishUndo
                    : t.inputBox.inputPolish
              }
            >
              <PromptInputButton
                aria-label={
                  inputPolishUndoAvailable
                    ? t.inputBox.inputPolishUndo
                    : t.inputBox.inputPolish
                }
                className="px-2!"
                data-testid="polish-input-button"
                disabled={inputPolishDisabled}
                onClick={
                  inputPolishUndoAvailable
                    ? handleUndoInputPolish
                    : handlePolishInput
                }
              >
                {polishingInput ? (
                  <Loader2Icon className="size-3 animate-spin" />
                ) : inputPolishUndoAvailable ? (
                  <Undo2Icon className="size-3" />
                ) : (
                  <SparklesIcon className="size-3" />
                )}
              </PromptInputButton>
            </Tooltip>
            <InputBoxModeChooser
              disabled={composerLocked}
              labels={t.inputBox}
              mode={effectiveMode}
              supportReasoningEffort={supportReasoningEffort}
              supportThinking={supportThinking}
              onSelect={handleModeSelect}
            />
          </PromptInputTools>
          <PromptInputTools className="min-w-0 justify-end">
            {threadExists && compactCommandEnabled && !isMock && (
              <ContextWindowIndicator
                error={contextUsage.error}
                isLoading={contextUsage.isLoading}
                usage={contextUsage.data}
              />
            )}
            <InputBoxModelChooser
              disabled={composerLocked}
              displayName={modelDisplayName}
              labels={t.inputBox}
              locked={modelSelectionLocked}
              models={models}
              open={modelDialogOpen}
              selectedModelName={context.model_name}
              onOpenChange={setModelDialogOpen}
              onSelect={handleModelSelect}
            />
            <PromptInputSubmit
              className="rounded-full"
              disabled={composerLocked}
              variant="outline"
              status={status}
              onClick={(e) => {
                if (status === "streaming") {
                  e.preventDefault();
                  onStop?.();
                }
              }}
            />
          </PromptInputTools>
        </PromptInputFooter>
      </PromptInput>
      {!isWelcomeMode && (
        <div className="bg-background absolute right-0 -bottom-[17px] left-0 z-0 h-4"></div>
      )}

      {isWelcomeMode &&
        !composerLocked &&
        searchParams.get("mode") !== "skill" &&
        !selectedSlashSkill &&
        !showSkillSuggestions && (
          <div
            className="flex items-center justify-center"
            data-testid="welcome-quick-actions"
          >
            <SuggestionList onSelectPlaceholder={onSelectPlaceholder} />
          </div>
        )}

      <FollowupConfirmDialog
        cancelLabel={t.common.cancel}
        labels={t.inputBox}
        open={confirmOpen}
        onAppend={confirmAppendAndSend}
        onCancel={() => setConfirmOpen(false)}
        onOpenChange={setConfirmOpen}
        onReplace={confirmReplaceAndSend}
      />
      <DreamRestoreConfirmDialog
        cancelLabel={t.common.cancel}
        labels={t.inputBox}
        restoring={restoringMemoryVersion}
        version={pendingDreamRestoreVersion}
        onOpenChange={(open) => {
          if (!open && !restoringMemoryVersion) {
            dismissDreamRestore();
          }
        }}
        onCancel={dismissDreamRestore}
        onConfirm={() => void confirmDreamRestore().catch(() => undefined)}
      />
    </div>
  );
}
