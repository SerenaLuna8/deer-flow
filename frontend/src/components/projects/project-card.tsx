"use client";

import { ArrowRightIcon, FolderIcon, PencilIcon, PinIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
  return (
    <Card
      data-testid="project-card"
      className="group focus-within:ring-ring/50 hover:border-foreground/20 h-full min-h-[23.5rem] gap-0 overflow-hidden py-0 transition-[border-color,box-shadow,transform] focus-within:ring-[3px] hover:-translate-y-0.5 hover:shadow-md"
    >
      <CardHeader className="gap-0 p-6">
        <div className="flex items-start justify-between gap-3">
          <div className="bg-muted text-primary flex size-16 shrink-0 items-center justify-center rounded-xl">
            {project.icon === "folder" ? (
              <FolderIcon aria-hidden size={28} />
            ) : (
              <span aria-hidden className="text-xl">
                {project.icon}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1">
            {canUpdateProject(project) && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="编辑项目"
                onClick={onEdit}
              >
                <PencilIcon size={16} />
              </Button>
            )}
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={project.is_pinned ? "取消置顶" : "置顶项目"}
              aria-pressed={project.is_pinned}
              disabled={pinPending}
              onClick={onPin}
            >
              <PinIcon
                size={16}
                className={cn(
                  project.is_pinned && "text-selection fill-current",
                )}
              />
            </Button>
          </div>
        </div>
        <div className="mt-10 min-w-0">
          <CardTitle className="truncate text-xl">
            {project.display_name}
          </CardTitle>
          <p className="text-muted-foreground mt-1 truncate font-mono text-xs">
            {project.slug}
          </p>
        </div>
        <CardDescription className="mt-8 line-clamp-2 min-h-10">
          {project.description || "暂无项目描述"}
        </CardDescription>
      </CardHeader>
      <CardFooter className="mt-auto p-6 pt-0">
        <Button asChild size="lg" className="relative h-14 w-full text-base">
          <Link href={`/projects/${encodeURIComponent(project.slug)}`}>
            进入项目
            <ArrowRightIcon aria-hidden className="absolute right-4 size-4" />
          </Link>
        </Button>
      </CardFooter>
    </Card>
  );
}
