"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAdminJobs, useSafeRequeue } from "@/core/admin-operations/api";
import {
  jobFiltersSchema,
  type AdminJobFilters,
  type AdminJobPage,
} from "@/core/admin-operations/types";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";

export type AdminJobsState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: AdminJobPage };

export function AdminJobsStateView({
  state,
  onRetry,
  onRequeue,
  requeueingCoordinate,
  requeueError,
}: {
  state: AdminJobsState;
  onRetry?: () => void;
  onRequeue?: (job: AdminJobPage["items"][number]) => void;
  requeueingCoordinate?: { projectId: string; deadJobId: string };
  requeueError?: boolean;
}) {
  const { t } = useI18n();
  const labels = t.adminOperations.jobs;
  if (state.status === "loading")
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
  if (state.status === "error")
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
  if (state.data.items.length === 0)
    return (
      <section className="rounded-xl border p-8 text-center">
        <h2 className="font-semibold">{labels.emptyTitle}</h2>
        <p className="text-muted-foreground mt-2 text-sm">
          {labels.emptyDescription}
        </p>
      </section>
    );
  return (
    <div className="space-y-3">
      {requeueError ? (
        <p role="alert" className="text-sm text-red-600">
          {labels.requeueError}
        </p>
      ) : null}
      <ol className="space-y-3">
        {state.data.items.map((job) => {
          const isRequeueing =
            job.dead_job_id !== null &&
            requeueingCoordinate?.projectId === job.project_id &&
            requeueingCoordinate.deadJobId === job.dead_job_id;
          return (
            <li key={job.job_id} className="bg-card rounded-xl border p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <code className="text-sm">{job.job_id}</code>
                  <p className="text-muted-foreground mt-1 text-sm">
                    {job.job_type} · {job.status} · {job.retry_safety}
                  </p>
                </div>
                {job.safe_to_requeue && onRequeue ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={isRequeueing}
                    onClick={() => onRequeue(job)}
                  >
                    {isRequeueing ? labels.requeueing : labels.requeue}
                  </Button>
                ) : null}
              </div>
              {job.public_error_code ? (
                <p className="mt-3 text-sm font-medium text-red-700 dark:text-red-300">
                  {job.public_error_code}
                </p>
              ) : null}
              <p className="text-muted-foreground mt-2 text-xs">
                {job.project_id}
              </p>
            </li>
          );
        })}
      </ol>
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
    project_id: input.projectId.trim() || undefined,
    status: input.status || undefined,
    type: input.type || undefined,
  });
  return result.success ? result.data : null;
}

export function AdminJobs() {
  const { user } = useAuth();
  if (!user || user.system_role !== "system_admin") return null;
  return <AuthorizedAdminJobs accountId={user.id} />;
}

function AuthorizedAdminJobs({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const [cursor, setCursor] = useState<string | null>(null);
  const [filters, setFilters] = useState<AdminJobFilters>({});
  const [projectId, setProjectId] = useState("");
  const [status, setStatus] = useState("");
  const [jobType, setJobType] = useState("");
  const [filterError, setFilterError] = useState(false);
  const jobs = useAdminJobs(accountId, cursor, filters);
  const requeue = useSafeRequeue(accountId);
  const state: AdminJobsState = jobs.isLoading
    ? { status: "loading" }
    : jobs.error || !jobs.data
      ? { status: "error" }
      : { status: "ready", data: jobs.data };
  return (
    <main className="mx-auto max-w-7xl space-y-6 px-4 py-8 lg:px-6">
      <div>
        <h1 className="font-serif text-2xl">{t.adminOperations.jobs.title}</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          {t.adminOperations.jobs.description}
        </p>
      </div>
      <form
        className="bg-card grid gap-3 rounded-xl border p-4 sm:grid-cols-2 lg:grid-cols-[minmax(14rem,1fr)_12rem_12rem_auto] lg:items-end"
        onSubmit={(event) => {
          event.preventDefault();
          const parsed = parseAdminJobFilters({
            projectId,
            status,
            type: jobType,
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
          {t.adminOperations.jobs.filters.project}
          <Input
            value={projectId}
            onChange={(event) => setProjectId(event.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
            aria-invalid={filterError || undefined}
          />
        </label>
        <label className="grid gap-1.5 text-sm">
          {t.adminOperations.jobs.filters.status}
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
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
                {value}
              </option>
            ))}
          </select>
        </label>
        <label className="grid gap-1.5 text-sm">
          {t.adminOperations.jobs.filters.type}
          <select
            value={jobType}
            onChange={(event) => setJobType(event.target.value)}
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          >
            <option value="">{t.adminOperations.jobs.filters.allTypes}</option>
            {["private_run", "automation_run", "retention_purge"].map(
              (value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ),
            )}
          </select>
        </label>
        <div className="flex flex-wrap gap-2">
          <Button type="submit">{t.adminOperations.jobs.filters.apply}</Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              setProjectId("");
              setStatus("");
              setJobType("");
              setFilterError(false);
              setCursor(null);
              setFilters({});
            }}
          >
            {t.adminOperations.jobs.filters.clear}
          </Button>
        </div>
        {filterError ? (
          <p
            role="alert"
            className="text-destructive text-sm sm:col-span-2 lg:col-span-4"
          >
            {t.adminOperations.jobs.filters.invalidProject}
          </p>
        ) : null}
      </form>
      <AdminJobsStateView
        state={state}
        onRetry={() => void jobs.refetch()}
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
      {jobs.data?.next_cursor ? (
        <Button
          type="button"
          variant="outline"
          onClick={() => setCursor(jobs.data?.next_cursor ?? null)}
        >
          {t.adminOperations.jobs.older}
        </Button>
      ) : null}
    </main>
  );
}
