"use client";

import Link from "next/link";

import { useProjectPrivateWorkReadiness } from "@/core/private-work/readiness";
import { PROJECT_PRIVATE_WORKSPACE } from "@/core/projects/features";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { useThreads } from "@/core/threads/hooks";
import type { AgentThread } from "@/core/threads/types";
import { titleOfThread } from "@/core/threads/utils";
import { formatTimeAgo } from "@/core/utils/datetime";

export const RECENT_PRIVATE_WORK_LIMIT = 5;

export function RecentPrivateWorkView({
  projectSlug,
  threads,
}: {
  projectSlug: string;
  threads: AgentThread[];
}) {
  return (
    <section className="border-border/70 bg-card rounded-2xl border p-6">
      <div className="flex items-center justify-between gap-4">
        <h2 className="font-semibold">最近私有对话</h2>
        <Link
          href={`/projects/${encodeURIComponent(projectSlug)}/chats`}
          className="text-primary text-sm hover:underline"
        >
          查看全部
        </Link>
      </div>
      {threads.length === 0 ? (
        <p className="text-muted-foreground mt-4 text-sm">还没有私有对话</p>
      ) : (
        <div className="mt-4 divide-y">
          {threads.map((thread) => (
            <Link
              key={thread.thread_id}
              href={`/projects/${encodeURIComponent(projectSlug)}/chats/${encodeURIComponent(thread.thread_id)}`}
              className="flex items-center justify-between gap-4 py-3 text-sm hover:underline"
            >
              <span className="truncate">{titleOfThread(thread)}</span>
              {thread.updated_at && (
                <span className="text-muted-foreground shrink-0 text-xs">
                  {formatTimeAgo(thread.updated_at)}
                </span>
              )}
            </Link>
          ))}
        </div>
      )}
    </section>
  );
}

export function RecentPrivateWork({ project }: { project: Project }) {
  const canRead = project.capabilities.includes("private_work.read_own");
  const staticWebsiteOnly = isStaticWebsiteOnly();
  const readinessEnabled =
    PROJECT_PRIVATE_WORKSPACE && canRead && !staticWebsiteOnly;
  const readiness = useProjectPrivateWorkReadiness(readinessEnabled);
  const enabled = readinessEnabled && readiness.data?.status === "ready";
  const threads = useThreads(
    {
      limit: RECENT_PRIVATE_WORK_LIMIT,
      offset: 0,
      sortBy: "updated_at",
      sortOrder: "desc",
      select: ["thread_id", "updated_at", "values", "metadata"],
    },
    undefined,
    { enabled },
  );
  if (!enabled) return null;
  return (
    <RecentPrivateWorkView
      projectSlug={project.slug}
      threads={threads.data ?? []}
    />
  );
}
