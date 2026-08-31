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
  const [createOpen, setCreateOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
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
    <WorkspaceContainer
      data-testid="project-workbench"
      className="[--selection:#2454ff] dark:[--selection:#8b8ae8]"
    >
      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        defaultSection="appearance"
      />
      <header className="bg-background flex h-14 shrink-0 items-center justify-between gap-4 border-b px-4 sm:px-8">
        <div className="flex min-w-0 items-center gap-3">
          <span className="text-foreground font-serif text-xl">Fluva</span>
          <span className="text-muted-foreground hidden text-xs sm:inline">
            {copy.title}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <SystemNotificationCenter userId={userId} />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                className="h-8 gap-2 px-2 text-xs"
                aria-label={copy.account}
              >
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
      <WorkspaceBody className="dark:bg-muted/30 overflow-y-auto bg-[#f3f5f7]">
        <main id="workspace-main" tabIndex={-1} className="w-full outline-none">
          <div
            data-testid="project-toolbar"
            className="bg-background flex flex-wrap items-center gap-x-6 gap-y-3 border-b px-4 py-5 sm:px-8 lg:gap-x-10 lg:py-6"
          >
            <div className="flex shrink-0 items-center gap-3">
              <h1 className="text-lg font-semibold tracking-tight">
                {copy.title}
              </h1>
              <span
                aria-live="polite"
                aria-atomic="true"
                className="text-muted-foreground text-xs"
              >
                {!projectsQuery.isLoading && !projectsQuery.error
                  ? copy.projectCount(projects.length)
                  : null}
              </span>
            </div>
            <span className="border-selection text-selection flex h-8 items-center border-b-2 px-1 text-[13px] font-medium">
              {copy.allProjects}
            </span>
            <div className="flex w-full items-center gap-3 md:ml-auto md:w-auto">
              <div className="relative min-w-0 flex-1 md:w-64">
                <SearchIcon
                  aria-hidden
                  className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-3.5 -translate-y-1/2"
                />
                <Input
                  aria-label={copy.searchProjects}
                  className="h-8 rounded-md pl-9 text-base shadow-none md:text-[13px]"
                  placeholder={copy.searchPlaceholder}
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </div>
              <Button
                type="button"
                className="bg-selection text-selection-foreground hover:bg-selection/90 h-8 shrink-0 gap-1.5 rounded-md px-3 text-[13px] shadow-none"
                onClick={() => setCreateOpen(true)}
              >
                <PlusIcon aria-hidden className="size-3.5" />{" "}
                {copy.createProject}
              </Button>
            </div>
          </div>

          <div className="px-4 py-6 sm:px-8 sm:py-7">
            <section aria-label={copy.projectList}>
              {projectsQuery.isLoading ? (
                <div
                  data-testid="project-loading"
                  aria-label={copy.loadingProjects}
                  className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,18rem),1fr))] gap-3.5"
                >
                  {[0, 1, 2].map((item) => (
                    <div
                      key={item}
                      className="bg-card h-[164px] rounded-xl border p-4"
                    >
                      <div className="flex items-center gap-3">
                        <Skeleton className="size-10 rounded-lg" />
                        <div className="flex-1 space-y-2">
                          <Skeleton className="h-3 w-24" />
                          <Skeleton className="h-2.5 w-32" />
                        </div>
                      </div>
                      <Skeleton className="mt-4 h-3 w-3/4" />
                    </div>
                  ))}
                </div>
              ) : projectsQuery.error ? (
                <div
                  data-testid="project-load-error"
                  role="alert"
                  className="border-destructive/30 bg-destructive/5 rounded-xl border p-5 text-sm"
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
                  onClearSearch={() => setSearch("")}
                />
              ) : (
                <div
                  data-testid="project-list"
                  className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,18rem),1fr))] gap-3.5"
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

            <div className="mt-6">
              <WorkspaceRecoverySection userId={userId} />
            </div>
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
