"use client";

import { FolderKanbanIcon, PlusIcon, SearchIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  WorkspaceBody,
  WorkspaceContainer,
  WorkspaceHeader,
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

export function ProjectWorkbench({ userId }: { userId: string }) {
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
      <WorkspaceHeader>
        <span className="font-medium">项目工作台</span>
      </WorkspaceHeader>
      <WorkspaceBody className="overflow-y-auto">
        <main className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
          <div className="mb-8 flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <div className="text-primary mb-2 flex items-center gap-2 text-sm font-medium">
                <FolderKanbanIcon size={18} /> Project-first workspace
              </div>
              <h1 className="text-3xl font-semibold tracking-tight">
                项目工作台
              </h1>
              <p className="text-muted-foreground mt-2">
                管理你的项目、成员身份与共享资产入口。
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
            <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
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
        onSubmit={async (input: CreateProjectInput) => {
          await create.mutateAsync(input);
        }}
      />
      <EditProjectDialog
        project={editingProject}
        open={editingProject !== null}
        onOpenChange={(open) => !open && setEditingProject(null)}
        pending={update.isPending}
        errorMessage={update.error ? projectErrorMessage(update.error) : null}
        onSubmit={async (input) => {
          await update.mutateAsync(input);
        }}
      />
    </WorkspaceContainer>
  );
}
