"use client";

import { ArrowRightIcon, FolderIcon, PencilIcon, PinIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import type { Project } from "@/core/projects/types";
import { cn } from "@/lib/utils";

import { canUpdateProject } from "./project-view-model";

export function ProjectCard({
  project,
  onPin,
  onEdit,
  pinPending = false,
}: {
  project: Project;
  onPin: () => void;
  onEdit: () => void;
  pinPending?: boolean;
}) {
  const { t } = useI18n();
  const copy = t.projectWorkspace.card;
  return (
    <article
      data-testid="project-card"
      className="group hover:bg-muted/20 focus-within:ring-ring/50 grid min-w-0 gap-5 px-5 py-5 transition-colors focus-within:ring-[3px] focus-within:ring-inset xl:grid-cols-[minmax(18rem,1.2fr)_minmax(14rem,1fr)_minmax(23rem,auto)] xl:items-center xl:px-8 xl:py-6"
    >
      <div className="flex min-w-0 items-center gap-4 lg:gap-6">
        <div className="bg-muted text-primary flex size-16 shrink-0 items-center justify-center rounded-xl lg:size-[4.5rem] lg:rounded-2xl">
          {project.icon === "folder" ? (
            <FolderIcon aria-hidden className="size-7 lg:size-8" />
          ) : (
            <span aria-hidden className="text-xl">
              {project.icon}
            </span>
          )}
        </div>
        <div className="min-w-0">
          <h3 className="truncate text-lg font-semibold lg:text-xl">
            {project.display_name}
          </h3>
          <p className="text-muted-foreground mt-1 truncate font-mono text-xs">
            {project.slug}
          </p>
        </div>
      </div>

      <p className="text-muted-foreground line-clamp-2 text-sm lg:text-base">
        {project.description || copy.noDescription}
      </p>

      <div className="flex flex-wrap items-center gap-1 border-t pt-4 xl:justify-end xl:border-t-0 xl:pt-0">
        <Button
          type="button"
          variant="ghost"
          className={cn(
            "h-11 px-3 text-sm lg:text-base",
            project.is_pinned && "text-selection hover:text-selection",
          )}
          aria-label={project.is_pinned ? copy.unpin : copy.pin}
          aria-pressed={project.is_pinned}
          disabled={pinPending}
          onClick={onPin}
        >
          <PinIcon
            aria-hidden
            className={cn(
              "size-[1.125rem]",
              project.is_pinned && "fill-current",
            )}
          />
          {project.is_pinned ? copy.pinned : copy.pinAction}
        </Button>
        {canUpdateProject(project) ? (
          <>
            <span
              aria-hidden
              className="bg-border mx-1 hidden h-7 w-px sm:block"
            />
            <Button
              type="button"
              variant="ghost"
              className="h-11 px-3 text-sm lg:text-base"
              aria-label={copy.edit}
              onClick={onEdit}
            >
              <PencilIcon aria-hidden className="size-[1.125rem]" />
              {copy.editAction}
            </Button>
          </>
        ) : null}
        <Button
          asChild
          size="lg"
          className="ml-auto h-12 px-5 text-base max-sm:mt-2 max-sm:w-full xl:ml-4"
        >
          <Link href={`/projects/${encodeURIComponent(project.slug)}`}>
            {copy.open}
            <ArrowRightIcon aria-hidden className="size-4" />
          </Link>
        </Button>
      </div>
    </article>
  );
}
