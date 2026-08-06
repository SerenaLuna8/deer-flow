"use client";

import {
  ArrowLeftIcon,
  Loader2Icon,
  MessageSquareIcon,
  MoreHorizontalIcon,
  SendIcon,
  SparklesIcon,
  Trash2Icon,
  WorkflowIcon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

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
import type { HumanInputResponse } from "@/core/messages/human-input";
import {
  SkillBuilderApiError,
  SKILL_BUILDER_MAX_FILE_BYTES,
  SKILL_BUILDER_MAX_MESSAGE_CHARS,
  SKILL_BUILDER_MAX_TOTAL_BYTES,
  createSkillBuilderIdempotencyRegistry,
  reconcileSkillBuilderFileSelection,
  skillBuilderCanAuthor,
  skillBuilderCanCommit,
  skillBuilderComposerDisabled,
  skillBuilderSemanticSignature,
  skillBuilderValidationCurrent,
  useCancelSkillBuilderSession,
  useCommitSkillBuilderSession,
  useSkillBuilderSession,
  useSubmitSkillBuilderTurn,
  useValidateSkillBuilderSession,
  type SkillBuilderFile,
  type SkillBuilderIdempotencyChannel,
  type SkillBuilderSession,
} from "@/core/skill-builder";
import { SafeStreamdown } from "@/core/streamdown/components";
import { isIMEComposing } from "@/lib/ime";
import { cn } from "@/lib/utils";

import { useCurrentProject } from "../project-context";

import { SkillBuilderCandidateWorkbench } from "./skill-builder-candidate-workbench";
import { skillBuilderErrorMessage } from "./skill-builder-start";

export function skillBuilderWorkspaceErrorMessage(
  error: unknown,
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
    return "创建结果暂时无法确认。请勿重复创建，先返回 Skill 列表检查同名项目；若未出现再重试。";
  }
  if (
    error instanceof SkillBuilderApiError &&
    error.code === "SKILL_BUILDER_CONFLICT"
  ) {
    return "候选文件已发生变化，请刷新到最新状态后重试。";
  }
  return skillBuilderErrorMessage(error);
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
  if (session.progress.length === 0) return null;
  return (
    <section
      aria-label="Skill 创建进度"
      className="border-border/70 bg-muted/20 rounded-2xl border p-4"
    >
      <p className="mb-3 text-xs font-semibold">创建进度</p>
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
  onComposerTextChange,
  onSubmitMessage,
  onSubmitClarification,
}: {
  session: SkillBuilderSession;
  composerText: string;
  pendingUserMessage?: string | null;
  canAuthor: boolean;
  dirty: boolean;
  pending: boolean;
  errorMessage: string | null;
  onComposerTextChange: (value: string) => void;
  onSubmitMessage: () => void;
  onSubmitClarification: (
    response: HumanInputResponse,
  ) => boolean | void | Promise<boolean | void>;
}) {
  const composerDisabled =
    skillBuilderComposerDisabled(session, pending, dirty) || !canAuthor;
  const clarificationOpen = Boolean(session.active_clarification);
  const generating =
    Boolean(pendingUserMessage) ||
    session.status === "generating" ||
    session.status === "committing";

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmitMessage();
  }

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex-1 space-y-6 p-4 sm:p-5">
        <SkillBuilderProgress session={session} />

        {!canAuthor ? (
          <p
            role="alert"
            className="border-border/70 bg-muted/20 text-muted-foreground rounded-xl border px-4 py-3 text-sm"
          >
            当前账号没有继续创建 Skill
            的权限。你仍可查看已保存的会话和候选文件。
          </p>
        ) : null}

        {session.messages.length === 0 ? (
          <div className="flex justify-end">
            <p className="bg-muted max-w-[90%] rounded-2xl rounded-br-md px-4 py-3 text-sm leading-6">
              新 Skill 的名称是{" "}
              <span className="font-semibold">{session.display_name}</span>
              。请描述用途、触发条件、输入输出和需要的参考资料或脚本。
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

        {pendingUserMessage ? (
          <SkillBuilderMessageBubble role="user" content={pendingUserMessage} />
        ) : null}

        {session.active_clarification ? (
          <section className="border-border/70 rounded-2xl border px-4">
            <HumanInputCard
              key={session.active_clarification.request_id}
              request={session.active_clarification}
              disabled={!canAuthor || dirty}
              pending={pending}
              onSubmit={(response) => onSubmitClarification(response)}
            />
          </section>
        ) : null}

        {generating ? (
          <p
            role="status"
            className="text-muted-foreground flex items-center gap-2 text-xs"
          >
            <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
            {session.status === "committing"
              ? "正在创建 Skill…"
              : "skill-creator 正在生成候选文件…"}
          </p>
        ) : null}

        {session.status === "failed" && session.error_message ? (
          <p
            role="alert"
            className="border-destructive/30 bg-destructive/5 text-destructive rounded-xl border p-4 text-sm"
          >
            {session.error_message}
          </p>
        ) : null}

        {errorMessage ? (
          <p role="alert" className="text-destructive text-sm">
            {errorMessage}
          </p>
        ) : null}
      </div>

      {canAuthor ? (
        <form
          className="bg-background/95 border-border/70 sticky bottom-0 m-3 mt-8 rounded-2xl border p-2 shadow-lg backdrop-blur"
          onSubmit={submit}
        >
          <Textarea
            aria-label="描述想要的 Skill"
            value={composerText}
            maxLength={SKILL_BUILDER_MAX_MESSAGE_CHARS}
            disabled={composerDisabled}
            placeholder={
              dirty
                ? "请先保存或放弃右侧文件修改"
                : clarificationOpen
                  ? "等待你回答上方问题"
                  : generating
                    ? "正在生成候选文件…"
                    : "继续描述或要求调整候选 Skill。"
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
          <div className="flex justify-end p-1">
            <Button
              type="submit"
              size="icon"
              className="size-10 rounded-xl"
              aria-label="发送"
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
    <div className="grid min-h-0 flex-1 lg:grid-cols-2">
      <div className="space-y-4 p-5">
        <Skeleton className="ml-auto h-16 w-2/3 rounded-2xl" />
        <Skeleton className="h-28 w-full rounded-2xl" />
      </div>
      <div className="border-border/70 hidden border-l p-5 lg:block">
        <Skeleton className="h-full min-h-96 w-full rounded-2xl" />
      </div>
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

type SkillBuilderPendingLeave = {
  href: string;
  viaHistory: boolean;
};

export function SkillBuilderWorkspace({ sessionId }: { sessionId: string }) {
  const { user } = useAuth();
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
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [displayMode, setDisplayMode] = useState<"source" | "preview">(
    "source",
  );
  const [mobilePanel, setMobilePanel] = useState<"conversation" | "files">(
    "conversation",
  );
  const [acknowledgedValidationToken, setAcknowledgedValidationToken] =
    useState<string | null>(null);
  const [draftBaselineChecksum, setDraftBaselineChecksum] = useState<
    string | null
  >(null);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [commitOpen, setCommitOpen] = useState(false);
  const [pendingLeave, setPendingLeave] =
    useState<SkillBuilderPendingLeave | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [idempotency] = useState(() => createSkillBuilderIdempotencyRegistry());
  const previousFilesRef = useRef<SkillBuilderFile[]>([]);
  const previousChecksumRef = useRef<string | null>(null);
  const allowLeaveRef = useRef(false);
  const session = sessionQuery.data;
  const pendingUserMessage =
    submitTurn.isPending &&
    submitTurn.variables?.input.kind === "message" &&
    session?.revision === submitTurn.variables.expected_revision
      ? submitTurn.variables.input.message
      : null;
  const canAuthor = skillBuilderCanAuthor(project.capabilities);
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
    cancel.isPending;
  const selectedFile =
    session?.files.find((file) => file.path === selectedPath) ?? null;
  const draftContent = selectedFile
    ? (drafts[selectedFile.path] ?? selectedFile.content)
    : "";
  const fileEditingAllowed =
    session?.status === "draft_ready" || session?.status === "validated";
  const draftBaselineStale = Boolean(
    dirty &&
    draftBaselineChecksum &&
    draftBaselineChecksum !== session?.draft_checksum,
  );
  const builderReadOnly =
    mutationPending || !fileEditingAllowed || draftBaselineStale;
  const validationCurrent = session
    ? skillBuilderValidationCurrent(session)
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
  const canValidate = Boolean(
    session?.draft_checksum &&
    session?.files.some((file) => file.path === "SKILL.md") &&
    (session.status === "draft_ready" || session.status === "validated") &&
    !dirty &&
    !builderReadOnly,
  );

  useEffect(() => {
    if (!session) return;
    const previousFiles = previousFilesRef.current;
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
      setLocalError("候选文件包不能超过 2 MiB。");
      return;
    }
    resetErrors();
    const signature = skillBuilderSemanticSignature({
      kind: "message",
      message,
      expected_revision: session.revision,
    });
    const command = idempotency.acquire("message-turn", signature, (key) => ({
      input: { kind: "message" as const, message },
      expected_revision: session.revision,
      idempotency_key: key,
    }));
    setComposerText("");
    submitTurn.mutate(command, {
      onSuccess: (response) => {
        if (response.data.status === "failed") {
          setComposerText((current) => current || message);
          return;
        }
        idempotency.complete("message-turn", signature);
      },
      onError: (error) => {
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
    const signature = skillBuilderSemanticSignature({
      kind: "clarification",
      response,
      expected_revision: session.revision,
    });
    const command = idempotency.acquire(
      "clarification-turn",
      signature,
      (key) => ({
        input: { kind: "clarification" as const, response },
        expected_revision: session.revision,
        idempotency_key: key,
      }),
    );
    try {
      const result = await submitTurn.mutateAsync(command);
      if (result.data.status === "failed") return false;
      idempotency.complete("clarification-turn", signature);
      return true;
    } catch (error) {
      refreshAfterConflict("clarification-turn", signature, error);
      return false;
    }
  }

  function saveDraft() {
    const draftChecksum = session?.draft_checksum;
    if (
      !session?.draft_checksum ||
      !canAuthor ||
      !fileEditingAllowed ||
      draftBaselineStale ||
      mutationPending ||
      changes.length === 0
    ) {
      return;
    }
    resetErrors();
    const signature = skillBuilderSemanticSignature({
      expected_draft_checksum: draftChecksum,
      expected_revision: session.revision,
      changes,
    });
    const command = idempotency.acquire("draft-turn", signature, (key) => ({
      input: {
        kind: "draft_update" as const,
        expected_draft_checksum: draftChecksum!,
        changes,
      },
      expected_revision: session.revision,
      idempotency_key: key,
    }));
    submitTurn.mutate(command, {
      onSuccess: (response) => {
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
      dirty ||
      !skillBuilderCanCommit(session) ||
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
        router.replace(
          `/projects/${encodeURIComponent(project.slug)}/skills?skill_id=${encodeURIComponent(response.data.skill.id)}`,
        );
      },
      onError: (error) => refreshAfterConflict("commit", signature, error),
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
      ? skillBuilderWorkspaceErrorMessage(submitTurn.error)
      : validate.error
        ? skillBuilderWorkspaceErrorMessage(validate.error)
        : commit.error
          ? skillBuilderWorkspaceErrorMessage(commit.error, true)
          : cancel.error
            ? skillBuilderWorkspaceErrorMessage(cancel.error)
            : null);

  if (!user) return null;

  const listHref = `/projects/${encodeURIComponent(project.slug)}/skills`;

  return (
    <>
      <main className="flex h-[calc(100svh-3.5rem)] min-h-0 flex-col overflow-hidden md:h-screen">
        <header className="border-border/70 bg-background/95 flex min-h-14 shrink-0 items-center gap-3 border-b px-3 backdrop-blur sm:px-5">
          {dirty ? (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              aria-label="返回 Skill 列表"
              onClick={() =>
                setPendingLeave({ href: listHref, viaHistory: false })
              }
            >
              <ArrowLeftIcon aria-hidden className="size-4" />
            </Button>
          ) : (
            <Button asChild type="button" size="icon" variant="ghost">
              <Link href={listHref} aria-label="稍后继续，返回 Skill 列表">
                <ArrowLeftIcon aria-hidden className="size-4" />
              </Link>
            </Button>
          )}
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold">
              {session?.display_name ?? "创建 Skill"}
            </p>
            <p className="text-muted-foreground truncate text-xs">
              {dirty
                ? "有未保存修改"
                : session?.status === "generating"
                  ? "正在生成候选文件"
                  : validationCurrent
                    ? "已检查，可创建"
                    : "已自动保存，可稍后继续"}
            </p>
          </div>
          {canAuthor ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  aria-label="更多操作"
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
                  放弃本次创建
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </header>

        <div
          className="border-border/70 grid shrink-0 grid-cols-2 border-b lg:hidden"
          role="tablist"
          aria-label="Skill Builder 工作区"
        >
          <button
            type="button"
            role="tab"
            aria-selected={mobilePanel === "conversation"}
            className={cn(
              "flex min-h-11 items-center justify-center gap-2 text-sm",
              mobilePanel === "conversation" && "bg-muted font-medium",
            )}
            onClick={() => setMobilePanel("conversation")}
          >
            <MessageSquareIcon aria-hidden className="size-4" />
            对话
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mobilePanel === "files"}
            className={cn(
              "flex min-h-11 items-center justify-center gap-2 text-sm",
              mobilePanel === "files" && "bg-muted font-medium",
            )}
            onClick={() => setMobilePanel("files")}
          >
            <WorkflowIcon aria-hidden className="size-4" />
            文件与检查
          </button>
        </div>

        {sessionQuery.isLoading ? (
          <SkillBuilderLoading />
        ) : sessionQuery.error || !session ? (
          <div className="mx-auto max-w-xl px-4 py-16 text-center">
            <p role="alert" className="text-destructive text-sm">
              {sessionQuery.error
                ? skillBuilderErrorMessage(sessionQuery.error)
                : "Skill 设计会话暂时不可用。"}
            </p>
            <Button
              type="button"
              variant="outline"
              className="mt-4 min-h-11"
              disabled={sessionQuery.isFetching}
              onClick={() => void sessionQuery.refetch()}
            >
              {sessionQuery.isFetching ? "重试中…" : "重试"}
            </Button>
          </div>
        ) : (
          <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(20rem,0.9fr)_minmax(28rem,1.1fr)]">
            <section
              aria-label="Skill 创建对话"
              className={cn(
                "min-h-0 overflow-y-auto overscroll-contain",
                mobilePanel !== "conversation" && "hidden lg:block",
              )}
            >
              <SkillBuilderConversationView
                session={session}
                composerText={composerText}
                pendingUserMessage={pendingUserMessage}
                canAuthor={canAuthor}
                dirty={dirty}
                pending={mutationPending}
                errorMessage={
                  mobilePanel === "conversation" ? requestError : null
                }
                onComposerTextChange={(value) => {
                  setComposerText(value);
                  if (requestError) resetErrors();
                }}
                onSubmitMessage={sendMessage}
                onSubmitClarification={submitClarification}
              />
            </section>
            <section
              aria-label="Skill 候选文件"
              className={cn(
                "border-border/70 min-h-0 border-l",
                mobilePanel !== "files" && "hidden lg:block",
              )}
            >
              <SkillBuilderCandidateWorkbench
                files={session.files}
                selectedPath={selectedPath}
                draftContent={draftContent}
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
                canCommit={skillBuilderCanCommit(session) && !dirty}
                acknowledgeWarnings={acknowledgeWarnings}
                baselineStale={draftBaselineStale}
                errorMessage={mobilePanel === "files" ? requestError : null}
                onSelectPath={(path) => {
                  setSelectedPath(path);
                  setDisplayMode("source");
                  setLocalError(null);
                }}
                onDraftContentChange={(content) => {
                  if (!selectedFile) return;
                  if (
                    new TextEncoder().encode(content).byteLength >
                    SKILL_BUILDER_MAX_FILE_BYTES
                  ) {
                    setLocalError("单个候选文件不能超过 512 KiB。");
                    return;
                  }
                  if (!dirty && content !== selectedFile.content) {
                    setDraftBaselineChecksum(session.draft_checksum);
                  }
                  setDrafts((current) => {
                    const next = { ...current };
                    if (content === selectedFile.content) {
                      delete next[selectedFile.path];
                    } else {
                      next[selectedFile.path] = content;
                    }
                    return next;
                  });
                  if (requestError) resetErrors();
                }}
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
              />
            </section>
          </div>
        )}
      </main>

      <Dialog open={commitOpen} onOpenChange={setCommitOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>创建 Skill？</DialogTitle>
            <DialogDescription>
              将在项目 {project.display_name} 中原子创建并发布版本 1。Skill
              创建后保持停用，不会自动加入任何 Agent。
            </DialogDescription>
          </DialogHeader>
          <div className="bg-muted/35 rounded-xl p-4 text-sm">
            <p className="font-medium">{session?.display_name}</p>
            <p className="text-muted-foreground mt-1 text-xs">
              {session?.files.length ?? 0} 个文件 · 默认停用
            </p>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={commit.isPending}
              onClick={() => setCommitOpen(false)}
            >
              返回检查
            </Button>
            <Button
              type="button"
              disabled={commit.isPending}
              onClick={confirmCommit}
            >
              {commit.isPending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : null}
              {commit.isPending ? "正在创建…" : "确认创建"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={cancelOpen} onOpenChange={setCancelOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>放弃本次 Skill 创建？</DialogTitle>
            <DialogDescription>
              这个设计会话将结束，候选文件包会被清理，且不再显示在未完成列表中。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={cancel.isPending}
              onClick={() => setCancelOpen(false)}
            >
              继续创建
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={cancel.isPending}
              onClick={confirmCancel}
            >
              {cancel.isPending ? "正在放弃…" : "确认放弃"}
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
            <DialogTitle>放弃未保存修改？</DialogTitle>
            <DialogDescription>
              Skill Builder 会话仍会保留，但本次文件修改不会保存。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setPendingLeave(null)}
            >
              继续编辑
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
              放弃修改并离开
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
