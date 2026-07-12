"use client";

import {
  FolderKanbanIcon,
  LogOutIcon,
  PlusIcon,
  SearchIcon,
  UserRoundIcon,
} from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  WorkspaceBody,
  WorkspaceContainer,
} from "@/components/workspace/workspace-container";
import {
  useCreateProject,
  usePinProject,
  useProjects,
  useUpdateProject,
} from "@/core/projects/hooks";
import type { CreateProjectInput, Project } from "@/core/projects/types";

import { CreateProjectDialog } from "./create-project-dialog";
import { EditProjectDialog } from "./edit-project-dialog";
import { ProjectCard } from "./project-card";
import { ProjectEmptyState } from "./project-empty-state";
import {
  filterAndSortProjects,
  projectErrorMessage,
} from "./project-view-model";
import { WorkspaceInvitations } from "./workspace-invitations";
import { WorkspaceRecoverySection } from "./workspace-recovery-section";

function ProjectCardWithActions({
  project,
  userId,
  onEdit,
}: {
  project: Project;
  userId: string;
  onEdit: () => void;
}) {
  const pin = usePinProject(userId, project.id);
  return (
    <ProjectCard
      project={project}
      onEdit={onEdit}
      pinPending={pin.isPending}
      onPin={() => pin.mutate(!project.is_pinned)}
    />
  );
}

export function ProjectWorkbench({
  userId,
  accountEmail,
  onLogout,
}: {
  userId: string;
  accountEmail: string;
  onLogout: () => Promise<void>;
}) {
  const [search, setSearch] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [editingProject, setEditingProject] = useState<Project | null>(null);
  const projectsQuery = useProjects(userId, {
    ...(search.trim() ? { query: search.trim() } : {}),
    limit: 100,
  });
  const create = useCreateProject(userId);
  const update = useUpdateProject(userId, editingProject?.id);
  const projects = filterAndSortProjects(
    projectsQuery.data?.items ?? [],
    search,
  );

  useEffect(() => {
    if (create.isSuccess) setCreateOpen(false);
  }, [create.isSuccess]);

  useEffect(() => {
    if (update.isSuccess) setEditingProject(null);
  }, [update.isSuccess]);

  return (
    <WorkspaceContainer data-testid="project-workbench">
      <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b px-4 sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <span className="text-primary font-serif text-lg">DeerFlow</span>
          <span className="text-muted-foreground hidden text-sm sm:inline">
            工作空间
          </span>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="ghost" aria-label="账户">
              <UserRoundIcon className="size-4" />
              <span className="hidden max-w-56 truncate sm:inline">
                {accountEmail}
              </span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-64">
            <DropdownMenuLabel className="truncate">
              {accountEmail}
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => void onLogout()}>
              <LogOutIcon className="size-4" />
              退出登录
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>
      <WorkspaceBody className="overflow-y-auto">
        <main className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
          <div className="mb-8 flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <div className="text-primary mb-2 flex items-center gap-2 text-sm font-medium">
                <FolderKanbanIcon size={18} /> 多项目工作空间
              </div>
              <h1 className="text-3xl font-semibold tracking-tight">
                工作空间
              </h1>
              <p className="text-muted-foreground mt-2">
                创建、搜索和整理你参与的项目。
              </p>
            </div>
            <Button type="button" onClick={() => setCreateOpen(true)}>
              <PlusIcon size={16} /> 创建项目
            </Button>
          </div>

          <div className="relative mb-6 max-w-xl">
            <SearchIcon className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2" />
            <Input
              aria-label="搜索项目"
              className="pl-9"
              placeholder="搜索名称或项目标识"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>

          <WorkspaceInvitations userId={userId} />
          <WorkspaceRecoverySection userId={userId} />

          {projectsQuery.isLoading ? (
            <div
              data-testid="project-loading"
              className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3"
            >
              {[0, 1, 2].map((item) => (
                <Skeleton key={item} className="h-80 rounded-xl" />
              ))}
            </div>
          ) : projectsQuery.error ? (
            <div
              data-testid="project-load-error"
              role="alert"
              className="border-destructive/30 bg-destructive/5 rounded-xl border p-6"
            >
              <h2 className="font-semibold">项目加载失败</h2>
              <p className="text-muted-foreground mt-2 text-sm">
                {projectErrorMessage(projectsQuery.error)}
              </p>
              <Button
                type="button"
                className="mt-4"
                variant="outline"
                onClick={() => void projectsQuery.refetch()}
              >
                重试
              </Button>
            </div>
          ) : projects.length === 0 ? (
            <ProjectEmptyState
              search={search}
              onCreate={() => setCreateOpen(true)}
              onClearSearch={() => setSearch("")}
            />
          ) : (
            <div
              data-testid="project-grid"
              className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3"
            >
              {projects.map((project) => (
                <ProjectCardWithActions
                  key={project.id}
                  project={project}
                  userId={userId}
                  onEdit={() => setEditingProject(project)}
                />
              ))}
            </div>
          )}
        </main>
      </WorkspaceBody>

      <CreateProjectDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        pending={create.isPending}
        errorMessage={create.error ? projectErrorMessage(create.error) : null}
        onSubmit={(input: CreateProjectInput) => create.mutate(input)}
      />
      <EditProjectDialog
        project={editingProject}
        open={editingProject !== null}
        onOpenChange={(open) => !open && setEditingProject(null)}
        pending={update.isPending}
        errorMessage={update.error ? projectErrorMessage(update.error) : null}
        onSubmit={(input) => update.mutate(input)}
      />
    </WorkspaceContainer>
  );
}
