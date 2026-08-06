"use client";

import {
  ArrowLeftIcon,
  BotIcon,
  Loader2Icon,
  MoreHorizontalIcon,
  SendIcon,
  Trash2Icon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

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
import { HumanInputCard } from "@/components/workspace/messages/human-input-card";
import {
  AgentBuilderApiError,
  agentBuilderBlueprintValidationError,
  agentBuilderCanAuthor,
  agentBuilderCanComplete,
  agentBuilderComposerDisabled,
  agentBuilderSemanticSignature,
  createAgentBuilderIdempotencyRegistry,
  useAgentBuilderSession,
  useCancelAgentBuilderSession,
  useCommitAgentBuilderSession,
  useSubmitAgentBuilderTurn,
  type AgentBuilderBlueprint,
  type AgentBuilderSession,
} from "@/core/agent-builder";
import { useAuth } from "@/core/auth/AuthProvider";
import type { HumanInputResponse } from "@/core/messages/human-input";
import { SafeStreamdown } from "@/core/streamdown/components";
import { isIMEComposing } from "@/lib/ime";

import { useMcpDependencyRuntime } from "../assets/use-mcp-dependency-runtime";
import { useCurrentProject } from "../project-context";

import { AgentBuilderBlueprintReview } from "./agent-builder-blueprint-review";
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
    return "创建结果暂时无法确认。请勿重复创建，先返回列表检查同名 Agent；若未出现再重试。";
  }
  if (
    error instanceof AgentBuilderApiError &&
    error.code === "AGENT_BUILDER_CONFLICT"
  ) {
    return "设计内容已发生变化，请刷新到最新状态后再继续。";
  }
  return agentBuilderErrorMessage(error);
}

function sameBlueprint(
  left: AgentBuilderBlueprint | null,
  right: AgentBuilderBlueprint | null,
) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export async function submitAgentBuilderClarificationMutation(
  operation: () => Promise<unknown>,
  onResolved: () => void = () => undefined,
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
      return false;
    }
    onResolved();
    return true;
  } catch {
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
  canAuthor,
  mutationPending,
  commitPending,
  blueprintEditing,
  blueprintDraft,
  blueprintDirty,
  mcpDependencyLoading = false,
  mcpDependencyBlockReason = null,
  selectedField,
  displayMode,
  errorMessage,
  onComposerTextChange,
  onSubmitMessage,
  onSubmitClarification,
  onSelectedFieldChange,
  onDisplayModeChange,
  onBlueprintChange,
  onBlueprintEdit,
  onBlueprintSave,
  onBlueprintDiscard,
  onComplete,
}: {
  session: AgentBuilderSession;
  composerText: string;
  canAuthor: boolean;
  mutationPending: boolean;
  commitPending: boolean;
  blueprintEditing: boolean;
  blueprintDraft: AgentBuilderBlueprint | null;
  blueprintDirty: boolean;
  mcpDependencyLoading?: boolean;
  mcpDependencyBlockReason?: string | null;
  selectedField: AgentInstructionField;
  displayMode: "source" | "preview";
  errorMessage: string | null;
  onComposerTextChange: (value: string) => void;
  onSubmitMessage: () => void;
  onSubmitClarification: (
    response: HumanInputResponse,
  ) => boolean | void | Promise<boolean | void>;
  onSelectedFieldChange: (field: AgentInstructionField) => void;
  onDisplayModeChange: (mode: "source" | "preview") => void;
  onBlueprintChange: (blueprint: AgentBuilderBlueprint) => void;
  onBlueprintEdit: () => void;
  onBlueprintSave: () => void;
  onBlueprintDiscard: () => void;
  onComplete: () => void;
}) {
  const localDraftLocked = blueprintEditing || blueprintDirty;
  const composerDisabled =
    agentBuilderComposerDisabled(session, mutationPending, localDraftLocked) ||
    !canAuthor;
  const clarificationOpen = Boolean(session.active_clarification);
  const generating =
    session.status === "generating" || session.status === "committing";

  function submitMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmitMessage();
  }

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col px-4 pt-7 pb-5 sm:px-6 sm:pt-10">
      <div className="space-y-7">
        <AgentBuilderProgress
          items={session.progress}
          generating={generating}
          documentsReady={Boolean(session.blueprint)}
        />

        {!canAuthor ? (
          <p
            role="alert"
            className="border-border/70 bg-muted/20 text-muted-foreground rounded-xl border px-4 py-3 text-sm"
          >
            当前账号没有继续设计 Agent 的权限。你仍可查看已保存的会话内容。
          </p>
        ) : null}

        {session.messages.length === 0 ? (
          <div className="flex justify-end">
            <p className="bg-muted max-w-[90%] rounded-2xl rounded-br-md px-4 py-3 text-sm leading-6">
              新 Agent 的名称是{" "}
              <span className="font-semibold">{session.display_name}</span>
              。请通过下面的对话描述它的用途、行为方式和协作边界。
            </p>
          </div>
        ) : null}

        {session.messages.map((message) => (
          <AgentBuilderMessageBubble
            key={message.id}
            role={message.role}
            content={message.content}
          />
        ))}

        {session.active_clarification ? (
          <section className="border-border/70 rounded-2xl border px-4 sm:px-5">
            <HumanInputCard
              key={session.active_clarification.request_id}
              request={session.active_clarification}
              disabled={!canAuthor || localDraftLocked}
              pending={mutationPending}
              onSubmit={(response) => onSubmitClarification(response)}
            />
          </section>
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

      {canAuthor ? (
        <form
          className="bg-background/95 border-border/70 sticky bottom-0 mt-10 rounded-2xl border p-2 shadow-lg backdrop-blur"
          onSubmit={submitMessage}
        >
          <div aria-disabled={composerDisabled}>
            <Textarea
              aria-label="描述想要的 Agent"
              value={composerText}
              disabled={composerDisabled}
              placeholder={
                localDraftLocked
                  ? "请先保存或放弃上方修改"
                  : clarificationOpen
                    ? "等待你回答上方问题"
                    : generating
                      ? "正在生成 Agent 设计稿…"
                      : "描述你想要的 Agent，我来帮你通过对话创建。"
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
            <div className="flex justify-end p-1">
              <Button
                type="submit"
                size="icon"
                className="size-11 rounded-xl"
                aria-label="发送"
                disabled={composerDisabled || composerText.trim().length === 0}
              >
                {mutationPending ? (
                  <Loader2Icon aria-hidden className="size-4 animate-spin" />
                ) : (
                  <SendIcon aria-hidden className="size-4" />
                )}
              </Button>
            </div>
          </div>
        </form>
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
  const { user } = useAuth();
  const project = useCurrentProject();
  const router = useRouter();
  const accountId = user?.id ?? "";
  const sessionQuery = useAgentBuilderSession(accountId, project.id, sessionId);
  const submitTurn = useSubmitAgentBuilderTurn(
    accountId,
    project.id,
    sessionId,
  );
  const commit = useCommitAgentBuilderSession(accountId, project.id, sessionId);
  const cancel = useCancelAgentBuilderSession(accountId, project.id, sessionId);
  const [composerText, setComposerText] = useState("");
  const [blueprintBaseline, setBlueprintBaseline] =
    useState<AgentBuilderBlueprint | null>(null);
  const [blueprintDraft, setBlueprintDraft] =
    useState<AgentBuilderBlueprint | null>(null);
  const [blueprintEditing, setBlueprintEditing] = useState(false);
  const [selectedField, setSelectedField] = useState<AgentInstructionField>(
    "agents_instructions",
  );
  const [displayMode, setDisplayMode] = useState<"source" | "preview">(
    "preview",
  );
  const [cancelOpen, setCancelOpen] = useState(false);
  const [pendingLeaveHref, setPendingLeaveHref] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [idempotency] = useState(() => createAgentBuilderIdempotencyRegistry());
  const scrollRef = useRef<HTMLElement | null>(null);
  const allowLeaveRef = useRef(false);
  const session = sessionQuery.data;
  const canAuthor = agentBuilderCanAuthor(project.capabilities);
  const blueprintDirty = useMemo(
    () => !sameBlueprint(blueprintBaseline, blueprintDraft),
    [blueprintBaseline, blueprintDraft],
  );
  const mutationPending =
    submitTurn.isPending || commit.isPending || cancel.isPending;
  const effectiveBlueprint = blueprintDraft ?? session?.blueprint ?? null;
  const mcpDependencyRuntime = useMcpDependencyRuntime({
    accountId,
    projectId: project.id,
    requiredVersionIds: effectiveBlueprint?.mcp_version_ids ?? [],
    enabled: Boolean(session),
  });

  useEffect(() => {
    if (!session?.blueprint || blueprintEditing || blueprintDirty) return;
    setBlueprintBaseline(session.blueprint);
    setBlueprintDraft(session.blueprint);
  }, [blueprintDirty, blueprintEditing, session?.blueprint]);

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
    session?.active_clarification,
  ]);

  useEffect(() => {
    if (!blueprintDirty) return;
    const preventDirtyUnload = (event: BeforeUnloadEvent) => {
      if (allowLeaveRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventDirtyUnload);
    return () => window.removeEventListener("beforeunload", preventDirtyUnload);
  }, [blueprintDirty]);

  useEffect(() => {
    if (!blueprintDirty) return;
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
  }, [blueprintDirty]);

  function resetErrors() {
    setLocalError(null);
    submitTurn.reset();
    commit.reset();
    cancel.reset();
  }

  function sendMessage() {
    const message = composerText.trim();
    if (
      !session ||
      !message ||
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
    });
    const command = idempotency.acquire("message-turn", signature, (key) => ({
      input: { kind: "message" as const, message },
      idempotency_key: key,
      expected_revision: session.revision,
    }));
    submitTurn.mutate(command, {
      onSuccess: (response) => {
        if (response.data.status === "failed") return;
        idempotency.complete("message-turn", signature);
        setComposerText("");
      },
    });
  }

  function submitClarification(
    response: HumanInputResponse,
  ): Promise<boolean> | false {
    if (
      !session ||
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
    });
    const command = idempotency.acquire(
      "clarification-turn",
      signature,
      (key) => ({
        input: { kind: "clarification" as const, response },
        idempotency_key: key,
        expected_revision: session.revision,
      }),
    );
    return submitAgentBuilderClarificationMutation(
      () => submitTurn.mutateAsync(command),
      () => {
        idempotency.complete("clarification-turn", signature);
      },
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
    const blueprintError = agentBuilderBlueprintValidationError(blueprintDraft);
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
      !agentBuilderCanComplete(session)
    ) {
      return;
    }
    resetErrors();
    const signature = agentBuilderSemanticSignature({
      blueprint_checksum: blueprintChecksum,
    });
    const command = idempotency.acquire("commit", signature, (key) => ({
      idempotency_key: key,
      expected_revision: session.revision,
      expected_blueprint_checksum: blueprintChecksum,
    }));
    commit.mutate(command, {
      onSuccess: (response) => {
        idempotency.complete("commit", signature);
        router.replace(
          `/projects/${encodeURIComponent(project.slug)}/agents?agent_id=${encodeURIComponent(response.data.agent.id)}`,
        );
      },
    });
  }

  function discardBlueprint() {
    setBlueprintDraft(blueprintBaseline);
    setBlueprintEditing(false);
    setDisplayMode("preview");
    setLocalError(null);
  }

  function confirmCancel() {
    if (!session || !canAuthor || mutationPending) return;
    resetErrors();
    const signature = agentBuilderSemanticSignature({
      session_id: session.id,
    });
    const command = idempotency.acquire("cancel", signature, (key) => ({
      idempotency_key: key,
      expected_revision: session.revision,
    }));
    cancel.mutate(command, {
      onSuccess: () => {
        idempotency.complete("cancel", signature);
        setCancelOpen(false);
        router.replace(`/projects/${encodeURIComponent(project.slug)}/agents`);
      },
    });
  }

  const requestError =
    localError ??
    (submitTurn.error
      ? agentBuilderWorkspaceErrorMessage(submitTurn.error)
      : commit.error
        ? agentBuilderWorkspaceErrorMessage(commit.error, true)
        : cancel.error
          ? agentBuilderWorkspaceErrorMessage(cancel.error)
          : null);

  if (!user) return null;

  return (
    <>
      <main className="flex h-[calc(100svh-3.5rem)] min-h-0 flex-col overflow-hidden md:h-screen">
        <header className="border-border/70 bg-background/95 flex min-h-14 shrink-0 items-center gap-3 border-b px-3 backdrop-blur sm:px-5">
          <Button asChild type="button" size="icon" variant="ghost">
            <Link
              href={`/projects/${encodeURIComponent(project.slug)}/agents`}
              aria-label="稍后继续，返回 Agent 列表"
            >
              <ArrowLeftIcon aria-hidden className="size-4" />
            </Link>
          </Button>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold">
              {session?.display_name ?? "设计 Agent"}
            </p>
            <p className="text-muted-foreground truncate text-xs">
              自动保存，可稍后继续
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
                  onSelect={() => setCancelOpen(true)}
                >
                  <Trash2Icon aria-hidden className="size-4" />
                  放弃本次设计
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </header>

        <section
          ref={scrollRef}
          className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
          aria-label="Agent 设计对话"
        >
          {sessionQuery.isLoading ? (
            <AgentBuilderLoading />
          ) : sessionQuery.error || !session ? (
            <div className="mx-auto max-w-xl px-4 py-16 text-center">
              <p role="alert" className="text-destructive text-sm">
                {sessionQuery.error
                  ? agentBuilderErrorMessage(sessionQuery.error)
                  : "Agent 设计会话暂时不可用。"}
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
            <AgentBuilderConversationView
              session={session}
              composerText={composerText}
              canAuthor={canAuthor}
              mutationPending={mutationPending}
              commitPending={commit.isPending}
              blueprintEditing={blueprintEditing}
              blueprintDraft={blueprintDraft ?? session.blueprint}
              blueprintDirty={blueprintDirty}
              mcpDependencyLoading={mcpDependencyRuntime.isLoading}
              mcpDependencyBlockReason={mcpDependencyRuntime.blockReason}
              selectedField={selectedField}
              displayMode={displayMode}
              errorMessage={requestError}
              onComposerTextChange={(value) => {
                setComposerText(value);
                if (requestError) resetErrors();
              }}
              onSubmitMessage={sendMessage}
              onSubmitClarification={submitClarification}
              onSelectedFieldChange={setSelectedField}
              onDisplayModeChange={setDisplayMode}
              onBlueprintChange={setBlueprintDraft}
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
            <DialogTitle>放弃本次 Agent 设计？</DialogTitle>
            <DialogDescription>
              这个设计会话将结束，且不再显示在未完成列表中。已创建的 Agent
              不受影响。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={cancel.isPending}
              onClick={() => setCancelOpen(false)}
            >
              继续设计
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={!canAuthor || cancel.isPending}
              onClick={confirmCancel}
            >
              {cancel.isPending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : null}
              {cancel.isPending ? "正在放弃…" : "确认放弃"}
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
            <DialogTitle>放弃未保存修改？</DialogTitle>
            <DialogDescription>
              Agent 设计会话会继续保留，但本次对四项设置的本地修改不会保存。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setPendingLeaveHref(null)}
            >
              继续编辑
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
              放弃修改并离开
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
