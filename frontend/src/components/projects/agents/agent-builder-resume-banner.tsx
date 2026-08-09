"use client";

import {
  ArrowRightIcon,
  Clock3Icon,
  Loader2Icon,
  Trash2Icon,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";

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
  agentBuilderSemanticSignature,
  createAgentBuilderIdempotencyRegistry,
  useCancelAgentBuilderSessionFromList,
  type AgentBuilderSessionSummary,
} from "@/core/agent-builder";

import { agentBuilderErrorMessage } from "./agent-builder-start";
import { agentBuilderSessionPath } from "./agent-builder-workspace";

export function AgentBuilderResumeBannerView({
  projectSlug,
  sessions,
  onDelete,
}: {
  projectSlug: string;
  sessions: AgentBuilderSessionSummary[];
  onDelete: (session: AgentBuilderSessionSummary) => Promise<void>;
}) {
  const [deleteTarget, setDeleteTarget] =
    useState<AgentBuilderSessionSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const unfinished = sessions.filter(
    (session) =>
      session.status !== "completed" && session.status !== "cancelled",
  );

  function closeDeleteDialog() {
    if (deleting) return;
    setDeleteTarget(null);
    setDeleteError(null);
  }

  async function confirmDelete() {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await onDelete(deleteTarget);
      setDeleteTarget(null);
    } catch (error) {
      setDeleteError(agentBuilderErrorMessage(error));
    } finally {
      setDeleting(false);
    }
  }

  if (unfinished.length === 0) return null;

  return (
    <>
      <section
        aria-labelledby="agent-builder-resume-title"
        className="border-border/70 bg-muted/20 mb-5 rounded-2xl border p-4 sm:p-5"
      >
        <div className="mb-3 flex items-center gap-2">
          <Clock3Icon aria-hidden className="text-muted-foreground size-4" />
          <h2 id="agent-builder-resume-title" className="text-sm font-semibold">
            继续设计未完成的 Agent
          </h2>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {unfinished.map((session) => (
            <div
              key={session.id}
              className="bg-background hover:border-foreground/20 flex min-h-12 items-stretch overflow-hidden rounded-xl border transition-colors"
            >
              <Link
                href={agentBuilderSessionPath(projectSlug, session.id)}
                className="focus-visible:ring-ring flex min-w-0 flex-1 items-center gap-3 px-4 py-3 outline-none focus-visible:ring-2 focus-visible:ring-inset"
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">
                    {session.display_name}
                  </span>
                  <span className="text-muted-foreground mt-0.5 block text-xs">
                    上次更新{" "}
                    {new Intl.DateTimeFormat("zh-CN", {
                      month: "numeric",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    }).format(new Date(session.updated_at))}
                  </span>
                </span>
                <ArrowRightIcon aria-hidden className="size-4 shrink-0" />
              </Link>
              <div className="border-border/70 flex items-center border-l px-2">
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  aria-label={`删除未完成的 Agent：${session.display_name}`}
                  onClick={() => {
                    setDeleteError(null);
                    setDeleteTarget(session);
                  }}
                >
                  <Trash2Icon aria-hidden className="size-4" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      </section>

      <Dialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && closeDeleteDialog()}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除未完成的 Agent？</DialogTitle>
            <DialogDescription>
              将删除“{deleteTarget?.display_name}”的设计草稿，之后无法继续。
              已经创建的 Agent 不受影响。
            </DialogDescription>
          </DialogHeader>
          {deleteError ? (
            <p role="alert" className="text-destructive text-sm">
              {deleteError}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={deleting}
              onClick={closeDeleteDialog}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleting}
              onClick={() => void confirmDelete()}
            >
              {deleting ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : null}
              {deleting ? "正在删除…" : "确认删除"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function AgentBuilderResumeBanner({
  accountId,
  projectId,
  projectSlug,
  sessions,
}: {
  accountId: string;
  projectId: string;
  projectSlug: string;
  sessions: AgentBuilderSessionSummary[];
}) {
  const cancel = useCancelAgentBuilderSessionFromList(accountId, projectId);
  const [idempotency] = useState(() => createAgentBuilderIdempotencyRegistry());

  async function deleteSession(session: AgentBuilderSessionSummary) {
    const signature = agentBuilderSemanticSignature({
      session_id: session.id,
      expected_revision: session.revision,
    });
    const command = idempotency.acquire("cancel", signature, (key) => ({
      session_id: session.id,
      expected_revision: session.revision,
      idempotency_key: key,
    }));
    await cancel.mutateAsync(command);
    idempotency.complete("cancel", signature);
  }

  return (
    <AgentBuilderResumeBannerView
      projectSlug={projectSlug}
      sessions={sessions}
      onDelete={deleteSession}
    />
  );
}
