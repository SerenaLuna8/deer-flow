"use client";

import {
  CalendarDaysIcon,
  FolderIcon,
  PencilIcon,
  PinIcon,
  UsersIcon,
} from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { Project } from "@/core/projects/types";
import { cn } from "@/lib/utils";

import { canUpdateProject, formatProjectTime } from "./project-view-model";

const ROLE_LABELS: Record<Project["role"], string> = {
  admin: "Admin",
  editor: "Editor",
  runner: "Runner",
  viewer: "Viewer",
};

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
      className="group focus-within:ring-primary/50 h-full overflow-hidden transition focus-within:ring-2 hover:-translate-y-0.5 hover:shadow-lg"
    >
      <CardHeader className="gap-3">
        <div className="flex items-start justify-between gap-3">
          <div className="bg-primary/10 text-primary flex size-11 shrink-0 items-center justify-center rounded-xl">
            {project.icon === "folder" ? (
              <FolderIcon aria-hidden size={22} />
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
                className={cn(project.is_pinned && "fill-current")}
              />
            </Button>
          </div>
        </div>
        <div className="min-w-0">
          <CardTitle className="truncate text-lg">
            {project.display_name}
          </CardTitle>
          <p className="text-muted-foreground mt-1 truncate font-mono text-xs">
            {project.slug}
          </p>
        </div>
        <CardDescription className="line-clamp-2 min-h-10">
          {project.description || "暂无项目描述"}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          <Badge variant="secondary">{ROLE_LABELS[project.role]}</Badge>
          <Badge variant="outline">{project.status}</Badge>
        </div>
        <div className="text-muted-foreground grid grid-cols-2 gap-2 text-xs">
          <span className="flex items-center gap-1.5">
            <UsersIcon size={14} /> {project.member_count} 位成员
          </span>
          <span className="flex items-center gap-1.5">
            <CalendarDaysIcon size={14} />
            {formatProjectTime(project.last_entered_at)}
          </span>
        </div>
        <div className="bg-muted/60 grid grid-cols-3 rounded-lg p-2 text-center text-xs">
          <span>Agent {project.agent_count}</span>
          <span>Skill {project.skill_count}</span>
          <span>MCP {project.mcp_count}</span>
        </div>
      </CardContent>
      <CardFooter>
        <Button asChild className="w-full">
          <Link href={`/projects/${encodeURIComponent(project.slug)}`}>
            进入项目
          </Link>
        </Button>
      </CardFooter>
    </Card>
  );
}
