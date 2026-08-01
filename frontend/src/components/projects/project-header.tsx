import { FolderIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { Project } from "@/core/projects/types";

export function ProjectHeader({ project }: { project: Project }) {
  return (
    <header className="border-border/70 h-[4.75rem] border-b">
      <div className="mx-auto flex h-full max-w-6xl items-center gap-3 px-4 py-3 sm:px-6 lg:px-8">
        <div className="bg-primary/10 text-primary flex size-10 shrink-0 items-center justify-center rounded-xl">
          {project.icon === "folder" ? (
            <FolderIcon aria-hidden className="size-5" />
          ) : (
            <span aria-hidden className="text-lg">
              {project.icon}
            </span>
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <h1 className="min-w-0 truncate text-xl font-semibold tracking-tight">
              {project.display_name}
            </h1>
            <Badge variant="secondary">{project.role}</Badge>
          </div>
          <div className="text-muted-foreground mt-0.5 flex min-w-0 items-center gap-2 text-sm">
            <span className="shrink-0 font-mono text-xs">{project.slug}</span>
            <span aria-hidden className="text-border">
              ·
            </span>
            <span className="truncate">
              {project.description || "暂无项目描述"}
            </span>
          </div>
        </div>
      </div>
    </header>
  );
}
