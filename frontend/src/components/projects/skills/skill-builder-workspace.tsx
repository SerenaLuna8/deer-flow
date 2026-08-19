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
  skillBuilderRunPresentation,
  skillBuilderSemanticSignature,
  skillBuilderValidationCurrent,
  updateSkillBuilderFileDraft,
  useCancelSkillBuilderSession,
  useCommitSkillBuilderSession,
  useSkillBuilderSession,
  useSkillBuilderRunStream,
  useSubmitSkillBuilderTurn,
  useValidateSkillBuilderSession,
  type SkillBuilderAttachment,
  type SkillBuilderCommitResponse,
  type SkillBuilderMergeAttachmentError,
  type SkillBuilderFile,
  type SkillBuilderIdempotencyChannel,
  type SkillBuilderReasoningEffort,
  type SkillBuilderRunPresentation,
  type SkillBuilderRunPresentationStatus,
  type SkillBuilderRunStreamProjection,
  type SkillBuilderSession,
} from "@/core/skill-builder";
import { SafeStreamdown } from "@/core/streamdown/components";
import { resolveAgentMode, type AgentMode } from "@/core/threads/agent-mode";
import { isIMEComposing } from "@/lib/ime";
import { cn } from "@/lib/utils";

import { useCurrentProject } from "../project-context";

import { SkillBuilderCandidateWorkbench } from "./skill-builder-candidate-workbench";
import {
  SkillBuilderComposerAttachments,
  SkillBuilderComposerControls,
} from "./skill-builder-composer-controls";
import { SkillBuilderFilesTrigger } from "./skill-builder-files-trigger";
import {
  SkillBuilderRunActivity,
  skillBuilderRunIsActive,
} from "./skill-builder-run-activity";
import { skillBuilderErrorMessage } from "./skill-builder-start";

const SKILL_BUILDER_EFFORT_BY_MODE = {
  flash: "none",
  thinking: "low",
  pro: "medium",
  ultra: "high",
} as const satisfies Record<AgentMode, SkillBuilderReasoningEffort>;

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
}: {
  versionNumber: number | null;
  href: string;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.success;
  return (
    <div className="mx-auto flex w-full max-w-xl flex-1 flex-col items-center justify-center px-4 py-16 text-center">
      <p className="text-sm font-medium">
        {skillBuilderRevisionCommitSuccessCopy(versionNumber, copy)}
      </p>
      <Button asChild type="button" className="mt-6 min-h-11">
        <Link href={href}>{copy.goPublish}</Link>
      </Button>
    </div>
  );
}

export type SkillBuilderCreatedSecretSetup = {
  skillId: string;
  skillVersionId: string;
  requirementNames: string[];
};

export function skillBuilderCreatedSecretSetupFromSession(
  session: SkillBuilderSession,
): SkillBuilderCreatedSecretSetup | null {
  const validation = session.validation;
  if (
    session.session_kind !== "create" ||
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
    !session.created_skill_version_id ||
    (options.configureCredentials && session.session_kind !== "create")
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
    session.created_skill_version_id ??
    version?.id ??
    skill.current_published_version_id;
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
        version.workflow_status !== "published" ||
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
  runProjection = null,
  runPresentation = null,
  onComposerTextChange,
  onSubmitMessage,
  onSubmitClarification,
  onAddAttachmentFiles,
  onRemoveAttachment,
  onSelectModel,
  onSelectThinkingMode,
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
  runProjection?: SkillBuilderRunStreamProjection | null;
  runPresentation?: SkillBuilderRunPresentation | null;
  onComposerTextChange: (value: string) => void;
  onSubmitMessage: () => void;
  onSubmitClarification: (
    response: HumanInputResponse,
  ) => boolean | void | Promise<boolean | void>;
  onAddAttachmentFiles?: (files: File[]) => void;
  onRemoveAttachment?: (name: string) => void;
  onSelectModel?: (name: string) => void;
  onSelectThinkingMode?: (mode: AgentMode) => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.conversation;
  const errors = t.skills.builder.errors;
  const composerDisabled =
    skillBuilderComposerDisabled(session, pending, dirty) || !canAuthor;
  const projectedMessageIds = new Set(
    session.messages.map((message) => message.id),
  );
  const projectedMessages =
    runProjection?.messages.filter(
      (message) => !projectedMessageIds.has(message.id),
    ) ?? [];
  const activeClarification =
    runProjection?.clarification ?? session.active_clarification;
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
    Boolean(runProjection && skillBuilderRunIsActive(runProjection.status)) ||
    session.status === "generating" ||
    session.status === "committing";
  const activeRunActivityVisible =
    Boolean(session.activeRun) ||
    Boolean(runProjection && skillBuilderRunIsActive(runProjection.status)) ||
    Boolean(runPresentation && skillBuilderRunIsActive(runPresentation.status));
  const showStandaloneGeneratingStatus =
    session.status === "committing" ||
    (generating && !activeRunActivityVisible);

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

        {session.messages.length === 0 &&
        projectedMessages.length === 0 &&
        !session.target_skill_deleted ? (
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

        {session.messages.map((message) => (
          <SkillBuilderMessageBubble
            key={message.id}
            role={message.role}
            content={message.content}
          />
        ))}

        {projectedMessages.map((message) => (
          <SkillBuilderMessageBubble
            key={message.id}
            role={message.role}
            content={message.content}
          />
        ))}

        {pendingUserMessage ? (
          <SkillBuilderMessageBubble role="user" content={pendingUserMessage} />
        ) : null}

        <SkillBuilderRunActivity
          activeRun={session.activeRun}
          projection={runProjection}
          presentation={runPresentation}
          failureCode={session.error_code}
        />

        {activeClarification ? (
          <section className="border-border/70 rounded-2xl border px-4">
            <HumanInputCard
              key={activeClarification.request_id}
              request={activeClarification}
              disabled={
                !canAuthor ||
                dirty ||
                Boolean(session.activeRun) ||
                Boolean(
                  runProjection &&
                  skillBuilderRunIsActive(runProjection.status),
                )
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
                ? copy.creatingDraft
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

      {canAuthor && !session.target_skill_deleted ? (
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

type SkillBuilderTrackedRun = {
  sessionId: string;
  runId: string;
  terminalStatus?: Exclude<
    SkillBuilderRunPresentationStatus,
    "pending" | "running"
  >;
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
  const [composerText, setComposerText] = useState("");
  const [admittedUserMessage, setAdmittedUserMessage] =
    useState<SkillBuilderAdmittedUserMessage | null>(null);
  const [trackedRun, setTrackedRun] = useState<SkillBuilderTrackedRun | null>(
    null,
  );
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
  const [baseStaleOpen, setBaseStaleOpen] = useState(false);
  const [createdDraftVersion, setCreatedDraftVersion] = useState<{
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
  const session = sessionQuery.data;
  const trackedRunId =
    trackedRun?.sessionId === sessionId ? trackedRun.runId : null;
  const runProjection = useSkillBuilderRunStream({
    threadId: session?.thread_id ?? null,
    runId: session?.activeRun?.runId ?? trackedRunId,
    initialStatus: session?.activeRun?.status ?? "running",
    enabled: Boolean(session?.activeRun),
  });
  const runPresentation =
    trackedRun?.sessionId === sessionId && trackedRun.terminalStatus
      ? {
          runId: trackedRun.runId,
          status: trackedRun.terminalStatus,
        }
      : session
        ? skillBuilderRunPresentation(session, trackedRunId)
        : null;
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
    executionModels.find((model) => model.is_default) ??
    executionModels[0];
  const thinkingMode = resolveAgentMode(
    requestedThinkingMode ?? "flash",
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
    attachmentIngestions > 0;
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
    const activeRun = session?.activeRun;
    if (!activeRun) return;
    setTrackedRun((current) =>
      current?.sessionId === sessionId && current.runId === activeRun.runId
        ? current
        : { sessionId, runId: activeRun.runId },
    );
  }, [session?.activeRun, sessionId]);

  useEffect(() => {
    if (
      !session ||
      session.activeRun ||
      trackedRun?.sessionId !== sessionId ||
      trackedRun.terminalStatus
    ) {
      return;
    }
    const settled = skillBuilderRunPresentation(session, trackedRun.runId);
    if (
      !settled ||
      settled.status === "pending" ||
      settled.status === "running"
    ) {
      return;
    }
    setTrackedRun({ ...trackedRun, terminalStatus: settled.status });
  }, [session, sessionId, trackedRun]);

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
  }

  function composerExecutionOptions(): {
    model_name?: string;
    reasoning_effort?: SkillBuilderReasoningEffort;
  } {
    const options: {
      model_name?: string;
      reasoning_effort?: SkillBuilderReasoningEffort;
    } = {};
    if (
      selectedModelName &&
      executionModels.some((model) => model.name === selectedModelName)
    ) {
      options.model_name = selectedModelName;
    }
    const effort = SKILL_BUILDER_EFFORT_BY_MODE[thinkingMode];
    if (effort !== "none") {
      options.reasoning_effort = effort;
    }
    return options;
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
          setTrackedRun({ sessionId, runId: response.runId });
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

  function saveDraft() {
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

  function confirmCommit(acknowledgeBaseStale = false) {
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
      acknowledge_base_stale: acknowledgeBaseStale,
    });
    const command = idempotency.acquire("commit", signature, (key) => ({
      expected_revision: session.revision,
      expected_draft_checksum: draftChecksum,
      acknowledge_warnings: acknowledgeWarnings,
      ...(acknowledgeBaseStale ? { acknowledge_base_stale: true } : {}),
      idempotency_key: key,
    }));
    commit.mutate(command, {
      onSuccess: (response) => {
        idempotency.complete("commit", signature);
        setCommitOpen(false);
        setBaseStaleOpen(false);
        if (response.data.session.session_kind === "revise") {
          const createdVersion = response.data.version;
          setCreatedDraftVersion(
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
        router.replace(createdHref);
      },
      onError: (error) => {
        refreshAfterConflict("commit", signature, error);
        if (
          error instanceof SkillBuilderApiError &&
          error.serverCode === "SKILL_DESIGN_BASE_STALE"
        ) {
          setCommitOpen(false);
          setBaseStaleOpen(true);
        }
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
        : commit.error && !baseStaleOpen
          ? skillBuilderWorkspaceErrorMessage(commit.error, errors, true)
          : cancel.error
            ? skillBuilderWorkspaceErrorMessage(cancel.error, errors)
            : null);

  if (!user) return null;

  const listHref = `/projects/${encodeURIComponent(project.slug)}/skills`;
  const durableSecretSetup = session
    ? skillBuilderCreatedSecretSetupFromSession(session)
    : null;
  const effectiveSecretSetup = createdSecretSetup ?? durableSecretSetup;
  const exactCreatedVersionId =
    session?.created_skill_version_id ?? createdDraftVersion?.id ?? null;
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
          configureCredentials: false,
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
        ) : effectiveSecretSetup &&
          createSecretHref &&
          session.status === "completed" &&
          session.created_skill_id === effectiveSecretSetup.skillId ? (
          <SkillBuilderCreateSecretSuccess
            key={effectiveSecretSetup.skillVersionId}
            requirementCount={effectiveSecretSetup.requirementNames.length}
            href={createSecretHref}
          />
        ) : revising &&
          session.status === "completed" &&
          session.created_skill_id &&
          revisionHref ? (
          <SkillBuilderRevisionCommitSuccess
            versionNumber={createdDraftVersion?.versionNumber ?? null}
            href={revisionHref}
          />
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
                runProjection={runProjection}
                runPresentation={runPresentation}
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
                onSelectModel={(name) => setSelectedModelName(name)}
                onSelectThinkingMode={(mode) => setRequestedThinkingMode(mode)}
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
                  onSave={saveDraft}
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
              commit.error &&
              !(
                commit.error instanceof SkillBuilderApiError &&
                commit.error.serverCode === "SKILL_DESIGN_BASE_STALE"
              )
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
              onClick={() => confirmCommit(false)}
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

      <Dialog
        open={baseStaleOpen}
        onOpenChange={(open) => {
          if (commit.isPending) return;
          setBaseStaleOpen(open);
          if (!open) commit.reset();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{dialogs.staleTitle}</DialogTitle>
            <DialogDescription>
              {dialogs.staleDescription(
                String(session?.base_version_number ?? "-"),
              )}
            </DialogDescription>
          </DialogHeader>
          {commit.error &&
          !(
            commit.error instanceof SkillBuilderApiError &&
            commit.error.serverCode === "SKILL_DESIGN_BASE_STALE"
          ) ? (
            <p role="alert" className="text-destructive text-sm">
              {skillBuilderWorkspaceErrorMessage(commit.error, errors, true)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={commit.isPending}
              onClick={() => {
                setBaseStaleOpen(false);
                commit.reset();
              }}
            >
              {dialogs.backToReview}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={
                commit.isPending ||
                !session ||
                !skillBuilderCanCommitCandidate(
                  session,
                  drafts,
                  secretDeclarationValid ? "valid" : "pending",
                )
              }
              onClick={() => confirmCommit(true)}
            >
              {commit.isPending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : null}
              {commit.isPending
                ? dialogs.creatingVersion
                : dialogs.confirmOverwrite}
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
