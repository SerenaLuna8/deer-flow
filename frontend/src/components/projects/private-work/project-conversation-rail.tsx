"use client";

import {
  Loader2Icon,
  MenuIcon,
  MessageSquarePlusIcon,
  PencilLineIcon,
  Trash2Icon,
} from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  projectPrivateWorkEntryEnabled,
  useProjectPrivateWorkReadiness,
} from "@/core/private-work/readiness";
import { PROJECT_PRIVATE_WORKSPACE } from "@/core/projects/features";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import {
  useDeleteThread,
  useInfiniteThreads,
  useRenameThread,
} from "@/core/threads/hooks";
import type { AgentThread } from "@/core/threads/types";
import { titleOfThread } from "@/core/threads/utils";
import { formatTimeAgo } from "@/core/utils/datetime";
import { cn } from "@/lib/utils";

import {
  ProjectThreadDeleteDialog,
  projectThreadDeleteLandingPath,
} from "./project-thread-delete-dialog";
import { ProjectThreadRenameDialog } from "./project-thread-rename-dialog";
import { useProjectNewChat } from "./use-project-new-chat";

export function filterProjectConversationThreads(
  threads: AgentThread[],
  search: string,
) {
  const query = search.trim().toLocaleLowerCase();
  if (!query) return threads;
  return threads.filter((thread) =>
    projectConversationTitle(thread).toLocaleLowerCase().includes(query),
  );
}

export function projectConversationTitle(thread: AgentThread): string {
  return titleOfThread(thread).trim() || "新对话";
}

export function projectConversationAutoOpenPath(
  projectSlug: string,
  activeThreadId: string | undefined,
  threads: AgentThread[],
): string | null {
  const firstThread = threads[0];
  if (activeThreadId || !firstThread) return null;
  return `/projects/${encodeURIComponent(projectSlug)}/chats/${encodeURIComponent(firstThread.thread_id)}`;
}

export function projectConversationPermissions(project: Project) {
  return {
    canCreate:
      project.capabilities.includes("private_work.create") &&
      project.capabilities.includes("shared_assets.execute"),
    canDelete: project.capabilities.includes("private_work.read_own"),
    canRename: project.capabilities.includes("private_work.create"),
  };
}

function ConversationRailContent({
  activeThreadId,
  canCreate,
  canDelete,
  canRename,
  entryEnabled,
  isCreating,
  filteredThreads,
  project,
  search,
  threads,
  threadsQuery,
  onDelete,
  onNavigate,
  onNewConversation,
  onRename,
  onSearchChange,
}: {
  activeThreadId?: string;
  canCreate: boolean;
  canDelete: boolean;
  canRename: boolean;
  entryEnabled: boolean;
  isCreating: boolean;
  filteredThreads: AgentThread[];
  project: Project;
  search: string;
  threads: AgentThread[];
  threadsQuery: ReturnType<typeof useInfiniteThreads>;
  onDelete: (thread: AgentThread) => void;
  onNavigate?: () => void;
  onNewConversation: () => void;
  onRename: (thread: AgentThread) => void;
  onSearchChange: (value: string) => void;
}) {
  return (
    <div
      className="flex size-full min-h-0 flex-col"
      data-testid="project-conversation-rail"
    >
      <header className="border-border/70 space-y-3 border-b p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-base font-semibold">会话</h1>
            <p className="text-muted-foreground mt-0.5 truncate text-xs">
              {project.display_name}
            </p>
          </div>
          {canCreate && (
            <Button
              type="button"
              size="sm"
              disabled={!entryEnabled || isCreating}
              aria-disabled={!entryEnabled || isCreating}
              onClick={onNewConversation}
            >
              <MessageSquarePlusIcon aria-hidden className="size-4" />
              新建
            </Button>
          )}
        </div>
        <Input
          type="search"
          value={search}
          placeholder="搜索会话"
          aria-label="搜索会话"
          onChange={(event) => onSearchChange(event.target.value)}
        />
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {threadsQuery.isLoading ? (
          <div
            className="text-muted-foreground flex items-center justify-center gap-2 px-3 py-8 text-sm"
            role="status"
          >
            <Loader2Icon aria-hidden className="size-4 animate-spin" />
            正在加载会话…
          </div>
        ) : threadsQuery.error ? (
          <div className="space-y-3 px-3 py-8 text-center">
            <p className="text-muted-foreground text-sm">无法加载会话。</p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void threadsQuery.refetch()}
            >
              重试
            </Button>
          </div>
        ) : filteredThreads.length === 0 ? (
          <p className="text-muted-foreground px-3 py-8 text-center text-sm">
            {threads.length === 0 ? "还没有会话" : "没有匹配的会话"}
          </p>
        ) : (
          <ul className="space-y-1">
            {filteredThreads.map((thread) => {
              const title = projectConversationTitle(thread);
              const active = thread.thread_id === activeThreadId;
              return (
                <li
                  key={thread.thread_id}
                  className={cn(
                    "group rounded-xl px-2 py-2 transition-colors",
                    active
                      ? "bg-accent text-accent-foreground"
                      : "hover:bg-accent/60",
                  )}
                >
                  <div className="relative min-w-0">
                    <Link
                      href={`/projects/${encodeURIComponent(project.slug)}/chats/${encodeURIComponent(thread.thread_id)}`}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "block truncate text-sm font-medium",
                        canRename && canDelete
                          ? "group-focus-within:pr-14 group-hover:pr-14"
                          : canRename || canDelete
                            ? "group-focus-within:pr-7 group-hover:pr-7"
                            : null,
                      )}
                      onClick={onNavigate}
                    >
                      {title}
                    </Link>
                    {(canRename || canDelete) && (
                      <div className="pointer-events-none absolute top-1/2 right-0 flex -translate-y-1/2 items-center opacity-0 transition-opacity group-focus-within:pointer-events-auto group-focus-within:opacity-100 group-hover:pointer-events-auto group-hover:opacity-100">
                        {canRename && (
                          <Button
                            type="button"
                            size="icon-sm"
                            variant="ghost"
                            className="text-muted-foreground size-7"
                            aria-label={`重命名 ${title}`}
                            title="重命名"
                            onClick={() => onRename(thread)}
                          >
                            <PencilLineIcon aria-hidden className="size-3.5" />
                          </Button>
                        )}
                        {canDelete && (
                          <Button
                            type="button"
                            size="icon-sm"
                            variant="ghost"
                            className="text-muted-foreground size-7"
                            aria-label={`删除 ${title}`}
                            onClick={() => onDelete(thread)}
                          >
                            <Trash2Icon aria-hidden className="size-3.5" />
                          </Button>
                        )}
                      </div>
                    )}
                  </div>
                  {thread.updated_at ? (
                    <Link
                      href={`/projects/${encodeURIComponent(project.slug)}/chats/${encodeURIComponent(thread.thread_id)}`}
                      tabIndex={-1}
                      className="text-muted-foreground mt-1 block text-xs"
                      onClick={onNavigate}
                    >
                      {formatTimeAgo(thread.updated_at)}
                    </Link>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {threadsQuery.hasNextPage && (
        <div className="border-border/70 border-t p-3">
          <Button
            type="button"
            className="w-full"
            variant="outline"
            disabled={threadsQuery.isFetchingNextPage}
            onClick={() => void threadsQuery.fetchNextPage()}
          >
            {threadsQuery.isFetchingNextPage ? "正在加载…" : "加载更多"}
          </Button>
        </div>
      )}
    </div>
  );
}

export function ProjectConversationRail({ project }: { project: Project }) {
  const router = useRouter();
  const params = useParams<{ thread_id?: string }>();
  const activeThreadId =
    typeof params.thread_id === "string" ? params.thread_id : undefined;
  const privateWork = usePrivateWorkAccess();
  const threadsQuery = useInfiniteThreads();
  const deleteThread = useDeleteThread(privateWork);
  const renameThread = useRenameThread(privateWork);
  const newChat = useProjectNewChat(project);
  const [search, setSearch] = useState("");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<AgentThread | null>(null);
  const [renameTarget, setRenameTarget] = useState<AgentThread | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const autoOpenedPathRef = useRef<string | null>(null);
  const threads = useMemo(
    () => threadsQuery.data?.pages.flat() ?? [],
    [threadsQuery.data],
  );
  const filteredThreads = useMemo(
    () => filterProjectConversationThreads(threads, search),
    [search, threads],
  );
  const { canCreate, canDelete, canRename } =
    projectConversationPermissions(project);
  const staticWebsiteOnly = isStaticWebsiteOnly();
  const readiness = useProjectPrivateWorkReadiness(
    canCreate && !staticWebsiteOnly,
  );
  const entryEnabled = projectPrivateWorkEntryEnabled(
    PROJECT_PRIVATE_WORKSPACE,
    canCreate,
    readiness.data?.status,
  );
  const autoOpenPath =
    !staticWebsiteOnly &&
    threadsQuery.isSuccess &&
    !threadsQuery.isFetching &&
    !deleteThread.isPending
      ? projectConversationAutoOpenPath(project.slug, activeThreadId, threads)
      : null;

  useEffect(() => {
    if (!autoOpenPath) {
      autoOpenedPathRef.current = null;
      return;
    }
    if (autoOpenedPathRef.current === autoOpenPath) return;
    autoOpenedPathRef.current = autoOpenPath;
    router.replace(autoOpenPath);
  }, [autoOpenPath, router]);

  if (staticWebsiteOnly) return null;

  const openNewConversation = () => {
    setMobileOpen(false);
    void newChat.startNewChat();
  };

  const confirmDelete = async () => {
    const target = deleteTarget;
    if (!target || deleteThread.isPending) return;
    const landingPath = projectThreadDeleteLandingPath(
      project.slug,
      activeThreadId,
      target.thread_id,
    );
    try {
      await deleteThread.mutateAsync({
        threadId: target.thread_id,
        ...(landingPath
          ? {
              onRemoteDeleted: () => {
                setMobileOpen(false);
                router.replace(landingPath);
              },
            }
          : {}),
      });
      setDeleteTarget(null);
      toast.success("对话已删除");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法删除对话");
    }
  };

  const openRename = (thread: AgentThread) => {
    setRenameTarget(thread);
    setRenameValue(projectConversationTitle(thread));
  };

  const confirmRename = async (title: string) => {
    const target = renameTarget;
    if (!target || renameThread.isPending) return;
    if (title === projectConversationTitle(target)) {
      setRenameTarget(null);
      return;
    }
    try {
      await renameThread.mutateAsync({
        threadId: target.thread_id,
        title,
      });
      setRenameTarget(null);
      toast.success("会话标题已更新");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "无法重命名会话");
    }
  };

  const rail = (onNavigate?: () => void) => (
    <ConversationRailContent
      activeThreadId={activeThreadId}
      canCreate={canCreate}
      canDelete={canDelete}
      canRename={canRename}
      entryEnabled={entryEnabled}
      isCreating={newChat.isCreating || newChat.isLoading}
      filteredThreads={filteredThreads}
      project={project}
      search={search}
      threads={threads}
      threadsQuery={threadsQuery}
      onDelete={setDeleteTarget}
      onNavigate={onNavigate}
      onNewConversation={openNewConversation}
      onRename={openRename}
      onSearchChange={setSearch}
    />
  );

  return (
    <>
      <aside className="border-border/70 bg-card/50 hidden h-full w-64 shrink-0 border-r xl:flex">
        {rail()}
      </aside>

      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetTrigger asChild>
          <Button
            type="button"
            size="icon"
            variant="outline"
            className="bg-background/90 absolute top-2 left-2 z-40 rounded-full shadow-sm backdrop-blur xl:hidden"
            aria-label="打开会话列表"
            data-testid="project-conversation-rail-trigger"
          >
            <MenuIcon aria-hidden className="size-4" />
          </Button>
        </SheetTrigger>
        <SheetContent side="left" className="w-[min(16rem,88vw)] gap-0 p-0">
          <SheetHeader className="sr-only">
            <SheetTitle>项目会话</SheetTitle>
            <SheetDescription>
              浏览、搜索或新建当前项目的会话。
            </SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 pt-10">
            {rail(() => setMobileOpen(false))}
          </div>
        </SheetContent>
      </Sheet>

      <ProjectThreadDeleteDialog
        open={deleteTarget !== null}
        title={deleteTarget ? titleOfThread(deleteTarget) : ""}
        pending={deleteThread.isPending}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        onConfirm={() => void confirmDelete()}
      />

      <ProjectThreadRenameDialog
        open={renameTarget !== null}
        value={renameValue}
        pending={renameThread.isPending}
        onValueChange={setRenameValue}
        onOpenChange={(open) => {
          if (!open) setRenameTarget(null);
        }}
        onConfirm={(title) => void confirmRename(title)}
      />
    </>
  );
}
