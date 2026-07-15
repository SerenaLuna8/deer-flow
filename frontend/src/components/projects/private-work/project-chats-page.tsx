"use client";

import { Trash2Icon } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  projectPrivateWorkEntryEnabled,
  useProjectPrivateWorkReadiness,
} from "@/core/private-work/readiness";
import { PROJECT_PRIVATE_WORKSPACE } from "@/core/projects/features";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { useDeleteThread, useInfiniteThreads } from "@/core/threads/hooks";
import { titleOfThread } from "@/core/threads/utils";
import { formatTimeAgo } from "@/core/utils/datetime";

import { ProjectAgentSelectorDialog } from "./agent-selector-dialog";

export function ProjectChatsPage({ project }: { project: Project }) {
  const [search, setSearch] = useState("");
  const [selectorOpen, setSelectorOpen] = useState(false);
  const privateWork = usePrivateWorkAccess();
  const deleteThread = useDeleteThread(privateWork);
  const threadsQuery = useInfiniteThreads();
  const threads = useMemo(
    () => threadsQuery.data?.pages.flat() ?? [],
    [threadsQuery.data],
  );
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return query
      ? threads.filter((thread) =>
          titleOfThread(thread).toLocaleLowerCase().includes(query),
        )
      : threads;
  }, [search, threads]);
  const canCreate =
    project.capabilities.includes("private_work.create") &&
    project.capabilities.includes("shared_assets.execute");
  const staticWebsiteOnly = isStaticWebsiteOnly();
  const readiness = useProjectPrivateWorkReadiness(
    canCreate && !staticWebsiteOnly,
  );
  const entryEnabled = projectPrivateWorkEntryEnabled(
    PROJECT_PRIVATE_WORKSPACE,
    canCreate,
    readiness.data?.status,
  );
  const canDelete = project.capabilities.includes("private_work.read_own");

  if (staticWebsiteOnly) return null;
  return (
    <main className="mx-auto w-full max-w-4xl space-y-6 px-4 py-8 sm:px-6">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold">私有对话</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            这里只显示你在当前项目中的对话。
          </p>
        </div>
        {canCreate ? (
          <Button
            type="button"
            disabled={!entryEnabled}
            aria-disabled={!entryEnabled}
            onClick={() => setSelectorOpen(true)}
          >
            新建对话
          </Button>
        ) : (
          <p className="text-muted-foreground text-sm">
            你可以查看自己的既有对话，但不能创建新工作
          </p>
        )}
      </header>
      <Input
        type="search"
        value={search}
        placeholder="搜索对话"
        aria-label="搜索对话"
        onChange={(event) => setSearch(event.target.value)}
      />
      <section className="divide-y rounded-2xl border px-4">
        {filtered.length === 0 ? (
          <p className="text-muted-foreground py-8 text-center text-sm">
            还没有私有对话
          </p>
        ) : (
          filtered.map((thread) => (
            <div
              key={thread.thread_id}
              className="flex items-center gap-2 py-2"
            >
              <Link
                href={`/projects/${encodeURIComponent(project.slug)}/chats/${encodeURIComponent(thread.thread_id)}`}
                className="flex min-w-0 flex-1 items-center justify-between gap-4 py-2 hover:underline"
              >
                <span className="truncate">{titleOfThread(thread)}</span>
                {thread.updated_at && (
                  <span className="text-muted-foreground shrink-0 text-xs">
                    {formatTimeAgo(thread.updated_at)}
                  </span>
                )}
              </Link>
              {canDelete && (
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  aria-label={`删除 ${titleOfThread(thread)}`}
                  disabled={deleteThread.isPending}
                  onClick={() =>
                    deleteThread.mutate({ threadId: thread.thread_id })
                  }
                >
                  <Trash2Icon aria-hidden className="size-4" />
                </Button>
              )}
            </div>
          ))
        )}
      </section>
      {threadsQuery.hasNextPage && (
        <Button
          type="button"
          variant="outline"
          disabled={threadsQuery.isFetchingNextPage}
          onClick={() => void threadsQuery.fetchNextPage()}
        >
          {threadsQuery.isFetchingNextPage ? "正在加载…" : "加载更多"}
        </Button>
      )}
      {selectorOpen && (
        <ProjectAgentSelectorDialog
          project={project}
          open
          onOpenChange={setSelectorOpen}
        />
      )}
    </main>
  );
}
