"use client";

import {
  ArrowDownIcon,
  ExternalLinkIcon,
  SearchIcon,
  XIcon,
} from "lucide-react";
import Link from "next/link";
import { Fragment, useRef, useState, type ReactNode } from "react";

import {
  AdminCursorPagination,
  AdminPage,
  AdminPageHeader,
  AdminSection,
  INITIAL_ADMIN_CURSOR_STATE,
  advanceAdminCursor,
  retreatAdminCursor,
} from "@/components/admin/ui/admin-page";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
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

import {
  AdminDataTable,
  AdminEmptyState,
  AdminErrorState,
  AdminInlineAlert,
  AdminLoadingState,
  AdminMobileRecordList,
  AdminStatus,
  AdminTechnicalValue,
} from "./admin-operations-ui";

export type AdminProjectsState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: AdminProjectPage };

export function AdminProjectsStateView({
  emptyAction,
  state,
  onRetry,
  onRequestLifecycle,
  pendingProjectId,
  mutationError,
}: {
  emptyAction?: ReactNode;
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
  const localLabels = t.adminOperations.ui;
  if (state.status === "loading") {
    return <AdminLoadingState label={labels.loading} />;
  }
  if (state.status === "error") {
    return (
      <AdminErrorState
        title={labels.unavailableTitle}
        description={labels.unavailableDescription}
        retryLabel={t.adminOperations.retry}
        onRetry={onRetry}
      />
    );
  }
  if (state.data.items.length === 0) {
    return (
      <AdminEmptyState
        title={labels.emptyTitle}
        description={labels.emptyDescription}
        action={emptyAction}
      />
    );
  }
  return (
    <div className="space-y-3">
      {mutationError ? (
        <AdminInlineAlert>{labels.actions.error}</AdminInlineAlert>
      ) : null}
      <AdminDataTable
        aria-label={labels.title}
        className="min-w-[68rem] table-fixed"
        containerClassName="hidden xl:block"
      >
        <thead className="bg-muted/45 text-muted-foreground">
          <tr className="border-border/70 border-b">
            <th className="w-[20%] px-3 py-2.5 text-xs font-medium">
              {labels.title}
            </th>
            <th className="w-[24%] px-3 py-2.5 text-xs font-medium">
              {labels.fields.projectId}
            </th>
            <th className="w-[15%] px-3 py-2.5 text-xs font-medium">
              {labels.filters.status}
            </th>
            <th className="w-[17%] px-3 py-2.5 text-xs font-medium">
              {labels.fields.updatedAt}
            </th>
            <th className="w-[24%] px-3 py-2.5 text-xs font-medium">
              <span className="sr-only">{labels.actions.governAssets}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {state.data.items.map((project) => {
            const pending = pendingProjectId === project.project_id;
            return (
              <Fragment key={project.project_id}>
                <tr className="hover:bg-muted/25 align-middle transition-colors">
                  <td className="px-3 py-2.5">
                    <h2 className="truncate text-sm font-semibold">
                      {project.display_name}
                    </h2>
                    <p className="text-muted-foreground mt-0.5 truncate font-mono text-xs">
                      {project.slug}
                    </p>
                  </td>
                  <td className="px-3 py-2.5">
                    <AdminTechnicalValue
                      compact
                      value={project.project_id}
                      copyLabel={localLabels.copy}
                      copiedLabel={localLabels.copied}
                    />
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <AdminStatus status={project.status}>
                        {project.status === "active"
                          ? labels.active
                          : labels.pendingDeletion}
                      </AdminStatus>
                      {project.is_suspended ? (
                        <AdminStatus status="suspended">
                          {labels.suspended}
                        </AdminStatus>
                      ) : null}
                    </div>
                  </td>
                  <td className="px-3 py-2.5">
                    <time
                      className="text-muted-foreground text-xs whitespace-nowrap"
                      dateTime={project.updated_at}
                    >
                      {new Date(project.updated_at).toLocaleString(locale)}
                    </time>
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex flex-wrap items-center justify-end gap-1.5">
                      {project.status === "active" && !project.is_suspended ? (
                        <Button asChild size="sm" variant="outline">
                          <Link
                            href={`/admin/projects/${project.project_id}/assets/agents`}
                          >
                            {labels.actions.governAssets}
                            <ExternalLinkIcon
                              aria-hidden
                              className="size-3.5"
                            />
                          </Link>
                        </Button>
                      ) : null}
                      {onRequestLifecycle && project.status === "active" ? (
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
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
                  </td>
                </tr>
                <tr className="border-border/70 bg-muted/10 border-b">
                  <td colSpan={5} className="px-3 py-0">
                    <details className="group text-sm">
                      <summary className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex w-fit cursor-pointer list-none items-center gap-1.5 py-2 text-xs font-medium focus-visible:ring-2 focus-visible:outline-none">
                        <ArrowDownIcon
                          aria-hidden
                          className="size-3.5 transition-transform group-open:rotate-180"
                        />
                        {labels.details}
                      </summary>
                      <dl className="border-border/70 bg-background mb-3 grid gap-x-6 gap-y-3 rounded-md border px-3 py-3 sm:grid-cols-2 xl:grid-cols-4">
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            {labels.fields.projectId}
                          </dt>
                          <dd className="mt-1">
                            <AdminTechnicalValue
                              value={project.project_id}
                              copyLabel={localLabels.copy}
                              copiedLabel={localLabels.copied}
                            />
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            {labels.fields.slug}
                          </dt>
                          <dd className="mt-1">
                            <AdminTechnicalValue
                              value={project.slug}
                              copyLabel={localLabels.copy}
                              copiedLabel={localLabels.copied}
                            />
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            {labels.fields.createdAt}
                          </dt>
                          <dd className="mt-1">
                            {new Date(project.created_at).toLocaleString(
                              locale,
                            )}
                          </dd>
                        </div>
                        {project.deletion_effective_at ? (
                          <div>
                            <dt className="text-muted-foreground text-xs">
                              {labels.fields.deletionAt}
                            </dt>
                            <dd className="mt-1">
                              {new Date(
                                project.deletion_effective_at,
                              ).toLocaleString(locale)}
                            </dd>
                          </div>
                        ) : null}
                      </dl>
                    </details>
                  </td>
                </tr>
              </Fragment>
            );
          })}
        </tbody>
      </AdminDataTable>
      <AdminMobileRecordList aria-label={labels.title}>
        {state.data.items.map((project) => {
          const pending = pendingProjectId === project.project_id;
          return (
            <li key={project.project_id}>
              <article className="space-y-3 p-4">
                <header className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h2 className="truncate text-sm font-semibold">
                      {project.display_name}
                    </h2>
                    <p className="text-muted-foreground mt-0.5 truncate font-mono text-xs">
                      {project.slug}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
                    <AdminStatus status={project.status}>
                      {project.status === "active"
                        ? labels.active
                        : labels.pendingDeletion}
                    </AdminStatus>
                    {project.is_suspended ? (
                      <AdminStatus status="suspended">
                        {labels.suspended}
                      </AdminStatus>
                    ) : null}
                  </div>
                </header>
                <dl className="grid gap-2 text-xs">
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.fields.projectId}
                    </dt>
                    <dd className="mt-1">
                      <AdminTechnicalValue
                        compact
                        value={project.project_id}
                        copyLabel={localLabels.copy}
                        copiedLabel={localLabels.copied}
                      />
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.fields.updatedAt}
                    </dt>
                    <dd className="mt-0.5">
                      <time dateTime={project.updated_at}>
                        {new Date(project.updated_at).toLocaleString(locale)}
                      </time>
                    </dd>
                  </div>
                </dl>
                <div className="flex flex-wrap gap-1.5">
                  {project.status === "active" && !project.is_suspended ? (
                    <Button asChild size="sm" variant="outline">
                      <Link
                        href={`/admin/projects/${project.project_id}/assets/agents`}
                      >
                        {labels.actions.governAssets}
                        <ExternalLinkIcon aria-hidden className="size-3.5" />
                      </Link>
                    </Button>
                  ) : null}
                  {onRequestLifecycle && project.status === "active" ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
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
                <details className="group border-border/70 border-t pt-2 text-sm">
                  <summary className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex w-fit cursor-pointer list-none items-center gap-1.5 text-xs font-medium focus-visible:ring-2 focus-visible:outline-none">
                    <ArrowDownIcon
                      aria-hidden
                      className="size-3.5 transition-transform group-open:rotate-180"
                    />
                    {labels.details}
                  </summary>
                  <dl className="bg-muted/35 mt-2 grid gap-2 rounded-md p-3 text-xs">
                    <div>
                      <dt className="text-muted-foreground">
                        {labels.fields.createdAt}
                      </dt>
                      <dd className="mt-0.5">
                        {new Date(project.created_at).toLocaleString(locale)}
                      </dd>
                    </div>
                    {project.deletion_effective_at ? (
                      <div>
                        <dt className="text-muted-foreground">
                          {labels.fields.deletionAt}
                        </dt>
                        <dd className="mt-0.5">
                          {new Date(
                            project.deletion_effective_at,
                          ).toLocaleString(locale)}
                        </dd>
                      </div>
                    ) : null}
                  </dl>
                </details>
              </article>
            </li>
          );
        })}
      </AdminMobileRecordList>
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
  if (user?.system_role !== "system_admin") return null;
  return <AuthorizedAdminProjects accountId={user.id} />;
}

function AuthorizedAdminProjects({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const localLabels = t.adminOperations.ui;
  const queryInputRef = useRef<HTMLInputElement>(null);
  const [pager, setPager] = useState(INITIAL_ADMIN_CURSOR_STATE);
  const [filters, setFilters] = useState<AdminProjectFilters>({});
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [suspension, setSuspension] = useState("");
  const [filterError, setFilterError] = useState(false);
  const [lifecycleTarget, setLifecycleTarget] = useState<{
    project: AdminProjectPage["items"][number];
    action: "suspend" | "resume";
  } | null>(null);
  const projects = useAdminProjects(accountId, pager.cursor, filters);
  const lifecycle = useAdminProjectLifecycle(accountId);
  const state: AdminProjectsState = projects.isLoading
    ? { status: "loading" }
    : projects.error || !projects.data
      ? { status: "error" }
      : { status: "ready", data: projects.data };
  const resetFilters = () => {
    setQuery("");
    setStatus("");
    setSuspension("");
    setFilterError(false);
    setPager(INITIAL_ADMIN_CURSOR_STATE);
    setFilters({});
  };
  const hasFilters = Object.keys(filters).length > 0;
  return (
    <AdminPage>
      <AdminPageHeader title={t.adminOperations.projects.title} />
      <AdminSection contentClassName="p-3">
        <form
          aria-label={t.adminOperations.projects.filters.apply}
          className="grid gap-2 sm:grid-cols-2 xl:grid-cols-[minmax(16rem,1fr)_11rem_11rem_auto] xl:items-center"
          onSubmit={(event) => {
            event.preventDefault();
            const parsed = parseAdminProjectFilters({
              query,
              status,
              suspension,
            });
            if (!parsed) {
              setFilterError(true);
              queryInputRef.current?.focus();
              return;
            }
            setFilterError(false);
            setPager(INITIAL_ADMIN_CURSOR_STATE);
            setFilters(parsed);
          }}
        >
          <label className="relative block">
            <span className="sr-only">
              {t.adminOperations.projects.filters.query}
            </span>
            <SearchIcon
              aria-hidden
              className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
            />
            <Input
              ref={queryInputRef}
              className="pl-9"
              value={query}
              maxLength={121}
              placeholder={t.adminOperations.projects.filters.queryPlaceholder}
              aria-invalid={filterError || undefined}
              aria-describedby={
                filterError ? "admin-project-filter-error" : undefined
              }
              aria-errormessage={
                filterError ? "admin-project-filter-error" : undefined
              }
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <label className="block">
            <span className="sr-only">
              {t.adminOperations.projects.filters.status}
            </span>
            <select
              aria-label={t.adminOperations.projects.filters.status}
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-9 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-[3px]"
            >
              <option value="">{t.adminOperations.projects.filters.all}</option>
              <option value="active">
                {t.adminOperations.projects.active}
              </option>
              <option value="pending_deletion">
                {t.adminOperations.projects.pendingDeletion}
              </option>
            </select>
          </label>
          <label className="block">
            <span className="sr-only">
              {t.adminOperations.projects.filters.suspension}
            </span>
            <select
              aria-label={t.adminOperations.projects.filters.suspension}
              value={suspension}
              onChange={(event) => setSuspension(event.target.value)}
              className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-9 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-[3px]"
            >
              <option value="">{t.adminOperations.projects.filters.all}</option>
              <option value="running">
                {t.adminOperations.projects.filters.notSuspended}
              </option>
              <option value="suspended">
                {t.adminOperations.projects.suspended}
              </option>
            </select>
          </label>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" size="sm">
              {t.adminOperations.projects.filters.apply}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={resetFilters}
            >
              {t.adminOperations.projects.filters.clear}
            </Button>
          </div>
          {filterError ? (
            <AdminInlineAlert
              id="admin-project-filter-error"
              className="sm:col-span-2 xl:col-span-4"
            >
              {t.adminOperations.projects.filters.invalid}
            </AdminInlineAlert>
          ) : null}
        </form>
      </AdminSection>
      <AdminProjectsStateView
        state={state}
        onRetry={() => void projects.refetch()}
        emptyAction={
          hasFilters ? (
            <Button type="button" variant="outline" onClick={resetFilters}>
              {localLabels.clearFilters}
            </Button>
          ) : undefined
        }
        pendingProjectId={
          lifecycle.isPending ? lifecycle.variables.projectId : undefined
        }
        onRequestLifecycle={(project, action) => {
          lifecycle.reset();
          setLifecycleTarget({ project, action });
        }}
      />
      <AdminCursorPagination
        state={pager}
        nextCursor={projects.data?.next_cursor ?? null}
        busy={projects.isFetching}
        previousLabel={localLabels.previousPage}
        nextLabel={t.adminOperations.projects.older}
        pageLabel={localLabels.page}
        onPrevious={() => setPager((current) => retreatAdminCursor(current))}
        onNext={() =>
          setPager((current) =>
            advanceAdminCursor(current, projects.data?.next_cursor ?? null),
          )
        }
      />
      <Dialog
        open={lifecycleTarget !== null}
        onOpenChange={(open) => {
          if (!open && !lifecycle.isPending) setLifecycleTarget(null);
        }}
      >
        <DialogContent showCloseButton={false}>
          <DialogClose asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="absolute top-3 right-3"
              disabled={lifecycle.isPending}
              aria-label={localLabels.close}
              title={localLabels.close}
            >
              <XIcon aria-hidden className="size-4" />
            </Button>
          </DialogClose>
          <DialogHeader>
            <DialogTitle>
              {lifecycleTarget?.action === "suspend"
                ? t.adminOperations.projects.actions.confirmSuspendTitle
                : t.adminOperations.projects.actions.confirmResumeTitle}
            </DialogTitle>
            <DialogDescription>
              {lifecycleTarget?.project.display_name ? (
                <span className="text-foreground mb-1 block font-medium">
                  {lifecycleTarget.project.display_name}
                </span>
              ) : null}
              <span className="block">
                {lifecycleTarget?.action === "suspend"
                  ? t.adminOperations.projects.actions.confirmSuspendDescription
                  : t.adminOperations.projects.actions.confirmResumeDescription}
              </span>
            </DialogDescription>
          </DialogHeader>
          {lifecycle.error ? (
            <AdminInlineAlert>
              {t.adminOperations.projects.actions.error}
            </AdminInlineAlert>
          ) : null}
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
              variant={
                lifecycleTarget?.action === "suspend"
                  ? "destructive"
                  : "default"
              }
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
    </AdminPage>
  );
}
