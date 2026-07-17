"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAdminJobs, useSafeRequeue } from "@/core/admin-operations/api";
import type { AdminJobPage } from "@/core/admin-operations/types";
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
  requeueingJobId,
  requeueError,
}: {
  state: AdminJobsState;
  onRetry?: () => void;
  onRequeue?: (job: AdminJobPage["items"][number]) => void;
  requeueingJobId?: string;
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
        {state.data.items.map((job) => (
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
                  disabled={requeueingJobId === job.job_id}
                  onClick={() => onRequeue(job)}
                >
                  {requeueingJobId === job.job_id
                    ? labels.requeueing
                    : labels.requeue}
                </Button>
              ) : null}
            </div>
            {job.public_error_code ? (
              <p className="mt-3 text-sm font-medium text-red-700 dark:text-red-300">
                {job.public_error_code}
              </p>
            ) : null}
            <p className="text-muted-foreground mt-2 text-xs">
              {job.project_id} · attempt {job.attempt_count}
            </p>
          </li>
        ))}
      </ol>
    </div>
  );
}

function idempotencyKey(): string {
  return `${crypto.randomUUID().replaceAll("-", "")}${crypto.randomUUID().replaceAll("-", "")}`;
}

export function AdminJobs() {
  const { user } = useAuth();
  if (!user || user.system_role !== "system_admin") return null;
  return <AuthorizedAdminJobs accountId={user.id} />;
}

function AuthorizedAdminJobs({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const [cursor, setCursor] = useState<string | null>(null);
  const jobs = useAdminJobs(accountId, cursor);
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
      <AdminJobsStateView
        state={state}
        onRetry={() => void jobs.refetch()}
        requeueError={Boolean(requeue.error)}
        requeueingJobId={
          requeue.isPending ? requeue.variables.dead_job_id : undefined
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
