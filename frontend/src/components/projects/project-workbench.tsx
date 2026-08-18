"use client";

import {
  ChevronDownIcon,
  LogOutIcon,
  PlusIcon,
  SearchIcon,
  SettingsIcon,
  ShieldCheckIcon,
  ShieldUserIcon,
  UserRoundIcon,
} from "lucide-react";
import Link from "next/link";
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
import { SettingsDialog } from "@/components/workspace/settings";
import { SystemNotificationCenter } from "@/components/workspace/system-notification-center";
import {
  WorkspaceBody,
  WorkspaceContainer,
} from "@/components/workspace/workspace-container";
import type { User } from "@/core/auth/types";
import { useI18n } from "@/core/i18n/hooks";
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
  type ProjectListFilter,
  projectErrorMessage,
} from "./project-view-model";
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
  accountUsername,
  systemRole,
  onLogout,
}: {
  userId: string;
  accountUsername: string;
  systemRole: User["system_role"];
  onLogout: () => Promise<void>;
}) {
  const { t } = useI18n();
  const copy = t.projectWorkspace;
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<ProjectListFilter>("all");
  const [createOpen, setCreateOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [editingProject, setEditingProject] = useState<Project | null>(null);
  const projectsQuery = useProjects(userId, {
    ...(search.trim() ? { query: search.trim() } : {}),
    ...(filter === "pinned" ? { pinned: true } : {}),
    limit: 100,
  });
  const create = useCreateProject(userId);
  const update = useUpdateProject(userId, editingProject?.id);
  const projects = filterAndSortProjects(
    projectsQuery.data?.items ?? [],
    search,
    filter,
  );

  useEffect(() => {
    if (create.isSuccess) setCreateOpen(false);
  }, [create.isSuccess]);

  useEffect(() => {
    if (update.isSuccess) setEditingProject(null);
  }, [update.isSuccess]);

  return (
    <WorkspaceContainer data-testid="project-workbench">
      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        defaultSection="appearance"
      />
      <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b px-4 sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <span className="text-primary font-serif text-lg">ActWeave</span>
          <span className="text-muted-foreground hidden text-sm sm:inline">
            {copy.title}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <SystemNotificationCenter userId={userId} />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button type="button" variant="ghost" aria-label={copy.account}>
                <UserRoundIcon className="size-4" />
                <span className="hidden max-w-56 truncate sm:inline">
                  {accountUsername}
                </span>
                <ChevronDownIcon
                  aria-hidden
                  className="hidden size-4 sm:block"
                />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-64">
              <DropdownMenuLabel className="truncate">
                {accountUsername}
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              {systemRole === "system_admin" ? (
                <>
                  <DropdownMenuItem asChild>
                    <Link href="/admin/operations">
                      <ShieldCheckIcon aria-hidden className="size-4" />
                      {copy.platformAdministration}
                    </Link>
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                </>
              ) : null}
              <DropdownMenuItem onSelect={() => setSettingsOpen(true)}>
                <SettingsIcon aria-hidden className="size-4" />
                {copy.systemSettings}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem asChild>
                <Link href="/workspace/privacy">
                  <ShieldUserIcon aria-hidden className="size-4" />
                  {copy.privacyCenter}
                </Link>
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => void onLogout()}>
                <LogOutIcon className="size-4" />
                {copy.logout}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </header>
      <WorkspaceBody className="overflow-y-auto">
        <main className="mx-auto w-full max-w-[1440px] px-4 py-8 sm:px-6 sm:py-14 lg:px-12">
          <div
            data-testid="project-toolbar"
            className="border-border/80 bg-card mb-8 grid gap-3 rounded-xl border p-5 shadow-xs lg:grid-cols-[minmax(24rem,1fr)_18.5rem_minmax(17rem,auto)] lg:items-center"
          >
            <div className="relative min-w-0 flex-1">
              <SearchIcon className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2" />
              <Input
                aria-label={copy.searchProjects}
                className="h-12 pl-9 text-base shadow-none"
                placeholder={copy.searchPlaceholder}
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
            <div
              role="group"
              aria-label={copy.filterProjects}
              className="bg-muted grid grid-cols-2 rounded-lg p-1"
            >
              <Button
                type="button"
                variant={filter === "all" ? "outline" : "ghost"}
                className="aria-pressed:border-border aria-pressed:bg-background h-10 min-w-36 border-transparent text-base shadow-none aria-pressed:shadow-xs"
                aria-pressed={filter === "all"}
                onClick={() => setFilter("all")}
              >
                {copy.allProjects}
              </Button>
              <Button
                type="button"
                variant={filter === "pinned" ? "outline" : "ghost"}
                className="aria-pressed:border-border aria-pressed:bg-background h-10 min-w-36 border-transparent text-base shadow-none aria-pressed:shadow-xs"
                aria-pressed={filter === "pinned"}
                onClick={() => setFilter("pinned")}
              >
                {copy.pinnedOnly}
              </Button>
            </div>
            <div className="flex items-center justify-between gap-4 lg:justify-end">
              <div
                aria-live="polite"
                aria-atomic="true"
                className="text-muted-foreground flex min-h-9 min-w-20 items-center text-base"
              >
                {!projectsQuery.isLoading && !projectsQuery.error
                  ? copy.projectCount(projects.length)
                  : null}
              </div>
              <Button
                type="button"
                size="lg"
                className="h-12 shrink-0 px-6"
                onClick={() => setCreateOpen(true)}
              >
                <PlusIcon size={16} /> {copy.createProject}
              </Button>
            </div>
          </div>

          <section aria-label={copy.projectList}>
            {projectsQuery.isLoading ? (
              <div
                data-testid="project-loading"
                aria-label={copy.loadingProjects}
                className="grid gap-6 md:grid-cols-2 xl:grid-cols-3 xl:gap-8"
              >
                {[0, 1, 2].map((item) => (
                  <Skeleton key={item} className="h-[23.5rem] rounded-xl" />
                ))}
              </div>
            ) : projectsQuery.error ? (
              <div
                data-testid="project-load-error"
                role="alert"
                className="border-destructive/30 bg-destructive/5 rounded-xl border p-6"
              >
                <h2 className="font-semibold">{copy.projectLoadFailed}</h2>
                <p className="text-muted-foreground mt-2 text-sm">
                  {projectErrorMessage(projectsQuery.error, copy.errors)}
                </p>
                <Button
                  type="button"
                  className="mt-4"
                  variant="outline"
                  onClick={() => void projectsQuery.refetch()}
                >
                  {copy.retry}
                </Button>
              </div>
            ) : projects.length === 0 ? (
              <ProjectEmptyState
                search={search}
                filtered={filter === "pinned"}
                onClearSearch={() => setSearch("")}
                onClearFilter={() => setFilter("all")}
              />
            ) : (
              <div
                data-testid="project-grid"
                className="grid gap-6 md:grid-cols-2 xl:grid-cols-3 xl:gap-8"
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
          </section>

          <div className="mt-12">
            <WorkspaceRecoverySection userId={userId} />
          </div>
        </main>
      </WorkspaceBody>

      <CreateProjectDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        pending={create.isPending}
        errorMessage={
          create.error ? projectErrorMessage(create.error, copy.errors) : null
        }
        onSubmit={(input: CreateProjectInput) => create.mutate(input)}
      />
      <EditProjectDialog
        project={editingProject}
        open={editingProject !== null}
        onOpenChange={(open) => !open && setEditingProject(null)}
        pending={update.isPending}
        errorMessage={
          update.error ? projectErrorMessage(update.error, copy.errors) : null
        }
        onSubmit={(input) => update.mutate(input)}
      />
    </WorkspaceContainer>
  );
}
