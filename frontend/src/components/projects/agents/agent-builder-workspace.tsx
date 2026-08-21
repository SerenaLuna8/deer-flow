"use client";

import {
  ArrowLeftIcon,
  BotIcon,
  CheckIcon,
  Loader2Icon,
  MoreHorizontalIcon,
  SendIcon,
  SquareIcon,
  Trash2Icon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useEffect,
  Fragment,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type Ref,
} from "react";

import { AgentBuilderActivityBlock } from "@/components/projects/agents/agent-builder-activity";
import type { AgentInstructionField } from "@/components/projects/assets/agent-instructions-workbench";
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { InputBoxModeChooser } from "@/components/workspace/input-box-mode-chooser";
import { HumanInputCard } from "@/components/workspace/messages/human-input-card";
import {
  ModelSelector,
  ModelSelectorContent,
  ModelSelectorItem,
  ModelSelectorLabel,
  ModelSelectorList,
  ModelSelectorName,
  ModelSelectorTrigger,
} from "@/components/workspace/model-selector-popover";
import {
  AgentBuilderApiError,
  agentBuilderCanAuthor,
  agentBuilderCancelActionDisabled,
  agentBuilderCanComplete,
  agentBuilderComposerDisabled,
  agentBuilderSemanticallyEqual,
  agentBuilderSemanticSignature,
  agentBuilderSlugErrorCode,
  createAgentBuilderIdempotencyRegistry,
  normalizeAgentBuilderSlug,
  prepareAgentBuilderCancelSession,
  useAgentBuilderSession,
  useAgentBuilderActivities,
  useCancelAgentBuilderSession,
  useCommitAgentBuilderSession,
  useSetAgentBuilderGenerationPreference,
  useSubmitAgentBuilderTurn,
  useStopAgentBuilderTurn,
  type AgentBuilderBlueprint,
  type AgentBuilderSession,
  type AgentBuilderActivity,
} from "@/core/agent-builder";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import type { HumanInputResponse } from "@/core/messages/human-input";
import { useModels } from "@/core/models/hooks";
import { resolveModelDisplayName } from "@/core/models/presentation";
import type { Model } from "@/core/models/types";
import { SafeStreamdown } from "@/core/streamdown/components";
import {
  getAgentModeExecutionProfile,
  resolveAgentMode,
  type AgentMode,
} from "@/core/threads/agent-mode";
import { isIMEComposing } from "@/lib/ime";

import { useMcpDependencyRuntime } from "../assets/use-mcp-dependency-runtime";
import { useCurrentProject } from "../project-context";

import {
  AgentBuilderBlueprintReview,
  agentBuilderBlueprintValidationMessage,
} from "./agent-builder-blueprint-review";
import { AgentBuilderProgress } from "./agent-builder-progress";
import { agentBuilderErrorMessage } from "./agent-builder-start";

export function agentBuilderSessionPath(
  projectSlug: string,
  sessionId: string,
) {
  return `/projects/${encodeURIComponent(projectSlug)}/agents/new/${encodeURIComponent(sessionId)}`;
}

export function agentBuilderWorkspaceErrorMessage(
  error: unknown,
  copy: Translations["agents"]["builder"]["errors"],
  commitUncertain = false,
): string {
  if (
    commitUncertain &&
    error instanceof AgentBuilderApiError &&
    [
      "AGENT_BUILDER_NETWORK_ERROR",
      "AGENT_BUILDER_RESPONSE_INVALID",
      "AGENT_BUILDER_UNAVAILABLE",
    ].includes(error.code)
  ) {
    return copy.commitUncertain;
  }
  if (
    error instanceof AgentBuilderApiError &&
    error.code === "AGENT_BUILDER_CONFLICT"
  ) {
    return copy.stale;
  }
  return agentBuilderErrorMessage(error, copy);
}

function sameBlueprint(
  left: AgentBuilderBlueprint | null,
  right: AgentBuilderBlueprint | null,
) {
  return agentBuilderSemanticallyEqual(left, right);
}

export function rebaseAgentBuilderBlueprint(
  localDraft: AgentBuilderBlueprint | null,
  serverBlueprint: AgentBuilderBlueprint,
  preserveLocalDraft: boolean,
): {
  baseline: AgentBuilderBlueprint;
  draft: AgentBuilderBlueprint;
} {
  return {
    baseline: serverBlueprint,
    draft: preserveLocalDraft && localDraft ? localDraft : serverBlueprint,
  };
}

export function agentBuilderCommitSlugFields(
  normalizedSlug: string,
  sessionSlug: string,
): { slug?: string } {
  return normalizedSlug === sessionSlug ? {} : { slug: normalizedSlug };
}

export function rebaseAgentBuilderName(
  localDraft: string | null,
  serverSlug: string,
  preserveLocalDraft: boolean,
): { baseline: string; draft: string } {
  return {
    baseline: serverSlug,
    draft: preserveLocalDraft && localDraft !== null ? localDraft : serverSlug,
  };
}

export async function recoverAgentBuilderConflict(
  error: unknown,
  releaseStaleCommand: () => void,
  refetchSession: () => Promise<unknown>,
): Promise<boolean> {
  if (
    !(error instanceof AgentBuilderApiError) ||
    error.code !== "AGENT_BUILDER_CONFLICT"
  ) {
    return false;
  }
  releaseStaleCommand();
  await refetchSession();
  return true;
}

export async function submitAgentBuilderClarificationMutation(
  operation: () => Promise<unknown>,
  onResolved: () => void = () => undefined,
  onRejected: (error: unknown) => void | Promise<void> = () => undefined,
): Promise<boolean> {
  try {
    const response = await operation();
    if (
      typeof response === "object" &&
      response !== null &&
      "data" in response &&
      typeof response.data === "object" &&
      response.data !== null &&
      "status" in response.data &&
      response.data.status === "failed"
    ) {
      onResolved();
      return false;
    }
    onResolved();
    return true;
  } catch (error) {
    await onRejected(error);
    return false;
  }
}

function AgentBuilderMessageBubble({
  role,
  content,
}: {
  role: "user" | "assistant";
  content: string;
}) {
  if (role === "user") {
    return (
      <div className="flex justify-end">
        <div className="bg-muted max-w-[88%] rounded-2xl rounded-br-md px-4 py-3 text-sm leading-6 sm:max-w-[75%]">
          <SafeStreamdown>{content}</SafeStreamdown>
        </div>
      </div>
    );
  }
  return (
    <div className="grid grid-cols-[2.5rem_minmax(0,1fr)] gap-3">
      <span className="bg-muted flex size-10 items-center justify-center rounded-xl">
        <BotIcon aria-hidden className="size-5" />
      </span>
      <div className="min-w-0 pt-1 text-[15px] leading-7">
        <SafeStreamdown>{content}</SafeStreamdown>
      </div>
    </div>
  );
}

export function AgentBuilderConversationView({
  session,
  composerText,
  agentName = session.slug,
  agentSlug = session.slug,
  agentSlugError = null,
  pendingUserMessage = null,
  activities = [],
  canAuthor,
  mutationPending,
  commitPending,
  blueprintEditing,
  blueprintDraft,
  blueprintDirty,
  models = [],
  modelsLoading = false,
  modelsError = null,
  selectedGenerationModelName = null,
  selectedGenerationMode = "pro",
  scrollContainerRef,
  mcpDependencyLoading = false,
  mcpDependencyBlockReason = null,
  selectedField,
  displayMode,
  errorMessage,
  onModelsRetry = () => undefined,
  onGenerationModelChange = () => undefined,
  onGenerationModeChange = () => undefined,
  onComposerTextChange,
  onSubmitMessage,
  onStopGeneration = () => undefined,
  stopPending = false,
  createdAgentHref = null,
  onSubmitClarification,
  onSelectedFieldChange,
  onDisplayModeChange,
  onBlueprintChange,
  onAgentNameChange = () => undefined,
  onBlueprintEdit,
  onBlueprintSave,
  onBlueprintDiscard,
  onComplete,
}: {
  session: AgentBuilderSession;
  composerText: string;
  agentName?: string;
  agentSlug?: string;
  agentSlugError?: string | null;
  pendingUserMessage?: string | null;
  activities?: AgentBuilderActivity[];
  canAuthor: boolean;
  mutationPending: boolean;
  commitPending: boolean;
  blueprintEditing: boolean;
  blueprintDraft: AgentBuilderBlueprint | null;
  blueprintDirty: boolean;
  models?: Model[];
  modelsLoading?: boolean;
  modelsError?: unknown;
  selectedGenerationModelName?: string | null;
  selectedGenerationMode?: AgentMode;
  scrollContainerRef?: Ref<HTMLDivElement>;
  mcpDependencyLoading?: boolean;
  mcpDependencyBlockReason?: string | null;
  selectedField: AgentInstructionField;
  displayMode: "source" | "preview";
  errorMessage: string | null;
  onModelsRetry?: () => void;
  onGenerationModelChange?: (modelName: string) => void;
  onGenerationModeChange?: (mode: AgentMode) => void;
  onComposerTextChange: (value: string) => void;
  onSubmitMessage: () => void;
  onStopGeneration?: () => void;
  stopPending?: boolean;
  createdAgentHref?: string | null;
  onSubmitClarification: (
    response: HumanInputResponse,
  ) => boolean | void | Promise<boolean | void>;
  onSelectedFieldChange: (field: AgentInstructionField) => void;
  onDisplayModeChange: (mode: "source" | "preview") => void;
  onBlueprintChange: (blueprint: AgentBuilderBlueprint) => void;
  onAgentNameChange?: (value: string) => void;
  onBlueprintEdit: () => void;
  onBlueprintSave: () => void;
  onBlueprintDiscard: () => void;
  onComplete: () => void;
}) {
  const { t } = useI18n();
  const copy = t.agents.builder.conversation;
  const localDraftLocked = blueprintEditing || blueprintDirty;
  const selectedGenerationModel = models.find(
    (model) => model.name === selectedGenerationModelName,
  );
  const generating =
    Boolean(pendingUserMessage) || session.status === "generating";
  const processing = generating || session.status === "committing";
  const modelUnavailable =
    modelsLoading || Boolean(modelsError) || !selectedGenerationModel;
  const composerDisabled =
    agentBuilderComposerDisabled(session, mutationPending, localDraftLocked) ||
    !canAuthor ||
    modelUnavailable;
  const modelSelectorDisabled =
    !canAuthor ||
    processing ||
    session.status === "completed" ||
    mutationPending ||
    localDraftLocked ||
    modelUnavailable;
  const clarificationOpen = session.active_clarifications.length > 0;
  const activitiesByOperation = new Map<string, AgentBuilderActivity[]>();
  for (const activity of activities) {
    const operationActivities =
      activitiesByOperation.get(activity.operation_id) ?? [];
    operationActivities.push(activity);
    activitiesByOperation.set(activity.operation_id, operationActivities);
  }
  const messageOperationIds = new Set(
    session.messages.flatMap((message) =>
      message.operation_id ? [message.operation_id] : [],
    ),
  );

  function submitMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmitMessage();
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        ref={scrollContainerRef}
        data-agent-builder-scroll-region
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
      >
        <div className="mx-auto w-full max-w-4xl px-4 pt-7 pb-6 sm:px-6 sm:pt-10">
          <div className="space-y-7">
            <AgentBuilderProgress
              items={session.progress}
              generating={processing}
              documentsReady={Boolean(session.blueprint)}
            />

            {!canAuthor ? (
              <p
                role="alert"
                className="border-border/70 bg-muted/20 text-muted-foreground rounded-xl border px-4 py-3 text-sm"
              >
                {copy.permissionReadOnly}
              </p>
            ) : null}

            {session.messages.length === 0 ? (
              <div className="flex justify-end">
                <p className="bg-muted max-w-[90%] rounded-2xl rounded-br-md px-4 py-3 text-sm leading-6">
                  {copy.newAgentIntro(session.display_name)}
                </p>
              </div>
            ) : null}

            {session.messages.map((message) => (
              <Fragment key={message.id}>
                <AgentBuilderMessageBubble
                  role={message.role}
                  content={message.content}
                />
                {message.role === "user" &&
                message.operation_id &&
                activitiesByOperation.has(message.operation_id) ? (
                  <AgentBuilderActivityBlock
                    activities={
                      activitiesByOperation.get(message.operation_id) ?? []
                    }
                  />
                ) : null}
              </Fragment>
            ))}

            {pendingUserMessage ? (
              <AgentBuilderMessageBubble
                role="user"
                content={pendingUserMessage}
              />
            ) : null}

            {[...activitiesByOperation.entries()]
              .filter(([operationId]) => !messageOperationIds.has(operationId))
              .map(([operationId, operationActivities]) => (
                <AgentBuilderActivityBlock
                  key={operationId}
                  activities={operationActivities}
                />
              ))}

            {processing ? (
              <p
                role="status"
                className="text-muted-foreground flex items-center gap-2 text-xs"
              >
                <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
                {session.status === "committing"
                  ? copy.creatingAgent
                  : copy.designingAgent}
              </p>
            ) : null}

            {session.status === "completed" && createdAgentHref ? (
              <div className="flex justify-start sm:pl-13">
                <Button asChild variant="outline">
                  <Link href={createdAgentHref}>{copy.viewAgent}</Link>
                </Button>
              </div>
            ) : null}

            {session.active_clarification ? (
              <div className="w-full">
                <HumanInputCard
                  request={session.active_clarification}
                  disabled={!canAuthor || localDraftLocked || modelUnavailable}
                  pending={mutationPending}
                  onSubmit={(response) => onSubmitClarification(response)}
                />
              </div>
            ) : null}

            {session.status === "failed" && session.error_message ? (
              <div
                role="alert"
                className="border-destructive/30 bg-destructive/5 text-destructive rounded-xl border p-4 text-sm"
              >
                {session.error_message}
              </div>
            ) : null}

            {blueprintDraft ? (
              <AgentBuilderBlueprintReview
                blueprint={blueprintDraft}
                agentName={agentName}
                agentSlug={agentSlug}
                agentSlugError={agentSlugError}
                models={models}
                assumptions={session.assumptions}
                conflicts={session.conflicts}
                modelsLoading={modelsLoading}
                modelsError={modelsError}
                canAuthor={canAuthor}
                editing={blueprintEditing}
                pending={mutationPending}
                creating={commitPending}
                dirty={blueprintDirty}
                canCreate={agentBuilderCanComplete(session)}
                mcpDependencyLoading={mcpDependencyLoading}
                mcpDependencyBlockReason={mcpDependencyBlockReason}
                selectedField={selectedField}
                displayMode={displayMode}
                errorMessage={errorMessage}
                onSelectedFieldChange={onSelectedFieldChange}
                onDisplayModeChange={onDisplayModeChange}
                onBlueprintChange={onBlueprintChange}
                onAgentNameChange={onAgentNameChange}
                onEdit={onBlueprintEdit}
                onSave={onBlueprintSave}
                onDiscard={onBlueprintDiscard}
                onCreate={onComplete}
              />
            ) : null}

            {errorMessage && !blueprintDraft ? (
              <p role="alert" className="text-destructive text-sm">
                {errorMessage}
              </p>
            ) : null}
          </div>
        </div>
      </div>

      {canAuthor ? (
        <div
          data-agent-builder-composer-shell
          className="bg-background border-border/70 shrink-0 border-t px-4 py-3 sm:px-6"
        >
          <form
            className="bg-background border-border/70 mx-auto w-full max-w-4xl rounded-2xl border p-2 shadow-lg"
            onSubmit={submitMessage}
          >
            <div aria-disabled={composerDisabled}>
              <Textarea
                aria-label={copy.composerAria}
                value={composerText}
                disabled={composerDisabled}
                placeholder={
                  localDraftLocked
                    ? copy.saveLocalChangesFirst
                    : clarificationOpen
                      ? copy.answerQuestionFirst
                      : processing
                        ? copy.generatingBlueprint
                        : copy.composerPlaceholder
                }
                className="min-h-24 resize-none rounded-xl border-0 px-3 py-3 text-sm shadow-none focus-visible:ring-0"
                onChange={(event) => onComposerTextChange(event.target.value)}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    !event.shiftKey &&
                    !isIMEComposing(event)
                  ) {
                    event.preventDefault();
                    if (!composerDisabled && composerText.trim()) {
                      onSubmitMessage();
                    }
                  }
                }}
              />
              {modelsLoading ? (
                <p role="status" className="text-muted-foreground px-3 text-xs">
                  {copy.loadingModels}
                </p>
              ) : modelsError ? (
                <div className="flex items-center gap-2 px-3">
                  <p role="alert" className="text-destructive text-xs">
                    {copy.modelLoadFailed}
                  </p>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    className="h-7 px-2 text-xs"
                    onClick={onModelsRetry}
                  >
                    {t.agents.common.retry}
                  </Button>
                </div>
              ) : models.length === 0 ? (
                <p role="alert" className="text-destructive px-3 text-xs">
                  {copy.noModels}
                </p>
              ) : null}
              <div className="flex items-center justify-between gap-2 p-1">
                <div className="flex min-w-0 items-center gap-1">
                  <ModelSelector>
                    <ModelSelectorTrigger asChild>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="max-w-56 min-w-0 justify-start"
                        aria-label={copy.selectModelAria}
                        disabled={modelSelectorDisabled}
                      >
                        <ModelSelectorName className="text-xs font-normal">
                          {selectedGenerationModel?.display_name ??
                            copy.selectModel}
                        </ModelSelectorName>
                      </Button>
                    </ModelSelectorTrigger>
                    <ModelSelectorContent>
                      <ModelSelectorLabel>{copy.modelLabel}</ModelSelectorLabel>
                      <ModelSelectorList>
                        {models.map((model) => (
                          <ModelSelectorItem
                            key={model.name}
                            onSelect={() => onGenerationModelChange(model.name)}
                          >
                            <div className="flex min-w-0 flex-1 flex-col">
                              <ModelSelectorName>
                                {model.display_name}
                              </ModelSelectorName>
                              {model.is_default ? (
                                <span className="text-muted-foreground truncate text-xs">
                                  {t.agents.common.defaultSuffix}
                                </span>
                              ) : null}
                            </div>
                            {model.name === selectedGenerationModelName ? (
                              <CheckIcon
                                aria-hidden
                                className="ml-auto size-4"
                              />
                            ) : (
                              <span aria-hidden className="ml-auto size-4" />
                            )}
                          </ModelSelectorItem>
                        ))}
                      </ModelSelectorList>
                    </ModelSelectorContent>
                  </ModelSelector>
                  <InputBoxModeChooser
                    mode={selectedGenerationMode}
                    disabled={modelSelectorDisabled}
                    supportThinking={
                      selectedGenerationModel?.supports_thinking ?? false
                    }
                    supportReasoningEffort={
                      selectedGenerationModel?.supports_reasoning_effort ??
                      false
                    }
                    labels={t.inputBox}
                    onSelect={onGenerationModeChange}
                  />
                </div>
                <Button
                  type={generating ? "button" : "submit"}
                  size="icon"
                  className="size-11 rounded-xl"
                  aria-label={
                    generating ? copy.stopGeneration : t.agents.common.send
                  }
                  disabled={
                    generating
                      ? stopPending
                      : composerDisabled || composerText.trim().length === 0
                  }
                  onClick={generating ? onStopGeneration : undefined}
                >
                  {stopPending || mutationPending ? (
                    <Loader2Icon aria-hidden className="size-4 animate-spin" />
                  ) : generating ? (
                    <SquareIcon aria-hidden className="size-4 fill-current" />
                  ) : (
                    <SendIcon aria-hidden className="size-4" />
                  )}
                </Button>
              </div>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  );
}

function AgentBuilderLoading() {
  return (
    <div className="mx-auto w-full max-w-4xl space-y-5 px-4 py-10 sm:px-6">
      <Skeleton className="ml-auto h-14 w-2/3 rounded-2xl" />
      <Skeleton className="h-28 w-full rounded-2xl" />
      <Skeleton className="h-52 w-full rounded-2xl" />
    </div>
  );
}

export function AgentBuilderWorkspace({ sessionId }: { sessionId: string }) {
  const { t } = useI18n();
  const { user } = useAuth();
  const project = useCurrentProject();
  const router = useRouter();
  const accountId = user?.id ?? "";
  const canAuthor = agentBuilderCanAuthor(project.capabilities);
  const modelsQuery = useModels({ enabled: Boolean(user) });
  const submitTurn = useSubmitAgentBuilderTurn(
    accountId,
    project.id,
    sessionId,
  );
  const stopTurn = useStopAgentBuilderTurn(accountId, project.id, sessionId);
  const generationPreference = useSetAgentBuilderGenerationPreference(
    accountId,
    project.id,
    sessionId,
  );
  const commit = useCommitAgentBuilderSession(accountId, project.id, sessionId);
  const cancel = useCancelAgentBuilderSession(accountId, project.id, sessionId);
  const sessionQuery = useAgentBuilderSession(
    accountId,
    project.id,
    sessionId,
    {
      canAuthor,
      pollWhileRequestPending:
        submitTurn.isPending || stopTurn.isPending || commit.isPending,
    },
  );
  const activitiesQuery = useAgentBuilderActivities(
    accountId,
    project.id,
    sessionId,
    Boolean(user) && sessionQuery.data?.status !== "cancelled",
  );
  const [composerText, setComposerText] = useState("");
  const [selectedGenerationModelName, setSelectedGenerationModelName] =
    useState<string | null>(null);
  const [selectedGenerationMode, setSelectedGenerationMode] =
    useState<AgentMode | null>(null);
  const [blueprintBaseline, setBlueprintBaseline] =
    useState<AgentBuilderBlueprint | null>(null);
  const [blueprintDraft, setBlueprintDraft] =
    useState<AgentBuilderBlueprint | null>(null);
  const [agentSlugBaseline, setAgentSlugBaseline] = useState<string | null>(
    null,
  );
  const [agentNameDraft, setAgentNameDraft] = useState<string | null>(null);
  const [blueprintEditing, setBlueprintEditing] = useState(false);
  const [selectedField, setSelectedField] = useState<AgentInstructionField>(
    "agents_instructions",
  );
  const [displayMode, setDisplayMode] = useState<"source" | "preview">(
    "preview",
  );
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelPreparing, setCancelPreparing] = useState(false);
  const [pendingLeaveHref, setPendingLeaveHref] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [idempotency] = useState(() => createAgentBuilderIdempotencyRegistry());
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const allowLeaveRef = useRef(false);
  const session = sessionQuery.data;
  const selectedGenerationModel =
    modelsQuery.models.find(
      (model) =>
        model.name ===
        (selectedGenerationModelName ??
          session?.generation_preference?.model_ref),
    ) ??
    modelsQuery.models.find((model) => model.is_default) ??
    modelsQuery.models[0] ??
    null;
  const effectiveGenerationModelName = selectedGenerationModel?.name ?? null;
  const effectiveGenerationMode = resolveAgentMode(
    selectedGenerationMode ??
      (session?.generation_preference?.model_ref ===
      effectiveGenerationModelName
        ? session.generation_preference.mode
        : undefined),
    selectedGenerationModel?.supports_thinking ?? false,
    selectedGenerationModel?.supports_reasoning_effort ?? false,
  );
  const generationExecutionProfile = getAgentModeExecutionProfile(
    effectiveGenerationMode,
    selectedGenerationModel?.supports_thinking ?? false,
    selectedGenerationModel?.supports_reasoning_effort ?? false,
  );
  const generationProfileRequest = {
    thinking_enabled: generationExecutionProfile.thinking_enabled === true,
    reasoning_effort: generationExecutionProfile.reasoning_effort as
      | "none"
      | "low"
      | "medium"
      | "high"
      | null,
  };
  const pendingUserMessage =
    submitTurn.isPending &&
    submitTurn.variables?.input.kind === "message" &&
    session?.revision === submitTurn.variables.expected_revision
      ? submitTurn.variables.input.message
      : null;
  const blueprintDirty = useMemo(
    () => !sameBlueprint(blueprintBaseline, blueprintDraft),
    [blueprintBaseline, blueprintDraft],
  );
  const effectiveAgentName = agentNameDraft ?? session?.slug ?? "";
  const normalizedAgentSlug = normalizeAgentBuilderSlug(effectiveAgentName);
  const agentSlugDirty =
    agentSlugBaseline !== null && normalizedAgentSlug !== agentSlugBaseline;
  const agentSlugValidationError = (() => {
    switch (agentBuilderSlugErrorCode(normalizedAgentSlug)) {
      case "too-short":
        return t.agents.builder.start.nameTooShort;
      case "too-long":
        return t.agents.builder.start.nameTooLong;
      case "invalid":
        return t.agents.builder.start.nameInvalid;
      default:
        return null;
    }
  })();
  const mutationPending =
    submitTurn.isPending ||
    generationPreference.isPending ||
    stopTurn.isPending ||
    commit.isPending ||
    cancel.isPending ||
    cancelPreparing;
  const effectiveBlueprint = blueprintDraft ?? session?.blueprint ?? null;
  const blueprintModelAvailable = Boolean(
    effectiveBlueprint &&
    !modelsQuery.isLoading &&
    !modelsQuery.error &&
    resolveModelDisplayName(effectiveBlueprint.model_ref, modelsQuery.models),
  );
  const cancelActionDisabled = agentBuilderCancelActionDisabled(session, {
    generationPending: submitTurn.isPending,
    commitPending: commit.isPending,
    cancelPending: cancel.isPending,
    cancelPreparing,
  });
  const mcpDependencyRuntime = useMcpDependencyRuntime({
    accountId,
    projectId: project.id,
    requiredVersionIds: effectiveBlueprint?.mcp_version_ids ?? [],
    enabled: Boolean(session),
  });

  useEffect(() => {
    if (!session?.blueprint) return;
    const rebased = rebaseAgentBuilderBlueprint(
      blueprintDraft,
      session.blueprint,
      blueprintEditing || blueprintDirty,
    );
    setBlueprintBaseline(rebased.baseline);
    setBlueprintDraft(rebased.draft);
  }, [blueprintDirty, blueprintDraft, blueprintEditing, session?.blueprint]);

  useEffect(() => {
    if (!session) return;
    const rebased = rebaseAgentBuilderName(
      agentNameDraft,
      session.slug,
      agentSlugDirty,
    );
    setAgentSlugBaseline(rebased.baseline);
    setAgentNameDraft(rebased.draft);
  }, [agentNameDraft, agentSlugDirty, session, session?.slug]);

  useEffect(() => {
    if (!session) return;
    const frame = window.requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({
        top: scrollRef.current.scrollHeight,
        behavior: "smooth",
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    session,
    session?.messages.length,
    session?.progress.length,
    session?.active_clarifications,
    pendingUserMessage,
  ]);

  useEffect(() => {
    if (!blueprintDirty && !agentSlugDirty) return;
    const preventDirtyUnload = (event: BeforeUnloadEvent) => {
      if (allowLeaveRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventDirtyUnload);
    return () => window.removeEventListener("beforeunload", preventDirtyUnload);
  }, [agentSlugDirty, blueprintDirty]);

  useEffect(() => {
    if (!blueprintDirty && !agentSlugDirty) return;
    const preventDirtyNavigation = (event: MouseEvent) => {
      if (
        allowLeaveRef.current ||
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest<HTMLAnchorElement>("a[href]");
      if (
        !anchor ||
        anchor.target === "_blank" ||
        anchor.hasAttribute("download")
      ) {
        return;
      }
      const destination = new URL(anchor.href, window.location.href);
      if (destination.href === window.location.href) return;
      event.preventDefault();
      event.stopPropagation();
      setPendingLeaveHref(destination.href);
    };
    document.addEventListener("click", preventDirtyNavigation, true);
    return () =>
      document.removeEventListener("click", preventDirtyNavigation, true);
  }, [agentSlugDirty, blueprintDirty]);

  function resetErrors() {
    setLocalError(null);
    submitTurn.reset();
    commit.reset();
    cancel.reset();
    stopTurn.reset();
    generationPreference.reset();
  }

  function persistGenerationPreference(model: Model, mode: AgentMode) {
    const executionProfile = getAgentModeExecutionProfile(
      mode,
      model.supports_thinking,
      model.supports_reasoning_effort,
    );
    generationPreference.mutate(
      {
        generation_model_ref: model.name,
        generation_mode: mode,
        thinking_enabled: executionProfile.thinking_enabled === true,
        reasoning_effort: executionProfile.reasoning_effort as
          | "none"
          | "low"
          | "medium"
          | "high"
          | null,
      },
      {
        onSuccess: () => {
          setSelectedGenerationModelName(null);
          setSelectedGenerationMode(null);
        },
        onError: (error) => {
          setSelectedGenerationModelName(null);
          setSelectedGenerationMode(null);
          if (
            error instanceof AgentBuilderApiError &&
            error.code === "AGENT_DESIGN_GENERATION_PROFILE_STALE"
          ) {
            void Promise.all([modelsQuery.refetch(), sessionQuery.refetch()]);
          }
        },
      },
    );
  }

  function selectGenerationModel(modelName: string) {
    const model = modelsQuery.models.find((item) => item.name === modelName);
    if (!model) return;
    const mode = resolveAgentMode(
      undefined,
      model.supports_thinking,
      model.supports_reasoning_effort,
    );
    setSelectedGenerationModelName(modelName);
    setSelectedGenerationMode(mode);
    persistGenerationPreference(model, mode);
  }

  function selectGenerationMode(mode: AgentMode) {
    if (!selectedGenerationModel) return;
    setSelectedGenerationMode(mode);
    persistGenerationPreference(selectedGenerationModel, mode);
  }

  function sendMessage() {
    const message = composerText.trim();
    if (
      !session ||
      !message ||
      !effectiveGenerationModelName ||
      !canAuthor ||
      mutationPending ||
      blueprintEditing ||
      blueprintDirty
    ) {
      return;
    }
    resetErrors();
    const signature = agentBuilderSemanticSignature({
      kind: "message",
      message,
      generation_model_ref: effectiveGenerationModelName,
      generation_mode: effectiveGenerationMode,
      ...generationProfileRequest,
    });
    const command = idempotency.acquire("message-turn", signature, (key) => ({
      input: { kind: "message" as const, message },
      generation_model_ref: effectiveGenerationModelName,
      generation_mode: effectiveGenerationMode,
      ...generationProfileRequest,
      idempotency_key: key,
      expected_revision: session.revision,
    }));
    setComposerText("");
    submitTurn.mutate(command, {
      onSuccess: (response) => {
        idempotency.complete("message-turn", signature);
        if (response.data.status === "failed") {
          if (
            response.data.error_code === "AGENT_DESIGN_GENERATION_PROFILE_STALE"
          ) {
            void modelsQuery.refetch();
          }
          setComposerText((current) => current || message);
          return;
        }
      },
      onError: (error) => {
        setComposerText((current) => current || message);
        if (
          error instanceof AgentBuilderApiError &&
          error.code === "AGENT_DESIGN_GENERATION_PROFILE_STALE"
        ) {
          void modelsQuery.refetch();
        }
        void recoverAgentBuilderConflict(
          error,
          () => idempotency.complete("message-turn", signature),
          () => sessionQuery.refetch(),
        ).catch(() => undefined);
      },
    });
  }

  function submitClarification(
    response: HumanInputResponse,
  ): Promise<boolean> | false {
    if (
      !session ||
      !effectiveGenerationModelName ||
      !canAuthor ||
      mutationPending ||
      blueprintEditing ||
      blueprintDirty
    ) {
      return false;
    }
    resetErrors();
    const signature = agentBuilderSemanticSignature({
      kind: "clarification",
      response,
      generation_model_ref: effectiveGenerationModelName,
      generation_mode: effectiveGenerationMode,
      ...generationProfileRequest,
    });
    const command = idempotency.acquire(
      "clarification-turn",
      signature,
      (key) => ({
        input: { kind: "clarification" as const, response },
        generation_model_ref: effectiveGenerationModelName,
        generation_mode: effectiveGenerationMode,
        ...generationProfileRequest,
        idempotency_key: key,
        expected_revision: session.revision,
      }),
    );
    return submitAgentBuilderClarificationMutation(
      () =>
        submitTurn.mutateAsync(command).then((result) => {
          if (
            result.data.status === "failed" &&
            result.data.error_code === "AGENT_DESIGN_GENERATION_PROFILE_STALE"
          ) {
            void modelsQuery.refetch();
          }
          return result;
        }),
      () => {
        idempotency.complete("clarification-turn", signature);
      },
      (error) =>
        (error instanceof AgentBuilderApiError &&
        error.code === "AGENT_DESIGN_GENERATION_PROFILE_STALE"
          ? modelsQuery.refetch().then(() => false)
          : Promise.resolve(false)
        )
          .then(() =>
            recoverAgentBuilderConflict(
              error,
              () => idempotency.complete("clarification-turn", signature),
              () => sessionQuery.refetch(),
            ),
          )
          .then(() => undefined)
          .catch(() => undefined),
    );
  }

  function saveBlueprint() {
    if (
      !session ||
      !canAuthor ||
      !blueprintDraft ||
      !blueprintDirty ||
      mutationPending
    ) {
      return;
    }
    const blueprintError = agentBuilderBlueprintValidationMessage(
      blueprintDraft,
      t.agents.builder.blueprint.validation,
    );
    if (blueprintError) {
      setLocalError(blueprintError);
      return;
    }
    resetErrors();
    const signature = agentBuilderSemanticSignature({
      kind: "blueprint_update",
      blueprint: blueprintDraft,
    });
    const command = idempotency.acquire("blueprint-turn", signature, (key) => ({
      input: {
        kind: "blueprint_update" as const,
        blueprint: blueprintDraft,
      },
      idempotency_key: key,
      expected_revision: session.revision,
    }));
    submitTurn.mutate(command, {
      onSuccess: (response) => {
        if (response.data.status === "failed") return;
        idempotency.complete("blueprint-turn", signature);
        setBlueprintBaseline(response.data.blueprint);
        setBlueprintDraft(response.data.blueprint);
        setBlueprintEditing(false);
        setDisplayMode("preview");
      },
      onError: (error) => {
        void recoverAgentBuilderConflict(
          error,
          () => idempotency.complete("blueprint-turn", signature),
          () => sessionQuery.refetch(),
        ).catch(() => undefined);
      },
    });
  }

  function complete() {
    const blueprintChecksum = session?.blueprint_checksum;
    if (
      !session ||
      !canAuthor ||
      !blueprintChecksum ||
      blueprintEditing ||
      blueprintDirty ||
      mcpDependencyRuntime.isLoading ||
      Boolean(mcpDependencyRuntime.blockReason) ||
      !blueprintModelAvailable ||
      Boolean(agentSlugValidationError) ||
      !normalizedAgentSlug ||
      session.conflicts.some((conflict) => conflict.severity === "error") ||
      !agentBuilderCanComplete(session)
    ) {
      return;
    }
    resetErrors();
    const slugFields = agentBuilderCommitSlugFields(
      normalizedAgentSlug,
      session.slug,
    );
    const signature = agentBuilderSemanticSignature({
      blueprint_checksum: blueprintChecksum,
      ...slugFields,
    });
    const command = idempotency.acquire("commit", signature, (key) => ({
      ...slugFields,
      idempotency_key: key,
      expected_revision: session.revision,
      expected_blueprint_checksum: blueprintChecksum,
    }));
    commit.mutate(command, {
      onSuccess: () => {
        idempotency.complete("commit", signature);
      },
      onError: (error) => {
        void recoverAgentBuilderConflict(
          error,
          () => idempotency.complete("commit", signature),
          () => sessionQuery.refetch(),
        ).catch(() => undefined);
      },
    });
  }

  function discardBlueprint() {
    setBlueprintDraft(blueprintBaseline);
    setBlueprintEditing(false);
    setDisplayMode("preview");
    setLocalError(null);
  }

  async function confirmCancel() {
    if (!session || !canAuthor || cancelActionDisabled) return;
    setLocalError(null);
    cancel.reset();
    setCancelPreparing(true);
    const authoritativeSession = await prepareAgentBuilderCancelSession(
      session,
      submitTurn.isPending || session.status === "generating",
      async () => (await sessionQuery.refetch()).data,
    );
    if (
      authoritativeSession.status === "committing" ||
      authoritativeSession.status === "completed" ||
      authoritativeSession.status === "cancelled"
    ) {
      setCancelPreparing(false);
      return;
    }
    const signature = agentBuilderSemanticSignature({
      session_id: authoritativeSession.id,
      expected_revision: authoritativeSession.revision,
    });
    const command = idempotency.acquire("cancel", signature, (key) => ({
      idempotency_key: key,
      expected_revision: authoritativeSession.revision,
    }));
    cancel.mutate(command, {
      onSuccess: () => {
        idempotency.complete("cancel", signature);
        setCancelOpen(false);
        router.replace(`/projects/${encodeURIComponent(project.slug)}/agents`);
      },
      onError: (error) => {
        void recoverAgentBuilderConflict(
          error,
          () => idempotency.complete("cancel", signature),
          () => sessionQuery.refetch(),
        ).catch(() => undefined);
      },
      onSettled: () => setCancelPreparing(false),
    });
  }

  const commitSlugConflictError =
    commit.error instanceof AgentBuilderApiError &&
    commit.error.code === "AGENT_DESIGN_SLUG_CONFLICT"
      ? agentBuilderWorkspaceErrorMessage(
          commit.error,
          t.agents.builder.errors,
          true,
        )
      : null;
  const agentSlugError = agentSlugValidationError ?? commitSlugConflictError;
  const requestError =
    localError ??
    (submitTurn.error
      ? agentBuilderWorkspaceErrorMessage(
          submitTurn.error,
          t.agents.builder.errors,
        )
      : commit.error && !commitSlugConflictError
        ? agentBuilderWorkspaceErrorMessage(
            commit.error,
            t.agents.builder.errors,
            true,
          )
        : generationPreference.error
          ? agentBuilderWorkspaceErrorMessage(
              generationPreference.error,
              t.agents.builder.errors,
            )
          : cancel.error
            ? agentBuilderWorkspaceErrorMessage(
                cancel.error,
                t.agents.builder.errors,
              )
            : null);

  if (!user) return null;

  return (
    <>
      <main className="flex h-[calc(100svh-3.5rem)] min-h-0 flex-col overflow-hidden md:h-screen">
        <header className="border-border/70 bg-background/95 flex min-h-14 shrink-0 items-center gap-3 border-b px-3 backdrop-blur sm:px-5">
          <Button asChild type="button" size="icon" variant="ghost">
            <Link
              href={`/projects/${encodeURIComponent(project.slug)}/agents`}
              aria-label={t.agents.builder.conversation.backToAgents}
            >
              <ArrowLeftIcon aria-hidden className="size-4" />
            </Link>
          </Button>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold">
              {session?.display_name ??
                t.agents.builder.conversation.designAgent}
            </p>
            <p className="text-muted-foreground truncate text-xs">
              {t.agents.builder.conversation.autosave}
            </p>
          </div>
          {canAuthor ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  aria-label={t.agents.builder.conversation.more}
                  disabled={cancelActionDisabled}
                >
                  <MoreHorizontalIcon aria-hidden className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  variant="destructive"
                  onSelect={() => setCancelOpen(true)}
                >
                  <Trash2Icon aria-hidden className="size-4" />
                  {t.agents.builder.conversation.abandon}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </header>

        <section
          className="min-h-0 flex-1 overflow-hidden"
          aria-label={t.agents.builder.conversation.conversationAria}
        >
          {sessionQuery.isLoading ? (
            <AgentBuilderLoading />
          ) : sessionQuery.error || !session ? (
            <div className="mx-auto max-w-xl px-4 py-16 text-center">
              <p role="alert" className="text-destructive text-sm">
                {sessionQuery.error
                  ? agentBuilderErrorMessage(
                      sessionQuery.error,
                      t.agents.builder.errors,
                    )
                  : t.agents.builder.conversation.sessionUnavailable}
              </p>
              <Button
                type="button"
                variant="outline"
                className="mt-4 min-h-11"
                disabled={sessionQuery.isFetching}
                onClick={() => void sessionQuery.refetch()}
              >
                {sessionQuery.isFetching
                  ? t.agents.common.retrying
                  : t.agents.common.retry}
              </Button>
            </div>
          ) : (
            <AgentBuilderConversationView
              session={session}
              composerText={composerText}
              agentName={effectiveAgentName}
              agentSlug={normalizedAgentSlug}
              agentSlugError={agentSlugError}
              pendingUserMessage={pendingUserMessage}
              activities={activitiesQuery.data ?? []}
              canAuthor={canAuthor && session.status !== "completed"}
              mutationPending={mutationPending}
              commitPending={commit.isPending}
              blueprintEditing={blueprintEditing}
              blueprintDraft={blueprintDraft ?? session.blueprint}
              blueprintDirty={blueprintDirty}
              models={modelsQuery.models}
              modelsLoading={modelsQuery.isLoading}
              modelsError={modelsQuery.error}
              selectedGenerationModelName={effectiveGenerationModelName}
              selectedGenerationMode={effectiveGenerationMode}
              scrollContainerRef={scrollRef}
              mcpDependencyLoading={mcpDependencyRuntime.isLoading}
              mcpDependencyBlockReason={mcpDependencyRuntime.blockReason}
              selectedField={selectedField}
              displayMode={displayMode}
              errorMessage={requestError}
              onModelsRetry={() => void modelsQuery.refetch()}
              onGenerationModelChange={selectGenerationModel}
              onGenerationModeChange={selectGenerationMode}
              onComposerTextChange={(value) => {
                setComposerText(value);
                if (requestError) resetErrors();
              }}
              onSubmitMessage={sendMessage}
              onStopGeneration={() => stopTurn.mutate()}
              stopPending={stopTurn.isPending}
              createdAgentHref={
                session.created_agent_id
                  ? `/projects/${encodeURIComponent(project.slug)}/agents?agent_id=${encodeURIComponent(session.created_agent_id)}`
                  : null
              }
              onSubmitClarification={submitClarification}
              onSelectedFieldChange={setSelectedField}
              onDisplayModeChange={setDisplayMode}
              onBlueprintChange={setBlueprintDraft}
              onAgentNameChange={(value) => {
                setAgentNameDraft(value);
                setLocalError(null);
                commit.reset();
              }}
              onBlueprintEdit={() => {
                setBlueprintEditing(true);
                setDisplayMode("source");
              }}
              onBlueprintSave={saveBlueprint}
              onBlueprintDiscard={discardBlueprint}
              onComplete={complete}
            />
          )}
        </section>
      </main>

      <Dialog open={cancelOpen} onOpenChange={setCancelOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {t.agents.builder.conversation.abandonTitle}
            </DialogTitle>
            <DialogDescription>
              {t.agents.builder.conversation.abandonDescription}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={cancel.isPending || cancelPreparing}
              onClick={() => setCancelOpen(false)}
            >
              {t.agents.builder.conversation.continueDesign}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={!canAuthor || cancelActionDisabled}
              onClick={() => void confirmCancel()}
            >
              {cancel.isPending || cancelPreparing ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : null}
              {cancel.isPending || cancelPreparing
                ? t.agents.builder.conversation.abandoning
                : t.agents.builder.conversation.confirmAbandon}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={pendingLeaveHref !== null}
        onOpenChange={(open) => !open && setPendingLeaveHref(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {t.agents.builder.conversation.discardTitle}
            </DialogTitle>
            <DialogDescription>
              {t.agents.builder.conversation.discardDescription}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setPendingLeaveHref(null)}
            >
              {t.agents.builder.conversation.continueEditing}
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => {
                const destination = pendingLeaveHref;
                setPendingLeaveHref(null);
                if (!destination) return;
                allowLeaveRef.current = true;
                const url = new URL(destination, window.location.href);
                if (url.origin === window.location.origin) {
                  router.push(`${url.pathname}${url.search}${url.hash}`);
                } else {
                  window.location.assign(url.href);
                }
              }}
            >
              {t.agents.builder.conversation.discardAndLeave}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
