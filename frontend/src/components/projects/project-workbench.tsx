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
      <header className="flex h-20 shrink-0 items-center justify-between gap-4 border-b px-5 sm:px-8">
        <div className="flex min-w-0 items-center gap-3">
          <span className="text-primary font-serif text-2xl">ActWeave</span>
          <span className="text-muted-foreground hidden text-base sm:inline">
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
        <main className="mx-auto w-full max-w-[1440px] px-5 py-10 sm:px-8 sm:py-12 lg:px-12 lg:py-14">
          <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
                {copy.title}
              </h1>
              <p className="text-muted-foreground mt-3 text-base sm:text-lg">
                {copy.subtitle}
              </p>
            </div>
            <Button
              type="button"
              size="lg"
              className="h-14 shrink-0 px-7 text-base max-sm:w-full"
              onClick={() => setCreateOpen(true)}
            >
              <PlusIcon aria-hidden className="size-5" /> {copy.createProject}
            </Button>
          </div>

          <div
            data-testid="project-toolbar"
            className="mt-12 mb-12 grid gap-4 md:grid-cols-[minmax(0,1fr)_20rem] md:items-center lg:grid-cols-[minmax(22rem,1fr)_24rem_auto]"
          >
            <div className="relative min-w-0 flex-1">
              <SearchIcon className="text-muted-foreground absolute top-1/2 left-4 size-5 -translate-y-1/2" />
              <Input
                aria-label={copy.searchProjects}
                className="h-14 rounded-lg pl-12 text-base shadow-none"
                placeholder={copy.searchPlaceholder}
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
            <div
              role="group"
              aria-label={copy.filterProjects}
              className="bg-muted grid h-14 grid-cols-2 rounded-lg p-1"
            >
              <Button
                type="button"
                variant="ghost"
                className="aria-pressed:border-selection/15 aria-pressed:bg-selection-subtle aria-pressed:text-selection h-12 min-w-0 border border-transparent text-base shadow-none"
                aria-pressed={filter === "all"}
                onClick={() => setFilter("all")}
              >
                {copy.allProjects}
              </Button>
              <Button
                type="button"
                variant="ghost"
                className="aria-pressed:border-selection/15 aria-pressed:bg-selection-subtle aria-pressed:text-selection h-12 min-w-0 border border-transparent text-base shadow-none"
                aria-pressed={filter === "pinned"}
                onClick={() => setFilter("pinned")}
              >
                {copy.pinnedOnly}
              </Button>
            </div>
            <div
              aria-live="polite"
              aria-atomic="true"
              className="text-muted-foreground flex min-h-9 min-w-20 items-center text-base md:col-span-2 md:justify-self-end lg:col-span-1"
            >
              {!projectsQuery.isLoading && !projectsQuery.error
                ? copy.projectCount(projects.length)
                : null}
            </div>
          </div>

          <section aria-label={copy.projectList}>
            {projectsQuery.isLoading ? (
              <div
                data-testid="project-loading"
                aria-label={copy.loadingProjects}
                className="border-border/80 divide-border/80 overflow-hidden rounded-xl border"
              >
                {[0, 1, 2].map((item) => (
                  <div key={item} className="border-t p-6 first:border-t-0">
                    <Skeleton className="h-20 rounded-xl" />
                  </div>
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
                data-testid="project-list"
                className="border-border/80 divide-border/80 overflow-hidden rounded-xl border"
              >
                <div
                  data-testid="project-list-header"
                  aria-hidden
                  className="text-foreground/80 hidden grid-cols-[minmax(18rem,1.2fr)_minmax(14rem,1fr)_minmax(23rem,auto)] items-center gap-5 border-b px-8 py-5 text-sm font-semibold xl:grid"
                >
                  <span>{copy.columns.project}</span>
                  <span>{copy.columns.description}</span>
                  <span>{copy.columns.actions}</span>
                </div>
                <div className="divide-border/80 divide-y">
                  {projects.map((project) => (
                    <ProjectCardWithActions
                      key={project.id}
                      project={project}
                      userId={userId}
                      onEdit={() => setEditingProject(project)}
                    />
                  ))}
                </div>
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
