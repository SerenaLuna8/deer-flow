import { FolderIcon } from "lucide-react";

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
        <h1 className="min-w-0 truncate text-xl font-semibold tracking-tight">
          {project.display_name}
        </h1>
      </div>
    </header>
  );
}
