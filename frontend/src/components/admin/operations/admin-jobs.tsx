"use client";

import { RotateCcwIcon, SearchIcon } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";

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
import { Input } from "@/components/ui/input";
import { useAdminJobs, useSafeRequeue } from "@/core/admin-operations/api";
import {
  jobFiltersSchema,
  type AdminJobFilters,
  type AdminJobPage,
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
  AdminCopyButton,
  AdminTechnicalValue,
} from "./admin-operations-ui";

export type AdminJobsState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: AdminJobPage };

export function AdminJobsStateView({
  emptyAction,
  state,
  onRetry,
  onRequeue,
  requeueingCoordinate,
  requeueError,
}: {
  emptyAction?: ReactNode;
  state: AdminJobsState;
  onRetry?: () => void;
  onRequeue?: (job: AdminJobPage["items"][number]) => void;
  requeueingCoordinate?: { projectId: string; deadJobId: string };
  requeueError?: boolean;
}) {
  const { t } = useI18n();
  const labels = t.adminOperations.jobs;
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
      {requeueError ? (
        <AdminInlineAlert>{labels.requeueError}</AdminInlineAlert>
      ) : null}
      <AdminDataTable
        aria-label={labels.title}
        className="min-w-[68rem] table-fixed"
        containerClassName="hidden xl:block"
      >
        <thead className="bg-muted/45 text-muted-foreground">
          <tr className="border-border/70 border-b">
            <th className="w-[18%] px-3 py-2.5 text-xs font-medium">
              {labels.filters.type}
            </th>
            <th className="w-[25%] px-3 py-2.5 text-xs font-medium">
              {localLabels.jobId}
            </th>
            <th className="w-[20%] px-3 py-2.5 text-xs font-medium">
              {labels.filters.status}
            </th>
            <th className="w-[23%] px-3 py-2.5 text-xs font-medium">
              {labels.filters.project}
            </th>
            <th className="w-[14%] px-3 py-2.5 text-xs font-medium">
              <span className="sr-only">{labels.requeue}</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-border/70 divide-y">
          {state.data.items.map((job) => {
            const isRequeueing =
              job.dead_job_id !== null &&
              requeueingCoordinate?.projectId === job.project_id &&
              requeueingCoordinate.deadJobId === job.dead_job_id;
            return (
              <tr
                key={job.job_id}
                className="hover:bg-muted/25 align-middle transition-colors"
              >
                <td className="px-3 py-2.5">
                  <h2 className="text-sm font-medium">
                    {labels.types[job.job_type]}
                  </h2>
                </td>
                <td className="px-3 py-2.5">
                  <AdminTechnicalValue
                    compact
                    value={job.job_id}
                    copyLabel={localLabels.copy}
                    copiedLabel={localLabels.copied}
                  />
                  {job.public_error_code ? (
                    <p className="text-destructive mt-1.5 truncate font-mono text-xs font-semibold">
                      <span className="sr-only">
                        {localLabels.publicErrorCode}:{" "}
                      </span>
                      {job.public_error_code}
                    </p>
                  ) : null}
                </td>
                <td className="px-3 py-2.5">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <AdminStatus status={job.status}>
                      {labels.statuses[job.status]}
                    </AdminStatus>
                    <AdminStatus status={job.retry_safety}>
                      {labels.retrySafety[job.retry_safety]}
                    </AdminStatus>
                  </div>
                </td>
                <td className="px-3 py-2.5">
                  <div className="flex min-w-0 items-center gap-2">
                    <div className="min-w-0">
                      <p
                        className="truncate text-sm font-medium"
                        title={job.project_display_name}
                      >
                        {job.project_display_name}
                      </p>
                      <p
                        className="text-muted-foreground mt-0.5 truncate font-mono text-xs"
                        title={job.project_slug}
                      >
                        {job.project_slug}
                      </p>
                    </div>
                    <AdminCopyButton
                      value={job.project_id}
                      copyLabel={labels.copyProjectId}
                      copiedLabel={labels.projectIdCopied}
                    />
                  </div>
                </td>
                <td className="px-3 py-2.5 text-right">
                  {job.safe_to_requeue && onRequeue ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={isRequeueing}
                      onClick={() => onRequeue(job)}
                    >
                      <RotateCcwIcon
                        aria-hidden
                        className={
                          isRequeueing
                            ? "size-3.5 animate-spin motion-reduce:animate-none"
                            : "size-3.5"
                        }
                      />
                      {isRequeueing ? labels.requeueing : labels.requeue}
                    </Button>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </AdminDataTable>
      <AdminMobileRecordList aria-label={labels.title}>
        {state.data.items.map((job) => {
          const isRequeueing =
            job.dead_job_id !== null &&
            requeueingCoordinate?.projectId === job.project_id &&
            requeueingCoordinate.deadJobId === job.dead_job_id;
          return (
            <li key={job.job_id}>
              <article className="space-y-3 p-4">
                <header className="flex items-start justify-between gap-3">
                  <h2 className="min-w-0 text-sm font-semibold">
                    {labels.types[job.job_type]}
                  </h2>
                  <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
                    <AdminStatus status={job.status}>
                      {labels.statuses[job.status]}
                    </AdminStatus>
                    <AdminStatus status={job.retry_safety}>
                      {labels.retrySafety[job.retry_safety]}
                    </AdminStatus>
                  </div>
                </header>
                <dl className="grid gap-2 text-xs">
                  <div>
                    <dt className="text-muted-foreground">
                      {localLabels.jobId}
                    </dt>
                    <dd className="mt-1">
                      <AdminTechnicalValue
                        compact
                        value={job.job_id}
                        copyLabel={localLabels.copy}
                        copiedLabel={localLabels.copied}
                      />
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.filters.project}
                    </dt>
                    <dd className="mt-1 flex min-w-0 items-center gap-2">
                      <div className="min-w-0">
                        <p
                          className="truncate text-sm font-medium"
                          title={job.project_display_name}
                        >
                          {job.project_display_name}
                        </p>
                        <p
                          className="text-muted-foreground mt-0.5 truncate font-mono text-xs"
                          title={job.project_slug}
                        >
                          {job.project_slug}
                        </p>
                      </div>
                      <AdminCopyButton
                        value={job.project_id}
                        copyLabel={labels.copyProjectId}
                        copiedLabel={labels.projectIdCopied}
                      />
                    </dd>
                  </div>
                  {job.public_error_code ? (
                    <div>
                      <dt className="text-muted-foreground">
                        {localLabels.publicErrorCode}
                      </dt>
                      <dd className="text-destructive mt-0.5 font-mono font-semibold">
                        {job.public_error_code}
                      </dd>
                    </div>
                  ) : null}
                </dl>
                {job.safe_to_requeue && onRequeue ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={isRequeueing}
                    onClick={() => onRequeue(job)}
                  >
                    <RotateCcwIcon
                      aria-hidden
                      className={
                        isRequeueing
                          ? "size-3.5 animate-spin motion-reduce:animate-none"
                          : "size-3.5"
                      }
                    />
                    {isRequeueing ? labels.requeueing : labels.requeue}
                  </Button>
                ) : null}
              </article>
            </li>
          );
        })}
      </AdminMobileRecordList>
    </div>
  );
}

function idempotencyKey(): string {
  return `${crypto.randomUUID().replaceAll("-", "")}${crypto.randomUUID().replaceAll("-", "")}`;
}

export function parseAdminJobFilters(input: {
  projectQuery: string;
  status: string;
  type: string;
}): AdminJobFilters | null {
  const result = jobFiltersSchema.safeParse({
    project_query: input.projectQuery.trim() || undefined,
    status: input.status || undefined,
    type: input.type || undefined,
  });
  return result.success ? result.data : null;
}

export function AdminJobs() {
  const { user } = useAuth();
  if (user?.system_role !== "system_admin") return null;
  return <AuthorizedAdminJobs accountId={user.id} />;
}

function AuthorizedAdminJobs({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const localLabels = t.adminOperations.ui;
  const projectQueryInputRef = useRef<HTMLInputElement>(null);
  const [pager, setPager] = useState(INITIAL_ADMIN_CURSOR_STATE);
  const [filters, setFilters] = useState<AdminJobFilters>({});
  const [projectQuery, setProjectQuery] = useState("");
  const [status, setStatus] = useState("");
  const [jobType, setJobType] = useState("");
  const [filterError, setFilterError] = useState(false);
  const jobs = useAdminJobs(accountId, pager.cursor, filters);
  const requeue = useSafeRequeue(accountId);
  const state: AdminJobsState = jobs.isLoading
    ? { status: "loading" }
    : jobs.error || !jobs.data
      ? { status: "error" }
      : { status: "ready", data: jobs.data };
  const resetFilters = () => {
    setProjectQuery("");
    setStatus("");
    setJobType("");
    setFilterError(false);
    setPager(INITIAL_ADMIN_CURSOR_STATE);
    setFilters({});
  };
  const hasFilters = Object.keys(filters).length > 0;
  const canResetFilters =
    hasFilters ||
    projectQuery.trim().length > 0 ||
    status.length > 0 ||
    jobType.length > 0;
  return (
    <AdminPage>
      <AdminPageHeader
        title={t.adminOperations.jobs.title}
        description={t.adminOperations.jobs.description}
      />
      <AdminSection contentClassName="p-3">
        <form
          aria-label={t.adminOperations.jobs.filters.label}
          className="grid gap-2 sm:grid-cols-2 lg:flex lg:flex-wrap lg:items-center"
          onSubmit={(event) => {
            event.preventDefault();
            const parsed = parseAdminJobFilters({
              projectQuery,
              status,
              type: jobType,
            });
            if (!parsed) {
              setFilterError(true);
              projectQueryInputRef.current?.focus();
              return;
            }
            setFilterError(false);
            setPager(INITIAL_ADMIN_CURSOR_STATE);
            setFilters(parsed);
          }}
        >
          <label className="relative min-w-0 sm:col-span-2 lg:w-80">
            <span className="sr-only">
              {t.adminOperations.jobs.filters.projectQuery}
            </span>
            <SearchIcon
              aria-hidden
              className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
            />
            <Input
              ref={projectQueryInputRef}
              className="pl-9"
              value={projectQuery}
              onChange={(event) => setProjectQuery(event.target.value)}
              placeholder={
                t.adminOperations.jobs.filters.projectQueryPlaceholder
              }
              maxLength={120}
              autoComplete="off"
              spellCheck={false}
              aria-invalid={filterError || undefined}
              aria-describedby={
                filterError ? "admin-job-filter-error" : undefined
              }
              aria-errormessage={
                filterError ? "admin-job-filter-error" : undefined
              }
            />
          </label>
          <label className="min-w-0 lg:w-40">
            <span className="sr-only">
              {t.adminOperations.jobs.filters.status}
            </span>
            <select
              aria-label={t.adminOperations.jobs.filters.status}
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-9 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-[3px]"
            >
              <option value="">
                {t.adminOperations.jobs.filters.allStatuses}
              </option>
              {[
                "queued",
                "leased",
                "running",
                "retry_wait",
                "succeeded",
                "failed",
                "cancelled",
                "dead",
              ].map((value) => (
                <option key={value} value={value}>
                  {
                    t.adminOperations.jobs.statuses[
                      value as keyof typeof t.adminOperations.jobs.statuses
                    ]
                  }
                </option>
              ))}
            </select>
          </label>
          <label className="min-w-0 lg:w-44">
            <span className="sr-only">
              {t.adminOperations.jobs.filters.type}
            </span>
            <select
              aria-label={t.adminOperations.jobs.filters.type}
              value={jobType}
              onChange={(event) => setJobType(event.target.value)}
              className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-9 w-full rounded-md border px-3 text-sm outline-none focus-visible:ring-[3px]"
            >
              <option value="">
                {t.adminOperations.jobs.filters.allTypes}
              </option>
              {["private_run", "automation_run", "retention_purge"].map(
                (value) => (
                  <option key={value} value={value}>
                    {
                      t.adminOperations.jobs.types[
                        value as keyof typeof t.adminOperations.jobs.types
                      ]
                    }
                  </option>
                ),
              )}
            </select>
          </label>
          <div className="flex flex-wrap gap-1 sm:col-span-2 lg:ml-auto">
            <Button type="submit" size="sm">
              {t.adminOperations.jobs.filters.apply}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={!canResetFilters}
              onClick={resetFilters}
            >
              {t.adminOperations.jobs.filters.clear}
            </Button>
          </div>
          {filterError ? (
            <AdminInlineAlert
              id="admin-job-filter-error"
              className="sm:col-span-2 lg:basis-full"
            >
              {t.adminOperations.jobs.filters.invalidQuery}
            </AdminInlineAlert>
          ) : null}
        </form>
      </AdminSection>
      <AdminJobsStateView
        state={state}
        onRetry={() => void jobs.refetch()}
        emptyAction={
          hasFilters ? (
            <Button type="button" variant="outline" onClick={resetFilters}>
              {localLabels.clearFilters}
            </Button>
          ) : undefined
        }
        requeueError={Boolean(requeue.error)}
        requeueingCoordinate={
          requeue.isPending
            ? {
                projectId: requeue.variables.project_id,
                deadJobId: requeue.variables.dead_job_id,
              }
            : undefined
        }
        onRequeue={(job) => {
          if (!job.dead_job_id) return;
          requeue.mutate({
            project_id: job.project_id,
            dead_job_id: job.dead_job_id,
            idempotency_key: idempotencyKey(),
            max_attempts: 3,
          });
        }}
      />
      <AdminCursorPagination
        state={pager}
        nextCursor={jobs.data?.next_cursor ?? null}
        busy={jobs.isFetching}
        previousLabel={localLabels.previousPage}
        nextLabel={t.adminOperations.jobs.older}
        pageLabel={localLabels.page}
        onPrevious={() => setPager((current) => retreatAdminCursor(current))}
        onNext={() =>
          setPager((current) =>
            advanceAdminCursor(current, jobs.data?.next_cursor ?? null),
          )
        }
      />
    </AdminPage>
  );
}
