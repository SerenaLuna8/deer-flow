import { ArrowLeftIcon, FolderIcon } from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import type { Project } from "@/core/projects/types";

export function ProjectHeader({ project }: { project: Project }) {
  return (
    <header className="border-border/70 border-b">
      <div className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
        <Link
          href="/workspace"
          className="text-muted-foreground hover:text-foreground focus-visible:ring-ring inline-flex w-fit items-center gap-2 rounded text-sm focus-visible:ring-2 focus-visible:outline-none"
        >
          <ArrowLeftIcon size={16} /> 返回工作空间
        </Link>
        <div className="flex items-start gap-4">
          <div className="bg-primary/10 text-primary flex size-14 shrink-0 items-center justify-center rounded-2xl">
            {project.icon === "folder" ? (
              <FolderIcon aria-hidden size={26} />
            ) : (
              <span aria-hidden className="text-2xl">
                {project.icon}
              </span>
            )}
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-3xl font-semibold tracking-tight">
                {project.display_name}
              </h1>
              <Badge variant="secondary">{project.role}</Badge>
            </div>
            <p className="text-muted-foreground mt-1 font-mono text-xs">
              {project.slug}
            </p>
            <p className="text-muted-foreground mt-3 max-w-2xl">
              {project.description || "暂无项目描述"}
            </p>
          </div>
        </div>
      </div>
    </header>
  );
}
