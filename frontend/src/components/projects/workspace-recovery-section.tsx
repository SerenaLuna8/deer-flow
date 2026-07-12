"use client";

import { RotateCcwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useRecoverableProjects,
  useRestoreProject,
} from "@/core/projects/hooks";
import type { Project } from "@/core/projects/types";

import { projectErrorMessage } from "./project-view-model";

function RecoverableProjectRow({
  project,
  userId,
}: {
  project: Project;
  userId: string;
}) {
  const restore = useRestoreProject(userId, project.id);
  return (
    <li className="bg-muted/40 flex flex-col gap-3 rounded-xl p-4 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <p className="font-medium">{project.display_name}</p>
        <p className="text-muted-foreground mt-1 text-xs">
          可恢复至 {project.deletion_effective_at ?? "恢复窗口结束"}
        </p>
        {restore.error && (
          <p role="alert" className="text-destructive mt-2 text-sm">
            {projectErrorMessage(restore.error)}
          </p>
        )}
      </div>
      <Button
        type="button"
        variant="outline"
        disabled={restore.isPending}
        onClick={() => restore.mutate()}
      >
        <RotateCcwIcon aria-hidden className="size-4" />
        恢复项目
      </Button>
    </li>
  );
}

export function WorkspaceRecoverySection({ userId }: { userId: string }) {
  const projects = useRecoverableProjects(userId);
  const recoverable =
    projects.data?.items.filter(
      (project) => project.status === "pending_deletion",
    ) ?? [];

  return (
    <section
      aria-labelledby="workspace-recovery-title"
      aria-label="可恢复项目"
      className="border-border/70 bg-card mb-8 rounded-2xl border p-5"
    >
      <div className="mb-4 flex items-center gap-2">
        <RotateCcwIcon aria-hidden className="text-primary size-5" />
        <h2 id="workspace-recovery-title" className="font-semibold">
          可恢复项目
        </h2>
      </div>
      {projects.isLoading ? (
        <Skeleton className="h-16 rounded-xl" />
      ) : projects.error ? (
        <p role="alert" className="text-muted-foreground text-sm">
          {projectErrorMessage(projects.error)}
        </p>
      ) : recoverable.length ? (
        <ul className="space-y-3">
          {recoverable.map((project) => (
            <RecoverableProjectRow
              key={project.id}
              project={project}
              userId={userId}
            />
          ))}
        </ul>
      ) : (
        <p className="text-muted-foreground text-sm">暂无可恢复项目。</p>
      )}
    </section>
  );
}
