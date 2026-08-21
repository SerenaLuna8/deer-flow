"use client";

import {
  ArrowLeftIcon,
  Loader2Icon,
  MoreHorizontalIcon,
  SendIcon,
  SparklesIcon,
  Trash2Icon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  Fragment,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";

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
import { HumanInputCard } from "@/components/workspace/messages/human-input-card";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import type { HumanInputResponse } from "@/core/messages/human-input";
import { useModels } from "@/core/models/hooks";
import type { Model } from "@/core/models/types";
import {
  SkillBuilderApiError,
  SKILL_BUILDER_MAX_ATTACHMENT_BYTES,
  SKILL_BUILDER_MAX_ATTACHMENTS,
  SKILL_BUILDER_MAX_FILE_BYTES,
  SKILL_BUILDER_MAX_MESSAGE_CHARS,
  SKILL_BUILDER_MAX_TOTAL_BYTES,
  createSkillBuilderIdempotencyRegistry,
  isSkillBuilderRunAdmission,
  reconcileSkillBuilderFileSelection,
  skillBuilderCanAuthor,
  skillBuilderCanCommitCandidate,
  skillBuilderCanValidateCandidate,
  skillBuilderCandidateValidationCurrent,
  skillBuilderComposerDisabled,
  skillBuilderFileDraftContent,
  skillBuilderMergeAttachment,
  skillBuilderSemanticSignature,
  skillBuilderValidationCurrent,
  updateSkillBuilderFileDraft,
  useCancelSkillBuilderSession,
  useCommitSkillBuilderSession,
  useSkillBuilderSession,
  useSkillBuilderActivities,
  useSetSkillBuilderExecutionPreference,
  useStopSkillBuilderTurn,
  useSubmitSkillBuilderTurn,
  useValidateSkillBuilderSession,
  type SkillBuilderAttachment,
  type SkillBuilderCommitResponse,
  type SkillBuilderMergeAttachmentError,
  type SkillBuilderFile,
  type SkillBuilderIdempotencyChannel,
  type SkillBuilderReasoningEffort,
  type SkillBuilderActivity,
  type SkillBuilderSession,
} from "@/core/skill-builder";
import { SafeStreamdown } from "@/core/streamdown/components";
import {
  getAgentModeExecutionProfile,
  resolveAgentMode,
  type AgentMode,
} from "@/core/threads/agent-mode";
import { isIMEComposing } from "@/lib/ime";
import { cn } from "@/lib/utils";

import { useCurrentProject } from "../project-context";

import { SkillBuilderCandidateWorkbench } from "./skill-builder-candidate-workbench";
import {
  SkillBuilderComposerAttachments,
  SkillBuilderComposerControls,
} from "./skill-builder-composer-controls";
import { SkillBuilderFilesTrigger } from "./skill-builder-files-trigger";
import { SkillBuilderActivityBlock } from "./skill-builder-run-activity";
import { skillBuilderErrorMessage } from "./skill-builder-start";

export function skillBuilderWorkspaceErrorMessage(
  error: unknown,
  copy: Translations["skills"]["builder"]["errors"],
  commitUncertain = false,
): string {
  if (
    commitUncertain &&
    error instanceof SkillBuilderApiError &&
    [
      "SKILL_BUILDER_NETWORK_ERROR",
      "SKILL_BUILDER_RESPONSE_INVALID",
      "SKILL_BUILDER_UNAVAILABLE",
    ].includes(error.code)
  ) {
    return copy.commitUncertain;
  }
  if (error instanceof SkillBuilderApiError) {
    if (error.serverCode === "SKILL_DESIGN_TARGET_DELETED") {
      return copy.targetDeleted;
    }
    if (error.serverCode === "SKILL_DESIGN_NO_CHANGES") {
      return copy.noChanges;
    }
    if (error.serverCode === "SKILL_DESIGN_BASE_STALE") {
      return copy.baseStale;
    }
    if (error.code === "SKILL_BUILDER_CONFLICT") {
      return copy.stale;
    }
  }
  return skillBuilderErrorMessage(error, copy);
}

export function skillBuilderRevisionCommitSuccessCopy(
  versionNumber: number | null,
  copy: Translations["skills"]["builder"]["success"],
): string {
  return versionNumber ? copy.withVersion(versionNumber) : copy.withoutVersion;
}

export function SkillBuilderRevisionCommitSuccess({
  versionNumber,
  href,
  credentialRequirementCount = 0,
}: {
  versionNumber: number | null;
  href: string;
  credentialRequirementCount?: number;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.success;
  return (
    <div className="mx-auto flex w-full max-w-xl flex-1 flex-col items-center justify-center px-4 py-16 text-center">
      <p className="text-sm font-medium">
        {credentialRequirementCount > 0
          ? copy.revisionWithSecrets(versionNumber, credentialRequirementCount)
          : skillBuilderRevisionCommitSuccessCopy(versionNumber, copy)}
      </p>
      <Button asChild type="button" className="mt-6 min-h-11">
        <Link href={href}>
          {credentialRequirementCount > 0
            ? copy.configureCredentials
            : copy.goActivate}
        </Link>
      </Button>
    </div>
  );
}

export type SkillBuilderCreatedSecretSetup = {
  skillId: string;
  skillVersionId: string;
  requirementNames: string[];
};

export function skillBuilderExecutionPreferenceFor(
  model: Pick<
    Model,
    "name" | "supports_thinking" | "supports_reasoning_effort"
  >,
  mode: unknown,
) {
  const resolvedMode = resolveAgentMode(
    mode,
    model.supports_thinking,
    model.supports_reasoning_effort,
  );
  const profile = getAgentModeExecutionProfile(
    resolvedMode,
    model.supports_thinking,
    model.supports_reasoning_effort,
  );
  return {
    model_name: model.name,
    mode: resolvedMode,
    thinking_enabled: profile.thinking_enabled === true,
    reasoning_effort:
      profile.reasoning_effort as SkillBuilderReasoningEffort | null,
  };
}

export function skillBuilderCreatedSecretSetupFromSession(
  session: SkillBuilderSession,
): SkillBuilderCreatedSecretSetup | null {
  const validation = session.validation;
  if (
    session.status !== "completed" ||
    !session.created_skill_id ||
    !session.created_skill_version_id ||
    !validation ||
    !skillBuilderValidationCurrent(session) ||
    validation.secret_requirements.length === 0
  ) {
    return null;
  }
  return {
    skillId: session.created_skill_id,
    skillVersionId: session.created_skill_version_id,
    requirementNames: validation.secret_requirements.map(
      (requirement) => requirement.name,
    ),
  };
}

function skillBuilderExactVersionHref(
  listHref: string,
  skillId: string,
  skillVersionId: string,
  configureCredentials: boolean,
): string {
  const params = new URLSearchParams({
    skill_id: skillId,
    skill_version_id: skillVersionId,
  });
  if (configureCredentials) {
    params.set("configure_credentials", "1");
  }
  return `${listHref}?${params.toString()}`;
}

export function skillBuilderCreateCommitHref(
  listHref: string,
  response: SkillBuilderCommitResponse,
): string | null {
  const { session, skill, version } = response.data;
  const exactVersionId = session.created_skill_version_id;
  if (
    session.session_kind !== "create" ||
    session.status !== "completed" ||
    session.created_skill_id !== skill.id ||
    session.project_id !== skill.project_id ||
    !exactVersionId ||
    (version !== null &&
      (version.id !== exactVersionId || version.skill_id !== skill.id))
  ) {
    return null;
  }
  return skillBuilderExactVersionHref(
    listHref,
    skill.id,
    exactVersionId,
    false,
  );
}

export function skillBuilderCompletedVersionHref(
  listHref: string,
  session: SkillBuilderSession,
  options: { configureCredentials: boolean },
): string | null {
  if (
    session.status !== "completed" ||
    !session.created_skill_id ||
    !session.created_skill_version_id
  ) {
    return null;
  }
  return skillBuilderExactVersionHref(
    listHref,
    session.created_skill_id,
    session.created_skill_version_id,
    options.configureCredentials,
  );
}

/**
 * Prefer the exact committed version. The live Skill pointer is only a
 * rolling-deployment fallback for an older Gateway create response.
 */
export function skillBuilderCreatedSecretSetup(
  response: SkillBuilderCommitResponse,
): SkillBuilderCreatedSecretSetup | null {
  const { session, skill, version } = response.data;
  const validation = session.validation;
  const exactVersionId =
    session.created_skill_version_id ?? version?.id ?? skill.current_version_id;
  const requirements =
    version?.secret_requirements ?? validation?.secret_requirements;
  if (
    session.session_kind !== "create" ||
    session.status !== "completed" ||
    session.created_skill_id !== skill.id ||
    session.project_id !== skill.project_id ||
    !exactVersionId ||
    (version !== null &&
      (version.id !== exactVersionId ||
        version.skill_id !== skill.id ||
        version.relation !== "candidate" ||
        version.payload_checksum !== session.draft_checksum)) ||
    !validation ||
    !skillBuilderValidationCurrent(session) ||
    !requirements ||
    requirements.length === 0
  ) {
    return null;
  }
  return {
    skillId: skill.id,
    skillVersionId: exactVersionId,
    requirementNames: requirements.map((requirement) => requirement.name),
  };
}

export function SkillBuilderCreateSecretSuccess({
  requirementCount,
  href,
}: {
  requirementCount: number;
  href: string;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.success;
  return (
    <div className="mx-auto flex w-full max-w-xl flex-1 flex-col items-center justify-center px-4 py-16 text-center">
      <p className="text-sm font-medium">
        {copy.createdWithSecrets(requirementCount)}
      </p>
      <Button asChild type="button" className="mt-6 min-h-11">
        <Link href={href}>{copy.configureCredentials}</Link>
      </Button>
    </div>
  );
}

function SkillBuilderMessageBubble({
  role,
  content,
}: {
  role: "user" | "assistant";
  content: string;
}) {
  if (role === "user") {
    return (
      <div
        className="flex justify-end"
        data-testid="skill-builder-user-message"
      >
        <div className="bg-muted max-w-[88%] rounded-2xl rounded-br-md px-4 py-3 text-sm leading-6">
          <SafeStreamdown>{content}</SafeStreamdown>
        </div>
      </div>
    );
  }
  return (
    <div className="grid grid-cols-[2.25rem_minmax(0,1fr)] gap-3">
      <span className="bg-muted flex size-9 items-center justify-center rounded-xl">
        <SparklesIcon aria-hidden className="size-4" />
      </span>
      <div className="min-w-0 pt-1 text-sm leading-7">
        <SafeStreamdown>{content}</SafeStreamdown>
      </div>
    </div>
  );
}

function SkillBuilderProgress({ session }: { session: SkillBuilderSession }) {
  const { t } = useI18n();
  const copy = t.skills.builder.conversation;
  if (session.progress.length === 0) return null;
  const revising = session.session_kind === "revise";
  return (
    <section
      aria-label={revising ? copy.progressAriaRevise : copy.progressAriaCreate}
      className="border-border/70 bg-muted/20 rounded-2xl border p-4"
    >
      <p className="mb-3 text-xs font-semibold">
        {revising ? copy.progressRevise : copy.progressCreate}
      </p>
      <ol className="space-y-2">
        {session.progress.map((item) => (
          <li key={item.id} className="flex items-center gap-2 text-xs">
            <span
              aria-hidden
              className={cn(
                "size-2 rounded-full",
                item.status === "completed"
                  ? "bg-emerald-500"
                  : item.status === "failed"
                    ? "bg-destructive"
                    : item.status === "running"
                      ? "bg-primary animate-pulse"
                      : "bg-muted-foreground/35",
              )}
            />
            <span
              className={cn(
                item.status === "pending" && "text-muted-foreground",
              )}
            >
              {item.label}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function SkillBuilderConversationView({
  session,
  composerText,
  pendingUserMessage,
  canAuthor,
  dirty,
  pending,
  errorMessage,
  attachments = [],
  models = [],
  executionModel,
  thinkingMode = "flash",
  activities = [],
  stopPending = false,
  completion,
  onComposerTextChange,
  onSubmitMessage,
  onSubmitClarification,
  onAddAttachmentFiles,
  onRemoveAttachment,
  onSelectModel,
  onSelectThinkingMode,
  onStopRun,
}: {
  session: SkillBuilderSession;
  composerText: string;
  pendingUserMessage?: string | null;
  canAuthor: boolean;
  dirty: boolean;
  pending: boolean;
  errorMessage: string | null;
  attachments?: SkillBuilderAttachment[];
  models?: Model[];
  executionModel?: Model;
  thinkingMode?: AgentMode;
  activities?: readonly SkillBuilderActivity[];
  stopPending?: boolean;
  completion?: {
    message: string;
    href: string;
    action: string;
  };
  onComposerTextChange: (value: string) => void;
  onSubmitMessage: () => void;
  onSubmitClarification: (
    response: HumanInputResponse,
  ) => boolean | void | Promise<boolean | void>;
  onAddAttachmentFiles?: (files: File[]) => void;
  onRemoveAttachment?: (name: string) => void;
  onSelectModel?: (name: string) => void;
  onSelectThinkingMode?: (mode: AgentMode) => void;
  onStopRun?: () => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.conversation;
  const errors = t.skills.builder.errors;
  const composerDisabled =
    skillBuilderComposerDisabled(session, pending, dirty) || !canAuthor;
  const activeClarification = session.active_clarification;
  const activitiesByOperation = new Map<string, SkillBuilderActivity[]>();
  for (const activity of activities) {
    const current = activitiesByOperation.get(activity.operation_id) ?? [];
    current.push(activity);
    activitiesByOperation.set(activity.operation_id, current);
  }
  const messageOperationIds = new Set(
    session.messages.flatMap((message) =>
      message.operation_id ? [message.operation_id] : [],
    ),
  );
  const orphanActivityGroups = [...activitiesByOperation.entries()].filter(
    ([operationId]) => !messageOperationIds.has(operationId),
  );
  const hasActiveActivity = [...activitiesByOperation.values()].some(
    (items) =>
      !items.some((item) =>
        ["run_terminal", "commit_terminal"].includes(item.kind),
      ),
  );
  const lastAssistantMessage = [...session.messages]
    .reverse()
    .find((message) => message.role === "assistant");
  const terminalErrorAlreadyMessaged =
    Boolean(session.error_message) &&
    lastAssistantMessage?.content.trim() === session.error_message?.trim();
  const clarificationOpen = Boolean(activeClarification);
  const generating =
    Boolean(pendingUserMessage) ||
    Boolean(session.activeRun) ||
    session.status === "generating" ||
    session.status === "committing";
  const showStandaloneGeneratingStatus = generating && !hasActiveActivity;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmitMessage();
  }

  return (
    <div className="flex min-h-full flex-col">
      <div className="mx-auto w-full max-w-(--chat-content-width) flex-1 space-y-6 p-4 sm:p-5">
        <SkillBuilderProgress session={session} />

        {session.target_skill_deleted ? (
          <p
            role="alert"
            className="border-destructive/30 bg-destructive/5 text-destructive rounded-xl border p-4 text-sm"
          >
            {errors.targetDeletedBanner}
          </p>
        ) : null}

        {!canAuthor ? (
          <p
            role="alert"
            className="border-border/70 bg-muted/20 text-muted-foreground rounded-xl border px-4 py-3 text-sm"
          >
            {session.session_kind === "revise"
              ? copy.permissionReadOnlyRevise
              : copy.permissionReadOnlyCreate}
          </p>
        ) : null}

        {session.messages.length === 0 && !session.target_skill_deleted ? (
          <div className="flex justify-end">
            <p className="bg-muted max-w-[90%] rounded-2xl rounded-br-md px-4 py-3 text-sm leading-6">
              {session.session_kind === "revise" ? (
                <>
                  {copy.reviseIntroBefore}{" "}
                  <span className="font-semibold">{session.slug}</span>
                  {session.base_version_number
                    ? ` v${session.base_version_number}`
                    : ""}{" "}
                  {copy.reviseIntroAfter}
                </>
              ) : (
                <>
                  {copy.createIntroBefore}{" "}
                  <span className="font-semibold">{session.display_name}</span>
                  {copy.createIntroAfter}
                </>
              )}
            </p>
          </div>
        ) : null}

        {session.messages.map((message) => {
          const operationActivities = message.operation_id
            ? activitiesByOperation.get(message.operation_id)
            : undefined;
          return (
            <Fragment key={message.id}>
              <SkillBuilderMessageBubble
                role={message.role}
                content={message.content}
              />
              {message.role === "user" && operationActivities?.length ? (
                <SkillBuilderActivityBlock
                  activities={operationActivities}
                  onStop={
                    operationActivities.some(
                      (activity) => activity.run_id !== null,
                    )
                      ? onStopRun
                      : undefined
                  }
                  stopPending={stopPending}
                />
              ) : null}
            </Fragment>
          );
        })}

        {pendingUserMessage ? (
          <SkillBuilderMessageBubble role="user" content={pendingUserMessage} />
        ) : null}

        {orphanActivityGroups.map(([operationId, operationActivities]) => (
          <SkillBuilderActivityBlock
            key={operationId}
            activities={operationActivities}
            onStop={
              operationActivities.some((activity) => activity.run_id !== null)
                ? onStopRun
                : undefined
            }
            stopPending={stopPending}
          />
        ))}

        {completion ? (
          <section className="border-border/70 bg-muted/20 rounded-2xl border p-4">
            <p className="text-sm leading-6">{completion.message}</p>
            <Button asChild type="button" className="mt-4 min-h-10">
              <Link href={completion.href}>{completion.action}</Link>
            </Button>
          </section>
        ) : null}

        {activeClarification ? (
          <section className="border-border/70 rounded-2xl border px-4">
            <HumanInputCard
              key={activeClarification.request_id}
              request={activeClarification}
              disabled={
                !canAuthor ||
                dirty ||
                Boolean(session.activeRun) ||
                hasActiveActivity
              }
              pending={pending}
              onSubmit={(response) => onSubmitClarification(response)}
            />
          </section>
        ) : null}

        {showStandaloneGeneratingStatus ? (
          <p
            role="status"
            className="text-muted-foreground flex items-center gap-2 text-xs"
          >
            <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
            {session.status === "committing"
              ? session.session_kind === "revise"
                ? copy.creatingCandidate
                : copy.creatingSkill
              : copy.processing}
          </p>
        ) : null}

        {session.status === "failed" &&
        session.error_message &&
        !terminalErrorAlreadyMessaged ? (
          session.error_code === "MODEL_OUTPUT_LIMIT" ? null : (
            <p
              role="alert"
              className="border-destructive/30 bg-destructive/5 text-destructive rounded-xl border p-4 text-sm"
            >
              {session.error_message}
            </p>
          )
        ) : null}

        {errorMessage ? (
          <p role="alert" className="text-destructive text-sm">
            {errorMessage}
          </p>
        ) : null}
      </div>

      {canAuthor &&
      !session.target_skill_deleted &&
      !["completed", "cancelled"].includes(session.status) ? (
        <form
          className="bg-background/95 border-border/70 sticky bottom-0 mx-auto mt-8 mb-3 w-[calc(100%-1.5rem)] max-w-(--chat-content-width) rounded-2xl border p-2 shadow-lg backdrop-blur"
          onSubmit={submit}
        >
          <SkillBuilderComposerAttachments
            attachments={attachments}
            disabled={pending}
            onRemove={(name) => onRemoveAttachment?.(name)}
          />
          <Textarea
            aria-label={
              session.session_kind === "revise"
                ? copy.composerAriaRevise
                : copy.composerAriaCreate
            }
            value={composerText}
            maxLength={SKILL_BUILDER_MAX_MESSAGE_CHARS}
            disabled={composerDisabled}
            placeholder={
              dirty
                ? copy.saveLocalChangesFirst
                : clarificationOpen
                  ? copy.answerQuestionFirst
                  : generating
                    ? copy.generatingFiles
                    : session.session_kind === "revise"
                      ? copy.placeholderRevise
                      : copy.placeholderCreate
            }
            className="min-h-20 resize-none rounded-xl border-0 px-3 py-3 text-sm shadow-none focus-visible:ring-0"
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
          <div className="flex items-center justify-between gap-2 p-1">
            <SkillBuilderComposerControls
              attachDisabled={composerDisabled}
              pickersDisabled={pending}
              models={models}
              selectedModel={executionModel}
              thinkingMode={thinkingMode}
              onPickFiles={(files) => onAddAttachmentFiles?.(files)}
              onSelectModel={(name) => onSelectModel?.(name)}
              onSelectThinkingMode={(mode) => onSelectThinkingMode?.(mode)}
            />
            <Button
              type="submit"
              size="icon"
              className="size-10 shrink-0 rounded-xl"
              aria-label={copy.send}
              disabled={composerDisabled || composerText.trim().length === 0}
            >
              {pending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : (
                <SendIcon aria-hidden className="size-4" />
              )}
            </Button>
          </div>
        </form>
      ) : null}
    </div>
  );
}

function SkillBuilderLoading() {
  return (
    <div className="mx-auto min-h-0 w-full max-w-(--chat-content-width) flex-1 space-y-4 p-5">
      <Skeleton className="ml-auto h-16 w-2/3 rounded-2xl" />
      <Skeleton className="h-28 w-full rounded-2xl" />
    </div>
  );
}

export function skillBuilderDraftChanges(
  files: SkillBuilderFile[],
  drafts: Record<string, string>,
) {
  return files.flatMap((file) => {
    const content = drafts[file.path];
    return content !== undefined && content !== file.content
      ? [
          {
            op: "replace" as const,
            path: file.path,
            content,
            media_type: file.media_type,
          },
        ]
      : [];
  });
}

export function skillBuilderDraftMutationSnapshot(
  session: SkillBuilderSession,
  changes: ReturnType<typeof skillBuilderDraftChanges>,
) {
  if (!session.draft_checksum || changes.length === 0) return null;
  return {
    expectedRevision: session.revision,
    expectedDraftChecksum: session.draft_checksum,
    changes,
  };
}

type SkillBuilderPendingLeave = {
  href: string;
  viaHistory: boolean;
};

type SkillBuilderAdmittedUserMessage = {
  runId: string;
  message: string;
  expectedRevision: number;
};

function skillBuilderAttachmentError(
  code: SkillBuilderMergeAttachmentError,
  copy: Translations["skills"]["builder"]["errors"],
): string {
  if (code === "too_large") return copy.attachmentTooLarge;
  if (code === "invalid_name") return copy.attachmentInvalidName;
  if (code === "too_many")
    return copy.attachmentTooMany(SKILL_BUILDER_MAX_ATTACHMENTS);
  return copy.attachmentTotalTooLarge;
}

type SkillBuilderAttachmentFile = Pick<File, "name" | "size" | "arrayBuffer">;

export type SkillBuilderAttachmentIngestResult =
  | { ok: true }
  | { ok: false; kind: "not_utf8"; fileName: string }
  | {
      ok: false;
      kind: "merge";
      error: SkillBuilderMergeAttachmentError;
    };

/**
 * Read a batch first, then merge and commit against the latest queue as one
 * synchronous step. The late `getCurrent` prevents an async File read from
 * resurrecting a removed attachment, while the synchronous commit prevents
 * concurrent batches from overwriting one another.
 */
export async function ingestSkillBuilderAttachmentFiles(
  files: readonly SkillBuilderAttachmentFile[],
  getCurrent: () => readonly SkillBuilderAttachment[],
  commit: (attachments: SkillBuilderAttachment[]) => void,
): Promise<SkillBuilderAttachmentIngestResult> {
  const loaded: SkillBuilderAttachment[] = [];
  for (const file of files) {
    if (file.size > SKILL_BUILDER_MAX_ATTACHMENT_BYTES) {
      return { ok: false, kind: "merge", error: "too_large" };
    }
    let content: string;
    try {
      content = new TextDecoder("utf-8", { fatal: true }).decode(
        await file.arrayBuffer(),
      );
    } catch {
      return { ok: false, kind: "not_utf8", fileName: file.name };
    }
    loaded.push({ name: file.name, content });
  }

  let next = [...getCurrent()];
  for (const attachment of loaded) {
    const merged = skillBuilderMergeAttachment(next, attachment);
    if (!merged.ok) {
      return { ok: false, kind: "merge", error: merged.error };
    }
    next = merged.attachments;
  }
  commit(next);
  return { ok: true };
}

export function SkillBuilderDialogError({
  message,
}: {
  message: string | null;
}) {
  return message ? (
    <p role="alert" className="text-destructive text-sm">
      {message}
    </p>
  ) : null;
}

export function SkillBuilderWorkspace({ sessionId }: { sessionId: string }) {
  const { user } = useAuth();
  const { t } = useI18n();
  const conversation = t.skills.builder.conversation;
  const dialogs = t.skills.builder.dialogs;
  const errors = t.skills.builder.errors;
  const project = useCurrentProject();
  const router = useRouter();
  const accountId = user?.id ?? "";
  const sessionQuery = useSkillBuilderSession(accountId, project.id, sessionId);
  const activitiesQuery = useSkillBuilderActivities(
    accountId,
    project.id,
    sessionId,
    Boolean(user),
  );
  const submitTurn = useSubmitSkillBuilderTurn(
    accountId,
    project.id,
    sessionId,
  );
  const validate = useValidateSkillBuilderSession(
    accountId,
    project.id,
    sessionId,
  );
  const commit = useCommitSkillBuilderSession(accountId, project.id, sessionId);
  const cancel = useCancelSkillBuilderSession(accountId, project.id, sessionId);
  const setExecutionPreference = useSetSkillBuilderExecutionPreference(
    accountId,
    project.id,
    sessionId,
  );
  const stopRun = useStopSkillBuilderTurn(accountId, project.id, sessionId);
  const [composerText, setComposerText] = useState("");
  const [admittedUserMessage, setAdmittedUserMessage] =
    useState<SkillBuilderAdmittedUserMessage | null>(null);
  const [composerAttachments, setComposerAttachments] = useState<
    SkillBuilderAttachment[]
  >([]);
  const composerAttachmentsRef = useRef<SkillBuilderAttachment[]>([]);
  const [attachmentIngestions, setAttachmentIngestions] = useState(0);
  const [selectedModelName, setSelectedModelName] = useState<string | null>(
    null,
  );
  const [requestedThinkingMode, setRequestedThinkingMode] =
    useState<AgentMode | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [displayMode, setDisplayMode] = useState<"source" | "preview">(
    "source",
  );
  const [workbenchOpen, setWorkbenchOpen] = useState(false);
  const [mobileSurface, setMobileSurface] = useState<
    "conversation" | "workbench"
  >("conversation");
  const [acknowledgedValidationToken, setAcknowledgedValidationToken] =
    useState<string | null>(null);
  const [draftBaselineChecksum, setDraftBaselineChecksum] = useState<
    string | null
  >(null);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [commitOpen, setCommitOpen] = useState(false);
  const [createdCandidateVersion, setCreatedCandidateVersion] = useState<{
    id: string;
    versionNumber: number;
  } | null>(null);
  const [createdSecretSetup, setCreatedSecretSetup] =
    useState<SkillBuilderCreatedSecretSetup | null>(null);
  const [validSecretSource, setValidSecretSource] = useState<{
    sessionId: string;
    content: string;
  } | null>(null);
  const [pendingLeave, setPendingLeave] =
    useState<SkillBuilderPendingLeave | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [idempotency] = useState(() => createSkillBuilderIdempotencyRegistry());
  const observedFilesSessionRef = useRef<string | null>(null);
  const previousFilesRef = useRef<SkillBuilderFile[]>([]);
  const previousChecksumRef = useRef<string | null>(null);
  const allowLeaveRef = useRef(false);
  const defaultPreferenceRequestedRef = useRef<string | null>(null);
  const session = sessionQuery.data;
  const optimisticUserMessage =
    submitTurn.isPending &&
    submitTurn.variables?.input.kind === "message" &&
    session?.revision === submitTurn.variables.expected_revision
      ? submitTurn.variables.input.message
      : null;
  const pendingUserMessage =
    optimisticUserMessage ??
    (admittedUserMessage &&
    session?.activeRun?.runId === admittedUserMessage.runId &&
    session.revision === admittedUserMessage.expectedRevision
      ? admittedUserMessage.message
      : null);
  const canAuthor = skillBuilderCanAuthor(project.capabilities);
  const modelsQuery = useModels({ enabled: Boolean(user) && canAuthor });
  const executionModels = modelsQuery.models;
  const executionModel =
    executionModels.find((model) => model.name === selectedModelName) ??
    executionModels.find(
      (model) => model.name === session?.execution_preference?.model_name,
    ) ??
    executionModels.find((model) => model.is_default) ??
    executionModels[0];
  const thinkingMode = resolveAgentMode(
    requestedThinkingMode ?? session?.execution_preference?.mode,
    executionModel?.supports_thinking ?? false,
    executionModel?.supports_reasoning_effort ?? false,
  );
  const changes = useMemo(
    () => (session ? skillBuilderDraftChanges(session.files, drafts) : []),
    [drafts, session],
  );
  const dirty = Object.keys(drafts).length > 0;
  const dirtyPaths = useMemo(() => new Set(Object.keys(drafts)), [drafts]);
  const mutationPending =
    submitTurn.isPending ||
    validate.isPending ||
    commit.isPending ||
    cancel.isPending ||
    setExecutionPreference.isPending ||
    stopRun.isPending ||
    attachmentIngestions > 0;
  useEffect(() => {
    if (
      !session ||
      executionModels.length === 0 ||
      setExecutionPreference.isPending
    ) {
      return;
    }
    const persistedModel = session.execution_preference
      ? executionModels.find(
          (model) => model.name === session.execution_preference?.model_name,
        )
      : undefined;
    const model =
      persistedModel ??
      executionModels.find((candidate) => candidate.is_default) ??
      executionModels[0];
    if (!model) return;
    const next = skillBuilderExecutionPreferenceFor(
      model,
      persistedModel ? session.execution_preference?.mode : undefined,
    );
    setSelectedModelName(next.model_name);
    setRequestedThinkingMode(next.mode);
    const current = session.execution_preference;
    const matches =
      current?.model_name === next.model_name &&
      current.mode === next.mode &&
      current.thinking_enabled === next.thinking_enabled &&
      current.reasoning_effort === next.reasoning_effort;
    if (
      matches ||
      !canAuthor ||
      ["generating", "committing", "completed", "cancelled"].includes(
        session.status,
      )
    ) {
      return;
    }
    const requestKey = `${session.id}:${JSON.stringify(next)}`;
    if (defaultPreferenceRequestedRef.current === requestKey) return;
    defaultPreferenceRequestedRef.current = requestKey;
    setExecutionPreference.mutate(next, {
      onError: () => {
        defaultPreferenceRequestedRef.current = null;
        void modelsQuery.refetch();
      },
    });
  }, [
    canAuthor,
    executionModels,
    modelsQuery,
    session,
    setExecutionPreference,
    setExecutionPreference.isPending,
  ]);
  const selectedFile =
    session?.files.find((file) => file.path === selectedPath) ?? null;
  const draftContent = session
    ? (skillBuilderFileDraftContent(
        session.files,
        drafts,
        selectedPath ?? "",
      ) ?? "")
    : "";
  const skillMdContent = session
    ? skillBuilderFileDraftContent(session.files, drafts, "SKILL.md")
    : null;
  const secretDeclarationValid = Boolean(
    session &&
    skillMdContent !== null &&
    validSecretSource?.sessionId === session.id &&
    validSecretSource.content === skillMdContent,
  );
  const fileEditingAllowed =
    (session?.status === "draft_ready" || session?.status === "validated") &&
    !session.target_skill_deleted;
  const revising = session?.session_kind === "revise";
  const draftBaselineStale = Boolean(
    dirty &&
    draftBaselineChecksum &&
    draftBaselineChecksum !== session?.draft_checksum,
  );
  const builderReadOnly =
    mutationPending ||
    Boolean(session?.activeRun) ||
    !fileEditingAllowed ||
    draftBaselineStale;
  const validationCurrent = session
    ? skillBuilderCandidateValidationCurrent(
        session,
        drafts,
        secretDeclarationValid ? "valid" : "pending",
      )
    : false;
  const validationToken = useMemo(
    () =>
      session?.validation
        ? skillBuilderSemanticSignature(session.validation)
        : null,
    [session?.validation],
  );
  const acknowledgeWarnings = Boolean(
    validationToken && acknowledgedValidationToken === validationToken,
  );
  const canValidate = session
    ? skillBuilderCanValidateCandidate(
        session,
        drafts,
        secretDeclarationValid ? "valid" : "pending",
        builderReadOnly,
      )
    : false;
  const secretSourceRef = useRef<{
    sessionId: string | null;
    content: string | null;
  }>({ sessionId: null, content: null });
  secretSourceRef.current = {
    sessionId: session?.id ?? null,
    content: skillMdContent,
  };
  const handleSecretValidityChange = useCallback((valid: boolean) => {
    const source = secretSourceRef.current;
    setValidSecretSource(
      valid && source.sessionId && source.content !== null
        ? { sessionId: source.sessionId, content: source.content }
        : null,
    );
  }, []);
  useEffect(() => {
    if (!session) return;
    const sameSession = observedFilesSessionRef.current === session.id;
    const previousFiles = sameSession ? previousFilesRef.current : [];
    if (!sameSession) {
      observedFilesSessionRef.current = session.id;
      setWorkbenchOpen(session.files.length > 0);
      setMobileSurface("conversation");
    } else if (
      session.files.length === 0 &&
      (session.status === "completed" || session.status === "cancelled")
    ) {
      setWorkbenchOpen(false);
    } else if (previousFiles.length === 0 && session.files.length > 0) {
      setWorkbenchOpen(true);
    }
    setSelectedPath((current) =>
      reconcileSkillBuilderFileSelection(current, previousFiles, session.files),
    );
    previousFilesRef.current = session.files;
    if (previousChecksumRef.current !== session.draft_checksum) {
      previousChecksumRef.current = session.draft_checksum;
      setAcknowledgedValidationToken(null);
    }
  }, [session]);

  useEffect(() => {
    if (
      !admittedUserMessage ||
      (session?.activeRun?.runId === admittedUserMessage.runId &&
        session.revision === admittedUserMessage.expectedRevision)
    ) {
      return;
    }
    setAdmittedUserMessage(null);
  }, [admittedUserMessage, session?.activeRun?.runId, session?.revision]);

  useEffect(() => {
    if (!dirty) setDraftBaselineChecksum(null);
  }, [dirty]);

  useEffect(() => {
    if (!dirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (allowLeaveRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [dirty]);

  useEffect(() => {
    if (!dirty) return;

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
      setPendingLeave({ href: destination.href, viaHistory: false });
    };

    const currentHref = window.location.href;
    const currentHistoryState: unknown = window.history.state;
    const preventHistoryNavigation = (event: PopStateEvent) => {
      if (allowLeaveRef.current) return;
      const destination = window.location.href;
      event.stopImmediatePropagation();
      window.history.pushState(currentHistoryState, "", currentHref);
      setPendingLeave({ href: destination, viaHistory: true });
    };

    document.addEventListener("click", preventDirtyNavigation, true);
    window.addEventListener("popstate", preventHistoryNavigation, true);
    return () => {
      document.removeEventListener("click", preventDirtyNavigation, true);
      window.removeEventListener("popstate", preventHistoryNavigation, true);
    };
  }, [dirty]);

  function resetErrors() {
    setLocalError(null);
    submitTurn.reset();
    validate.reset();
    commit.reset();
    cancel.reset();
    setExecutionPreference.reset();
    stopRun.reset();
  }

  function composerExecutionOptions(): {
    model_name?: string;
    mode?: AgentMode;
    thinking_enabled?: boolean;
    reasoning_effort?: SkillBuilderReasoningEffort | null;
  } {
    return executionModel
      ? skillBuilderExecutionPreferenceFor(executionModel, thinkingMode)
      : {};
  }

  function updateExecutionPreference(model: Model, mode: unknown) {
    if (!session || mutationPending) return;
    const next = skillBuilderExecutionPreferenceFor(model, mode);
    setSelectedModelName(next.model_name);
    setRequestedThinkingMode(next.mode);
    resetErrors();
    setExecutionPreference.mutate(next, {
      onError: () => void modelsQuery.refetch(),
    });
  }

  function updateComposerAttachments(
    update:
      | SkillBuilderAttachment[]
      | ((current: SkillBuilderAttachment[]) => SkillBuilderAttachment[]),
  ) {
    const next =
      typeof update === "function"
        ? update(composerAttachmentsRef.current)
        : update;
    composerAttachmentsRef.current = next;
    setComposerAttachments(next);
  }

  async function addAttachmentFiles(files: File[]) {
    setAttachmentIngestions((current) => current + 1);
    try {
      const result = await ingestSkillBuilderAttachmentFiles(
        files,
        () => composerAttachmentsRef.current,
        updateComposerAttachments,
      );
      if (!result.ok) {
        setLocalError(
          result.kind === "not_utf8"
            ? errors.attachmentNotUtf8(result.fileName)
            : skillBuilderAttachmentError(result.error, errors),
        );
        return;
      }
      setLocalError(null);
    } finally {
      setAttachmentIngestions((current) => Math.max(0, current - 1));
    }
  }

  function refreshAfterConflict(
    channel: SkillBuilderIdempotencyChannel,
    signature: string,
    error: unknown,
  ) {
    if (
      error instanceof SkillBuilderApiError &&
      error.code === "SKILL_BUILDER_CONFLICT"
    ) {
      idempotency.complete(channel, signature);
      void sessionQuery.refetch();
    }
  }

  function sendMessage() {
    const message = composerText.trim();
    if (
      !session ||
      !message ||
      !canAuthor ||
      mutationPending ||
      dirty ||
      skillBuilderComposerDisabled(session, false)
    ) {
      return;
    }
    const totalBytes = session.files.reduce(
      (total, file) =>
        total +
        new TextEncoder().encode(drafts[file.path] ?? file.content).byteLength,
      0,
    );
    if (totalBytes > SKILL_BUILDER_MAX_TOTAL_BYTES) {
      setLocalError(errors.packageTooLarge);
      return;
    }
    resetErrors();
    const executionOptions = composerExecutionOptions();
    const attachments =
      composerAttachments.length > 0 ? composerAttachments : undefined;
    const signature = skillBuilderSemanticSignature({
      kind: "message",
      message,
      expected_revision: session.revision,
      ...executionOptions,
      attachments,
    });
    const command = idempotency.acquire("message-turn", signature, (key) => ({
      input: {
        kind: "message" as const,
        message,
        ...executionOptions,
        ...(attachments ? { attachments } : {}),
      },
      expected_revision: session.revision,
      idempotency_key: key,
    }));
    setComposerText("");
    submitTurn.mutate(command, {
      onSuccess: (response) => {
        if (isSkillBuilderRunAdmission(response)) {
          setAdmittedUserMessage({
            runId: response.runId,
            message,
            expectedRevision: session.revision,
          });
        } else {
          setAdmittedUserMessage(null);
        }
        if (
          !isSkillBuilderRunAdmission(response) &&
          response.data.status === "failed"
        ) {
          setComposerText((current) => current || message);
          return;
        }
        idempotency.complete("message-turn", signature);
        updateComposerAttachments([]);
      },
      onError: (error) => {
        setAdmittedUserMessage(null);
        setComposerText((current) => current || message);
        refreshAfterConflict("message-turn", signature, error);
      },
    });
  }

  async function submitClarification(
    response: HumanInputResponse,
  ): Promise<boolean> {
    if (!session || !canAuthor || mutationPending || dirty) return false;
    resetErrors();
    const executionOptions = composerExecutionOptions();
    const signature = skillBuilderSemanticSignature({
      kind: "clarification",
      response,
      expected_revision: session.revision,
      ...executionOptions,
    });
    const command = idempotency.acquire(
      "clarification-turn",
      signature,
      (key) => ({
        input: {
          kind: "clarification" as const,
          response,
          ...executionOptions,
        },
        expected_revision: session.revision,
        idempotency_key: key,
      }),
    );
    try {
      const result = await submitTurn.mutateAsync(command);
      if (
        !isSkillBuilderRunAdmission(result) &&
        result.data.status === "failed"
      ) {
        return false;
      }
      idempotency.complete("clarification-turn", signature);
      return true;
    } catch (error) {
      refreshAfterConflict("clarification-turn", signature, error);
      return false;
    }
  }

  function updateCandidateDraft(path: string, content: string): boolean {
    if (!session) return false;
    const file = session.files.find((candidate) => candidate.path === path);
    if (!file) return false;
    if (
      new TextEncoder().encode(content).byteLength >
      SKILL_BUILDER_MAX_FILE_BYTES
    ) {
      setLocalError(errors.fileTooLarge);
      return false;
    }
    if (path === "SKILL.md") setValidSecretSource(null);
    if (!dirty && content !== file.content) {
      setDraftBaselineChecksum(session.draft_checksum);
    }
    setDrafts((current) =>
      updateSkillBuilderFileDraft(session.files, current, path, content),
    );
    if (requestError) resetErrors();
    return true;
  }

  function saveCandidateVersion() {
    const snapshot = session
      ? skillBuilderDraftMutationSnapshot(session, changes)
      : null;
    if (
      !session ||
      !snapshot ||
      !canAuthor ||
      !fileEditingAllowed ||
      draftBaselineStale ||
      mutationPending
    ) {
      return;
    }
    resetErrors();
    const signature = skillBuilderSemanticSignature({
      expected_draft_checksum: snapshot.expectedDraftChecksum,
      expected_revision: snapshot.expectedRevision,
      changes: snapshot.changes,
    });
    const command = idempotency.acquire("draft-turn", signature, (key) => ({
      input: {
        kind: "draft_update" as const,
        expected_draft_checksum: snapshot.expectedDraftChecksum,
        changes: snapshot.changes,
      },
      expected_revision: snapshot.expectedRevision,
      idempotency_key: key,
    }));
    submitTurn.mutate(command, {
      onSuccess: (response) => {
        if (isSkillBuilderRunAdmission(response)) return;
        if (response.data.status === "failed") return;
        idempotency.complete("draft-turn", signature);
        setDrafts({});
        setDraftBaselineChecksum(null);
      },
      onError: (error) => refreshAfterConflict("draft-turn", signature, error),
    });
  }

  function runValidation() {
    const draftChecksum = session?.draft_checksum;
    if (!draftChecksum || !canAuthor || !canValidate) return;
    resetErrors();
    const signature = skillBuilderSemanticSignature({
      draft_checksum: draftChecksum,
      expected_revision: session.revision,
    });
    const command = idempotency.acquire("validate", signature, (key) => ({
      expected_revision: session.revision,
      expected_draft_checksum: draftChecksum,
      idempotency_key: key,
    }));
    validate.mutate(command, {
      onSuccess: () => {
        idempotency.complete("validate", signature);
        setAcknowledgedValidationToken(null);
      },
      onError: (error) => refreshAfterConflict("validate", signature, error),
    });
  }

  function confirmCommit() {
    const draftChecksum = session?.draft_checksum;
    if (
      !draftChecksum ||
      !canAuthor ||
      !skillBuilderCanCommitCandidate(
        session,
        drafts,
        secretDeclarationValid ? "valid" : "pending",
      ) ||
      (session.validation?.scan_decision === "warn" && !acknowledgeWarnings)
    ) {
      return;
    }
    resetErrors();
    const signature = skillBuilderSemanticSignature({
      draft_checksum: draftChecksum,
      expected_revision: session.revision,
      acknowledge_warnings: acknowledgeWarnings,
    });
    const command = idempotency.acquire("commit", signature, (key) => ({
      expected_revision: session.revision,
      expected_draft_checksum: draftChecksum,
      acknowledge_warnings: acknowledgeWarnings,
      idempotency_key: key,
    }));
    commit.mutate(command, {
      onSuccess: (response) => {
        idempotency.complete("commit", signature);
        setCommitOpen(false);
        if (response.data.session.session_kind === "revise") {
          const createdVersion = response.data.version;
          setCreatedCandidateVersion(
            createdVersion
              ? {
                  id: createdVersion.id,
                  versionNumber: createdVersion.version_number,
                }
              : null,
          );
          return;
        }
        const secretSetup = skillBuilderCreatedSecretSetup(response);
        if (secretSetup) {
          setCreatedSecretSetup(secretSetup);
          return;
        }
        const createdHref = skillBuilderCreateCommitHref(listHref, response);
        if (!createdHref) {
          setLocalError(errors.commitUncertain);
          return;
        }
        const createdVersion = response.data.version;
        setCreatedCandidateVersion(
          createdVersion
            ? {
                id: createdVersion.id,
                versionNumber: createdVersion.version_number,
              }
            : null,
        );
      },
      onError: (error) => {
        refreshAfterConflict("commit", signature, error);
      },
    });
  }

  function confirmCancel() {
    if (!session || !canAuthor || mutationPending) return;
    resetErrors();
    const signature = skillBuilderSemanticSignature({
      session_id: session.id,
      expected_revision: session.revision,
    });
    const command = idempotency.acquire("cancel", signature, (key) => ({
      expected_revision: session.revision,
      idempotency_key: key,
    }));
    cancel.mutate(command, {
      onSuccess: () => {
        idempotency.complete("cancel", signature);
        setCancelOpen(false);
        router.replace(`/projects/${encodeURIComponent(project.slug)}/skills`);
      },
      onError: (error) => refreshAfterConflict("cancel", signature, error),
    });
  }

  const requestError =
    localError ??
    (submitTurn.error
      ? skillBuilderWorkspaceErrorMessage(submitTurn.error, errors)
      : validate.error
        ? skillBuilderWorkspaceErrorMessage(validate.error, errors)
        : commit.error
          ? skillBuilderWorkspaceErrorMessage(commit.error, errors, true)
          : cancel.error
            ? skillBuilderWorkspaceErrorMessage(cancel.error, errors)
            : setExecutionPreference.error
              ? skillBuilderWorkspaceErrorMessage(
                  setExecutionPreference.error,
                  errors,
                )
              : stopRun.error
                ? skillBuilderWorkspaceErrorMessage(stopRun.error, errors)
                : null);

  if (!user) return null;

  const listHref = `/projects/${encodeURIComponent(project.slug)}/skills`;
  const durableSecretSetup = session
    ? skillBuilderCreatedSecretSetupFromSession(session)
    : null;
  const effectiveSecretSetup = createdSecretSetup ?? durableSecretSetup;
  const exactCreatedVersionId =
    session?.created_skill_version_id ?? createdCandidateVersion?.id ?? null;
  const createSecretHref =
    (session
      ? skillBuilderCompletedVersionHref(listHref, session, {
          configureCredentials: true,
        })
      : null) ??
    (effectiveSecretSetup
      ? skillBuilderExactVersionHref(
          listHref,
          effectiveSecretSetup.skillId,
          effectiveSecretSetup.skillVersionId,
          true,
        )
      : null);
  const revisionHref =
    (session
      ? skillBuilderCompletedVersionHref(listHref, session, {
          configureCredentials: Boolean(
            revising &&
            effectiveSecretSetup?.skillId === session.created_skill_id &&
            effectiveSecretSetup.skillVersionId === exactCreatedVersionId,
          ),
        })
      : null) ??
    (session?.status === "completed" &&
    session.created_skill_id &&
    exactCreatedVersionId
      ? skillBuilderExactVersionHref(
          listHref,
          session.created_skill_id,
          exactCreatedVersionId,
          false,
        )
      : null);
  const completionSecretSetup =
    effectiveSecretSetup?.skillId === session?.created_skill_id &&
    effectiveSecretSetup?.skillVersionId === exactCreatedVersionId
      ? effectiveSecretSetup
      : null;
  const completionSecretCount =
    completionSecretSetup?.requirementNames.length ?? 0;
  const completionHref =
    session?.status === "completed"
      ? completionSecretCount > 0
        ? createSecretHref
        : revisionHref
      : null;
  const completion =
    session?.status === "completed" && completionHref
      ? {
          message:
            completionSecretCount > 0
              ? session.session_kind === "revise"
                ? t.skills.builder.success.revisionWithSecrets(
                    createdCandidateVersion?.versionNumber ?? null,
                    completionSecretCount,
                  )
                : t.skills.builder.success.createdWithSecrets(
                    completionSecretCount,
                  )
              : session.session_kind === "revise"
                ? skillBuilderRevisionCommitSuccessCopy(
                    createdCandidateVersion?.versionNumber ?? null,
                    t.skills.builder.success,
                  )
                : t.skills.builder.success.created,
          href: completionHref,
          action:
            completionSecretCount > 0
              ? t.skills.builder.success.configureCredentials
              : t.skills.builder.success.goActivate,
        }
      : undefined;

  return (
    <>
      <main className="flex h-[calc(100svh-3.5rem)] min-h-0 flex-col overflow-hidden md:h-screen">
        <header className="border-border/70 bg-background/95 flex min-h-14 shrink-0 items-center gap-3 border-b px-3 backdrop-blur sm:px-5">
          {dirty ? (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              aria-label={conversation.backToSkills}
              onClick={() =>
                setPendingLeave({ href: listHref, viaHistory: false })
              }
            >
              <ArrowLeftIcon aria-hidden className="size-4" />
            </Button>
          ) : (
            <Button asChild type="button" size="icon" variant="ghost">
              <Link href={listHref} aria-label={conversation.continueLater}>
                <ArrowLeftIcon aria-hidden className="size-4" />
              </Link>
            </Button>
          )}
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold">
              {session?.display_name ?? conversation.fallbackTitle}
            </p>
            <p className="text-muted-foreground truncate text-xs">
              {session?.target_skill_deleted
                ? errors.targetDeletedStatus
                : revising && session?.base_version_number
                  ? conversation.revisingBanner(
                      session.slug,
                      session.base_version_number,
                    )
                  : dirty
                    ? conversation.unsavedChanges
                    : session?.activeRun || session?.status === "generating"
                      ? conversation.agentRunning
                      : validationCurrent
                        ? revising
                          ? conversation.checkedRevise
                          : conversation.checkedCreate
                        : conversation.autosave}
            </p>
          </div>
          <SkillBuilderFilesTrigger
            fileCount={session?.files.length ?? 0}
            onOpen={() => {
              setWorkbenchOpen(true);
              setMobileSurface("workbench");
            }}
          />
          {canAuthor ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  aria-label={conversation.more}
                  disabled={!session || mutationPending}
                >
                  <MoreHorizontalIcon aria-hidden className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  variant="destructive"
                  disabled={dirty}
                  onSelect={() => setCancelOpen(true)}
                >
                  <Trash2Icon aria-hidden className="size-4" />
                  {revising
                    ? conversation.abandonRevise
                    : conversation.abandonCreate}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </header>

        {sessionQuery.isLoading ? (
          <SkillBuilderLoading />
        ) : sessionQuery.error || !session ? (
          <div className="mx-auto max-w-xl px-4 py-16 text-center">
            <p role="alert" className="text-destructive text-sm">
              {sessionQuery.error
                ? skillBuilderErrorMessage(sessionQuery.error, errors)
                : conversation.sessionUnavailable}
            </p>
            <Button
              type="button"
              variant="outline"
              className="mt-4 min-h-11"
              disabled={sessionQuery.isFetching}
              onClick={() => void sessionQuery.refetch()}
            >
              {sessionQuery.isFetching
                ? conversation.retrying
                : conversation.retry}
            </Button>
          </div>
        ) : (
          <div
            className={cn(
              "grid min-h-0 flex-1",
              workbenchOpen &&
                "lg:grid-cols-[minmax(20rem,0.9fr)_minmax(28rem,1.1fr)]",
            )}
          >
            <section
              aria-label={conversation.conversationAria}
              className={cn(
                "min-h-0 overflow-y-auto overscroll-contain",
                mobileSurface === "workbench" && "hidden lg:block",
              )}
            >
              <SkillBuilderConversationView
                session={session}
                composerText={composerText}
                pendingUserMessage={pendingUserMessage}
                canAuthor={canAuthor}
                dirty={dirty}
                pending={mutationPending}
                errorMessage={requestError}
                attachments={composerAttachments}
                models={executionModels}
                executionModel={executionModel}
                thinkingMode={thinkingMode}
                activities={activitiesQuery.data ?? []}
                stopPending={stopRun.isPending}
                completion={completion}
                onComposerTextChange={(value) => {
                  setComposerText(value);
                  if (requestError) resetErrors();
                }}
                onSubmitMessage={sendMessage}
                onSubmitClarification={submitClarification}
                onAddAttachmentFiles={(files) => void addAttachmentFiles(files)}
                onRemoveAttachment={(name) => {
                  updateComposerAttachments((current) =>
                    current.filter((item) => item.name !== name),
                  );
                }}
                onSelectModel={(name) => {
                  const model = executionModels.find(
                    (candidate) => candidate.name === name,
                  );
                  if (model) updateExecutionPreference(model, undefined);
                }}
                onSelectThinkingMode={(mode) => {
                  if (executionModel) {
                    updateExecutionPreference(executionModel, mode);
                  }
                }}
                onStopRun={() => {
                  resetErrors();
                  stopRun.mutate();
                }}
              />
            </section>
            {workbenchOpen ? (
              <section
                aria-label={conversation.workbenchAria}
                className={cn(
                  "border-border/70 min-h-0 lg:border-l",
                  mobileSurface === "conversation" && "hidden lg:block",
                )}
              >
                <SkillBuilderCandidateWorkbench
                  projectId={project.id}
                  files={session.files}
                  selectedPath={selectedPath}
                  draftContent={draftContent}
                  skillMdContent={skillMdContent}
                  displayMode={displayMode}
                  canAuthor={canAuthor}
                  readOnly={Boolean(builderReadOnly)}
                  dirty={dirty}
                  dirtyPaths={dirtyPaths}
                  pending={mutationPending}
                  validation={
                    validationCurrent && !dirty ? session.validation : null
                  }
                  canValidate={canValidate}
                  canCommit={skillBuilderCanCommitCandidate(
                    session,
                    drafts,
                    secretDeclarationValid ? "valid" : "pending",
                  )}
                  acknowledgeWarnings={acknowledgeWarnings}
                  baselineStale={draftBaselineStale}
                  sessionKind={session.session_kind}
                  baseFiles={session.base_files}
                  baseVersionNumber={session.base_version_number}
                  errorMessage={requestError}
                  onSelectPath={(path) => {
                    setSelectedPath(path);
                    setDisplayMode("source");
                    setLocalError(null);
                  }}
                  onDraftContentChange={(content) => {
                    if (!selectedFile) return;
                    updateCandidateDraft(selectedFile.path, content);
                  }}
                  onSkillMdContentChange={(content) =>
                    updateCandidateDraft("SKILL.md", content)
                  }
                  onSecretValidityChange={handleSecretValidityChange}
                  onDisplayModeChange={setDisplayMode}
                  onSave={saveCandidateVersion}
                  onDiscard={() => {
                    setDrafts({});
                    setDraftBaselineChecksum(null);
                    resetErrors();
                  }}
                  onValidate={runValidation}
                  onAcknowledgeWarningsChange={(value) => {
                    setAcknowledgedValidationToken(
                      value ? validationToken : null,
                    );
                    if (requestError) resetErrors();
                  }}
                  onCommit={() => setCommitOpen(true)}
                  onClose={() => {
                    setWorkbenchOpen(false);
                    setMobileSurface("conversation");
                  }}
                />
              </section>
            ) : null}
          </div>
        )}
      </main>

      <Dialog open={commitOpen} onOpenChange={setCommitOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {revising ? dialogs.commitTitleRevise : dialogs.commitTitleCreate}
            </DialogTitle>
            <DialogDescription>
              {revising
                ? dialogs.commitDescriptionRevise(
                    session?.slug ?? "",
                    String(session?.base_version_number ?? "-"),
                  )
                : dialogs.commitDescriptionCreate(project.display_name)}
            </DialogDescription>
          </DialogHeader>
          <div className="bg-muted/35 rounded-xl p-4 text-sm">
            <p className="font-medium">{session?.display_name}</p>
            <p className="text-muted-foreground mt-1 text-xs">
              {revising
                ? dialogs.fileMetaRevise(session?.files.length ?? 0)
                : dialogs.fileMetaCreate(session?.files.length ?? 0)}
            </p>
          </div>
          <SkillBuilderDialogError
            message={
              commit.error
                ? skillBuilderWorkspaceErrorMessage(commit.error, errors, true)
                : null
            }
          />
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={commit.isPending}
              onClick={() => setCommitOpen(false)}
            >
              {dialogs.backToReview}
            </Button>
            <Button
              type="button"
              disabled={
                commit.isPending ||
                !session ||
                !skillBuilderCanCommitCandidate(
                  session,
                  drafts,
                  secretDeclarationValid ? "valid" : "pending",
                )
              }
              onClick={() => confirmCommit()}
            >
              {commit.isPending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : null}
              {commit.isPending
                ? revising
                  ? dialogs.creatingVersion
                  : dialogs.creating
                : revising
                  ? dialogs.confirmCreateVersion
                  : dialogs.confirmCreate}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={cancelOpen} onOpenChange={setCancelOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {revising
                ? dialogs.abandonTitleRevise
                : dialogs.abandonTitleCreate}
            </DialogTitle>
            <DialogDescription>
              {revising
                ? dialogs.abandonDescriptionRevise
                : dialogs.abandonDescriptionCreate}
            </DialogDescription>
          </DialogHeader>
          <SkillBuilderDialogError
            message={
              cancel.error
                ? skillBuilderWorkspaceErrorMessage(cancel.error, errors)
                : null
            }
          />
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={cancel.isPending}
              onClick={() => setCancelOpen(false)}
            >
              {revising ? dialogs.continueRevise : dialogs.continueCreate}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={cancel.isPending}
              onClick={confirmCancel}
            >
              {cancel.isPending ? dialogs.abandoning : dialogs.confirmAbandon}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={pendingLeave !== null}
        onOpenChange={(open) => {
          if (!open) setPendingLeave(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{dialogs.discardTitle}</DialogTitle>
            <DialogDescription>{dialogs.discardDescription}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setPendingLeave(null)}
            >
              {dialogs.continueEditing}
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => {
                const leave = pendingLeave;
                setDrafts({});
                setDraftBaselineChecksum(null);
                setPendingLeave(null);
                if (!leave) return;
                allowLeaveRef.current = true;
                if (leave.viaHistory) {
                  window.history.back();
                  return;
                }
                const url = new URL(leave.href, window.location.href);
                if (url.origin === window.location.origin) {
                  router.push(`${url.pathname}${url.search}${url.hash}`);
                } else {
                  window.location.assign(url.href);
                }
              }}
            >
              {dialogs.discardAndLeave}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
