"use client";

import { ArrowRightIcon, PencilIcon, PinIcon } from "lucide-react";
import Image from "next/image";
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
  const { locale, t } = useI18n();
  const copy = t.projectWorkspace.card;
  const createdAt = new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(project.created_at));
  return (
    <article
      data-testid="project-card"
      className="group border-border/60 bg-card text-card-foreground hover:border-border relative h-[164px] min-w-0 rounded-xl border p-4 shadow-[0_1px_3px_rgba(15,23,42,0.04)] transition-[border-color,box-shadow] hover:shadow-[0_3px_10px_rgba(15,23,42,0.07)]"
    >
      <Link
        href={`/projects/${encodeURIComponent(project.slug)}`}
        aria-label={`${copy.open}: ${project.display_name}`}
        className="focus-visible:ring-ring/50 absolute inset-0 z-10 rounded-xl outline-none focus-visible:ring-2"
      >
        <span
          aria-hidden
          className="text-muted-foreground group-hover:text-primary absolute right-3 bottom-3 flex size-7 items-center justify-center transition-colors"
        >
          <ArrowRightIcon className="size-4" />
        </span>
      </Link>

      <div className="flex min-w-0 items-center gap-3 pr-14">
        {project.icon === "folder" ? (
          <Image
            src="/images/workspace/project-folder.webp"
            width={40}
            height={40}
            alt=""
            aria-hidden
            className="size-10 shrink-0 rounded-[10px] object-contain"
          />
        ) : (
          <div className="bg-muted flex size-10 shrink-0 items-center justify-center rounded-[10px]">
            <span aria-hidden className="text-xl leading-none">
              {project.icon}
            </span>
          </div>
        )}
        <div className="min-w-0">
          <h3 className="truncate text-sm leading-5 font-semibold">
            {project.display_name}
          </h3>
          <p className="text-muted-foreground mt-1 truncate text-[11px] leading-4">
            {project.slug}
          </p>
        </div>
      </div>

      <p className="text-muted-foreground mt-4 line-clamp-2 text-[13px] leading-5">
        {project.description || copy.noDescription}
      </p>

      <p className="text-muted-foreground absolute right-12 bottom-3 left-4 flex h-7 items-center gap-1 text-[11px] leading-4">
        <span className="shrink-0">{copy.createdAt}</span>
        <time dateTime={project.created_at} className="truncate">
          {createdAt}
        </time>
      </p>

      <div className="absolute top-3 right-3 z-20 flex items-center gap-0.5">
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className={cn(
            "text-muted-foreground size-7 disabled:pointer-events-auto",
            project.is_pinned && "text-selection hover:text-selection",
          )}
          aria-label={project.is_pinned ? copy.unpin : copy.pin}
          aria-pressed={project.is_pinned}
          title={project.is_pinned ? copy.unpin : copy.pin}
          disabled={pinPending}
          onClick={onPin}
        >
          <PinIcon
            aria-hidden
            className={cn("size-3.5", project.is_pinned && "fill-current")}
          />
        </Button>
        {canUpdateProject(project) ? (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground size-7"
            aria-label={copy.edit}
            title={copy.edit}
            onClick={onEdit}
          >
            <PencilIcon aria-hidden className="size-3.5" />
          </Button>
        ) : null}
      </div>
    </article>
  );
}
