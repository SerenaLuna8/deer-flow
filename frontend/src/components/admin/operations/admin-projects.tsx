"use client";

import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useAdminProjectLifecycle,
  useAdminProjects,
} from "@/core/admin-operations/api";
import {
  projectFiltersSchema,
  type AdminProjectFilters,
  type AdminProjectPage,
} from "@/core/admin-operations/types";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";

export type AdminProjectsState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: AdminProjectPage };

export function AdminProjectsStateView({
  state,
  onRetry,
  onRequestLifecycle,
  pendingProjectId,
  mutationError,
}: {
  state: AdminProjectsState;
  onRetry?: () => void;
  onRequestLifecycle?: (
    project: AdminProjectPage["items"][number],
    action: "suspend" | "resume",
  ) => void;
  pendingProjectId?: string;
  mutationError?: boolean;
}) {
  const { t, locale } = useI18n();
  const labels = t.adminOperations.projects;
  if (state.status === "loading") {
    return (
      <section
        aria-busy="true"
        aria-label={labels.loading}
        className="space-y-4"
      >
        <p>{labels.loading}</p>
        <Skeleton className="h-40 w-full rounded-xl" />
      </section>
    );
  }
  if (state.status === "error") {
    return (
      <section role="alert" className="rounded-xl border p-6">
        <h2 className="font-semibold">{labels.unavailableTitle}</h2>
        <p className="text-muted-foreground mt-2 text-sm">
          {labels.unavailableDescription}
        </p>
        {onRetry ? (
          <Button
            className="mt-4"
            type="button"
            variant="outline"
            onClick={onRetry}
          >
            {t.adminOperations.retry}
          </Button>
        ) : null}
      </section>
    );
  }
  if (state.data.items.length === 0) {
    return (
      <section className="rounded-xl border p-8 text-center">
        <h2 className="font-semibold">{labels.emptyTitle}</h2>
        <p className="text-muted-foreground mt-2 text-sm">
          {labels.emptyDescription}
        </p>
      </section>
    );
  }
  return (
    <div className="space-y-3">
      {mutationError ? (
        <p role="alert" className="text-destructive text-sm">
          {labels.actions.error}
        </p>
      ) : null}
      <ol className="space-y-3">
        {state.data.items.map((project) => {
          const pending = pendingProjectId === project.project_id;
          return (
            <li
              key={project.project_id}
              className="bg-card rounded-xl border p-5"
            >
              <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <h2 className="truncate font-semibold">
                    {project.display_name}
                  </h2>
                  <p className="text-muted-foreground mt-1 font-mono text-xs">
                    {project.slug}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground text-sm">
                    {project.status === "active"
                      ? labels.active
                      : labels.pendingDeletion}
                    {project.is_suspended ? ` · ${labels.suspended}` : ""}
                  </span>
                  {project.status === "active" && !project.is_suspended ? (
                    <Button asChild type="button" size="sm">
                      <Link
                        href={`/admin/projects/${project.project_id}/assets/agents`}
                      >
                        治理共享资产
                      </Link>
                    </Button>
                  ) : null}
                  {onRequestLifecycle && project.status === "active" ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={pending}
                      onClick={() =>
                        onRequestLifecycle(
                          project,
                          project.is_suspended ? "resume" : "suspend",
                        )
                      }
                    >
                      {pending
                        ? labels.actions.pending
                        : project.is_suspended
                          ? labels.actions.resume
                          : labels.actions.suspend}
                    </Button>
                  ) : null}
                </div>
              </div>
              <details className="mt-4 rounded-lg border px-3 py-2 text-sm">
                <summary className="cursor-pointer font-medium">
                  {labels.details}
                </summary>
                <dl className="mt-3 grid gap-3 sm:grid-cols-2">
                  <div>
                    <dt className="text-muted-foreground text-xs">
                      {labels.fields.projectId}
                    </dt>
                    <dd className="font-mono text-xs break-all">
                      {project.project_id}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground text-xs">
                      {labels.fields.slug}
                    </dt>
                    <dd>{project.slug}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground text-xs">
                      {labels.fields.createdAt}
                    </dt>
                    <dd>
                      {new Date(project.created_at).toLocaleString(locale)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground text-xs">
                      {labels.fields.updatedAt}
                    </dt>
                    <dd>
                      {new Date(project.updated_at).toLocaleString(locale)}
                    </dd>
                  </div>
                  {project.deletion_effective_at ? (
                    <div>
                      <dt className="text-muted-foreground text-xs">
                        {labels.fields.deletionAt}
                      </dt>
                      <dd>
                        {new Date(project.deletion_effective_at).toLocaleString(
                          locale,
                        )}
                      </dd>
                    </div>
                  ) : null}
                </dl>
              </details>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export function parseAdminProjectFilters(input: {
  query: string;
  status: string;
  suspension: string;
}): AdminProjectFilters | null {
  if (!["", "suspended", "running"].includes(input.suspension)) return null;
  const result = projectFiltersSchema.safeParse({
    query: input.query.trim() || undefined,
    status: input.status || undefined,
    suspended:
      input.suspension === "" ? undefined : input.suspension === "suspended",
  });
  return result.success ? result.data : null;
}

export function AdminProjects() {
  const { user } = useAuth();
  if (!user || user.system_role !== "system_admin") return null;
  return <AuthorizedAdminProjects accountId={user.id} />;
}

function AuthorizedAdminProjects({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const [cursor, setCursor] = useState<string | null>(null);
  const [filters, setFilters] = useState<AdminProjectFilters>({});
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [suspension, setSuspension] = useState("");
  const [filterError, setFilterError] = useState(false);
  const [lifecycleTarget, setLifecycleTarget] = useState<{
    project: AdminProjectPage["items"][number];
    action: "suspend" | "resume";
  } | null>(null);
  const projects = useAdminProjects(accountId, cursor, filters);
  const lifecycle = useAdminProjectLifecycle(accountId);
  const state: AdminProjectsState = projects.isLoading
    ? { status: "loading" }
    : projects.error || !projects.data
      ? { status: "error" }
      : { status: "ready", data: projects.data };
  return (
    <main className="mx-auto max-w-7xl space-y-6 px-4 py-8 lg:px-6">
      <div>
        <h1 className="font-serif text-2xl">
          {t.adminOperations.projects.title}
        </h1>
        <p className="text-muted-foreground mt-1 text-sm">
          {t.adminOperations.projects.description}
        </p>
      </div>
      <form
        className="bg-card grid gap-3 rounded-xl border p-4 sm:grid-cols-2 lg:grid-cols-[minmax(16rem,1fr)_12rem_12rem_auto] lg:items-end"
        onSubmit={(event) => {
          event.preventDefault();
          const parsed = parseAdminProjectFilters({
            query,
            status,
            suspension,
          });
          if (!parsed) {
            setFilterError(true);
            return;
          }
          setFilterError(false);
          setCursor(null);
          setFilters(parsed);
        }}
      >
        <label className="grid gap-1.5 text-sm">
          {t.adminOperations.projects.filters.query}
          <Input
            value={query}
            maxLength={121}
            placeholder={t.adminOperations.projects.filters.queryPlaceholder}
            aria-invalid={filterError || undefined}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <label className="grid gap-1.5 text-sm">
          {t.adminOperations.projects.filters.status}
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          >
            <option value="">{t.adminOperations.projects.filters.all}</option>
            <option value="active">{t.adminOperations.projects.active}</option>
            <option value="pending_deletion">
              {t.adminOperations.projects.pendingDeletion}
            </option>
          </select>
        </label>
        <label className="grid gap-1.5 text-sm">
          {t.adminOperations.projects.filters.suspension}
          <select
            value={suspension}
            onChange={(event) => setSuspension(event.target.value)}
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          >
            <option value="">{t.adminOperations.projects.filters.all}</option>
            <option value="running">{t.adminOperations.projects.active}</option>
            <option value="suspended">
              {t.adminOperations.projects.suspended}
            </option>
          </select>
        </label>
        <div className="flex flex-wrap gap-2">
          <Button type="submit">
            {t.adminOperations.projects.filters.apply}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              setQuery("");
              setStatus("");
              setSuspension("");
              setFilterError(false);
              setCursor(null);
              setFilters({});
            }}
          >
            {t.adminOperations.projects.filters.clear}
          </Button>
        </div>
        {filterError ? (
          <p
            role="alert"
            className="text-destructive text-sm sm:col-span-2 lg:col-span-4"
          >
            {t.adminOperations.projects.filters.invalid}
          </p>
        ) : null}
      </form>
      <AdminProjectsStateView
        state={state}
        onRetry={() => void projects.refetch()}
        pendingProjectId={
          lifecycle.isPending ? lifecycle.variables.projectId : undefined
        }
        mutationError={Boolean(lifecycle.error)}
        onRequestLifecycle={(project, action) => {
          lifecycle.reset();
          setLifecycleTarget({ project, action });
        }}
      />
      {projects.data?.next_cursor ? (
        <Button
          type="button"
          variant="outline"
          onClick={() => setCursor(projects.data?.next_cursor ?? null)}
        >
          {t.adminOperations.projects.older}
        </Button>
      ) : null}
      <Dialog
        open={lifecycleTarget !== null}
        onOpenChange={(open) => {
          if (!open && !lifecycle.isPending) setLifecycleTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {lifecycleTarget?.action === "suspend"
                ? t.adminOperations.projects.actions.confirmSuspendTitle
                : t.adminOperations.projects.actions.confirmResumeTitle}
            </DialogTitle>
            <DialogDescription>
              {lifecycleTarget?.project.display_name
                ? `${lifecycleTarget.project.display_name}：`
                : ""}
              {lifecycleTarget?.action === "suspend"
                ? t.adminOperations.projects.actions.confirmSuspendDescription
                : t.adminOperations.projects.actions.confirmResumeDescription}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={lifecycle.isPending}
              onClick={() => setLifecycleTarget(null)}
            >
              {t.adminOperations.projects.actions.cancel}
            </Button>
            <Button
              type="button"
              disabled={!lifecycleTarget || lifecycle.isPending}
              onClick={() => {
                if (!lifecycleTarget) return;
                lifecycle.mutate(
                  {
                    projectId: lifecycleTarget.project.project_id,
                    action: lifecycleTarget.action,
                  },
                  { onSuccess: () => setLifecycleTarget(null) },
                );
              }}
            >
              {lifecycle.isPending
                ? t.adminOperations.projects.actions.pending
                : t.adminOperations.projects.actions.confirm}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}
