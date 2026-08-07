"use client";

import { RotateCcwIcon } from "lucide-react";
import { useState, type ReactNode } from "react";

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
import { useAdminJobs, useSafeRequeue } from "@/core/admin-operations/api";
import {
  ADMIN_JOB_PAGE_SIZES,
  ADMIN_JOB_TYPES,
  DEFAULT_ADMIN_JOB_PAGE_SIZE,
  adminJobPageSizeSchema,
  jobFiltersSchema,
  type AdminJobFilters,
  type AdminJobPage,
  type AdminJobPageSize,
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
  AdminProjectFilterSelect,
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
        className="min-w-[80rem] table-fixed"
        containerClassName="hidden xl:block"
      >
        <thead className="bg-muted/45 text-muted-foreground">
          <tr className="border-border/70 border-b">
            <th className="w-[14%] px-3 py-2.5 text-xs font-medium">
              {labels.filters.type}
            </th>
            <th className="w-[18%] px-3 py-2.5 text-xs font-medium">
              {localLabels.jobId}
            </th>
            <th className="w-[10%] px-3 py-2.5 text-xs font-medium">
              {labels.filters.status}
            </th>
            <th className="w-[12%] px-3 py-2.5 text-xs font-medium">
              {labels.retrySafetyLabel}
            </th>
            <th className="w-[18%] px-3 py-2.5 text-xs font-medium">
              {labels.errorLabel}
            </th>
            <th className="w-[16%] px-3 py-2.5 text-xs font-medium">
              {labels.filters.project}
            </th>
            <th className="w-[12%] px-3 py-2.5 text-xs font-medium">
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
                </td>
                <td className="px-3 py-2.5">
                  <AdminStatus status={job.status}>
                    {labels.statuses[job.status]}
                  </AdminStatus>
                </td>
                <td className="px-3 py-2.5">
                  <AdminStatus status={job.retry_safety}>
                    {labels.retrySafety[job.retry_safety]}
                  </AdminStatus>
                </td>
                <td className="px-3 py-2.5">
                  {job.public_error_code ? (
                    <p
                      className="text-destructive truncate font-mono text-xs font-semibold"
                      title={job.public_error_code}
                    >
                      {job.public_error_code}
                    </p>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
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
                  <AdminStatus status={job.status}>
                    {labels.statuses[job.status]}
                  </AdminStatus>
                </header>
                <dl className="grid gap-2 text-xs">
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.retrySafetyLabel}
                    </dt>
                    <dd className="mt-1">
                      <AdminStatus status={job.retry_safety}>
                        {labels.retrySafety[job.retry_safety]}
                      </AdminStatus>
                    </dd>
                  </div>
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
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.errorLabel}
                    </dt>
                    <dd className="mt-0.5 font-mono font-semibold">
                      {job.public_error_code ? (
                        <span className="text-destructive">
                          {job.public_error_code}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </dd>
                  </div>
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
  projectId: string;
  status: string;
  type: string;
}): AdminJobFilters | null {
  const result = jobFiltersSchema.safeParse({
    project_id: input.projectId || undefined,
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
  const [pager, setPager] = useState(INITIAL_ADMIN_CURSOR_STATE);
  const [filters, setFilters] = useState<AdminJobFilters>({});
  const [pageSize, setPageSize] = useState<AdminJobPageSize>(
    DEFAULT_ADMIN_JOB_PAGE_SIZE,
  );
  const [projectId, setProjectId] = useState("");
  const [status, setStatus] = useState("");
  const [jobType, setJobType] = useState("");
  const jobs = useAdminJobs(accountId, pager.cursor, filters, pageSize);
  const requeue = useSafeRequeue(accountId);
  const state: AdminJobsState = jobs.isLoading
    ? { status: "loading" }
    : jobs.error || !jobs.data
      ? { status: "error" }
      : { status: "ready", data: jobs.data };
  const resetFilters = () => {
    setProjectId("");
    setStatus("");
    setJobType("");
    setPager(INITIAL_ADMIN_CURSOR_STATE);
    setPageSize(DEFAULT_ADMIN_JOB_PAGE_SIZE);
    setFilters({});
  };
  const applyPageSize = (next: number) => {
    const parsed = adminJobPageSizeSchema.safeParse(next);
    if (!parsed.success || parsed.data === pageSize) return;
    setPager(INITIAL_ADMIN_CURSOR_STATE);
    setPageSize(parsed.data);
  };
  const hasFilters = Object.keys(filters).length > 0;
  const canResetFilters =
    hasFilters ||
    pageSize !== DEFAULT_ADMIN_JOB_PAGE_SIZE ||
    projectId.length > 0 ||
    status.length > 0 ||
    jobType.length > 0;
  return (
    <AdminPage>
      <AdminPageHeader title={t.adminOperations.jobs.title} />
      <AdminSection contentClassName="p-3">
        <form
          aria-label={t.adminOperations.jobs.filters.label}
          className="grid gap-2 sm:grid-cols-2 lg:flex lg:flex-wrap lg:items-center"
          onSubmit={(event) => {
            event.preventDefault();
            const parsed = parseAdminJobFilters({
              projectId,
              status,
              type: jobType,
            });
            if (!parsed) return;
            setPager(INITIAL_ADMIN_CURSOR_STATE);
            setFilters(parsed);
          }}
        >
          <AdminProjectFilterSelect
            accountId={accountId}
            className="sm:col-span-2 lg:w-72"
            value={projectId}
            onChange={setProjectId}
            label={t.adminOperations.jobs.filters.project}
            allLabel={t.adminOperations.jobs.filters.allProjects}
          />
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
              {ADMIN_JOB_TYPES.map((value) => (
                <option key={value} value={value}>
                  {t.adminOperations.jobs.types[value]}
                </option>
              ))}
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
      {state.status === "ready" ? (
        <AdminCursorPagination
          alwaysVisible
          state={pager}
          nextCursor={jobs.data?.next_cursor ?? null}
          busy={jobs.isFetching}
          itemCount={jobs.data?.items.length ?? 0}
          itemCountLabel={localLabels.itemsOnPage}
          pageSize={pageSize}
          pageSizeOptions={ADMIN_JOB_PAGE_SIZES}
          pageSizeLabel={localLabels.pageSize}
          pageSizeOptionLabel={localLabels.pageSizeOption}
          previousLabel={localLabels.previousPage}
          nextLabel={localLabels.nextPage}
          pageLabel={localLabels.page}
          onPageSizeChange={applyPageSize}
          onPrevious={() => setPager((current) => retreatAdminCursor(current))}
          onNext={() =>
            setPager((current) =>
              advanceAdminCursor(current, jobs.data?.next_cursor ?? null),
            )
          }
        />
      ) : null}
    </AdminPage>
  );
}
