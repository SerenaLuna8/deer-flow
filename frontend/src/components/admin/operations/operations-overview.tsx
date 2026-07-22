"use client";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useOperationsOverview } from "@/core/admin-operations/api";
import type { OperationsOverviewData } from "@/core/admin-operations/types";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";

export type OperationsOverviewState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: OperationsOverviewData };

export function OperationsOverviewStateView({
  state,
  onRetry,
}: {
  state: OperationsOverviewState;
  onRetry?: () => void;
}) {
  const { t } = useI18n();
  const labels = t.adminOperations.overview;
  if (state.status === "loading") {
    return (
      <section
        aria-busy="true"
        aria-label={labels.loading}
        className="space-y-4"
      >
        <p>{labels.loading}</p>
        <Skeleton className="h-32 w-full rounded-xl" />
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

  const readiness = state.data.readiness;
  const readinessComponents = [
    ["database", readiness.database],
    ["schema", readiness.schema_state],
    ["worker_fleet", readiness.worker_fleet],
    ["scheduler", readiness.scheduler],
    ["stream", readiness.stream],
    ["quota", readiness.quota],
    ["audit", readiness.audit],
  ] as const;
  const readinessState = (value: string) => {
    const states = labels.readiness.states;
    return value in states
      ? states[value as keyof typeof states]
      : states.unknown;
  };
  const readinessView = (
    <section className="bg-card rounded-xl border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-medium">{labels.readiness.title}</h2>
        <span className="text-sm font-medium">
          {readinessState(readiness.status)}
        </span>
      </div>
      <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
        {readinessComponents.map(([component, value]) => (
          <div key={component}>
            <dt className="text-muted-foreground">
              {labels.readiness.components[component]}
            </dt>
            <dd>{readinessState(value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
  if (state.data.data_status === "unavailable") {
    return (
      <div className="space-y-6">
        {readinessView}
        <section role="alert" className="rounded-xl border p-6">
          <h2 className="font-semibold">{labels.unavailableTitle}</h2>
          <p className="text-muted-foreground mt-2 text-sm">
            {labels.unavailableDescription}
          </p>
        </section>
      </div>
    );
  }

  const counts = [
    [labels.counts.projects, state.data.counts.projects],
    [labels.counts.suspendedProjects, state.data.counts.suspended_projects],
    [labels.counts.queuedJobs, state.data.counts.queued_jobs],
    [labels.counts.runningJobs, state.data.counts.running_jobs],
    [labels.counts.deadJobs, state.data.counts.dead_jobs],
  ] as const;
  return (
    <div className="space-y-6">
      {readinessView}
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {counts.map(([label, value]) => (
          <div key={label} className="bg-card rounded-xl border p-4">
            <dt className="text-muted-foreground text-sm">{label}</dt>
            <dd className="mt-2 text-2xl font-semibold tabular-nums">
              {value}
            </dd>
          </div>
        ))}
      </dl>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {state.data.usage.map((item) => (
          <section
            key={item.dimension}
            className="bg-card rounded-xl border p-4"
          >
            <h2 className="font-medium">{labels.usage[item.dimension]}</h2>
            <dl className="mt-3 grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-muted-foreground">{labels.usage.used}</dt>
                <dd className="tabular-nums">{item.used}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">
                  {labels.usage.reserved}
                </dt>
                <dd className="tabular-nums">{item.reserved}</dd>
              </div>
            </dl>
          </section>
        ))}
      </div>
    </div>
  );
}

export function OperationsOverview() {
  const { user } = useAuth();
  if (!user || user.system_role !== "system_admin") return null;
  return <AuthorizedOperationsOverview accountId={user.id} />;
}

function AuthorizedOperationsOverview({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const overview = useOperationsOverview(accountId);
  const state: OperationsOverviewState = overview.isLoading
    ? { status: "loading" }
    : overview.error || !overview.data
      ? { status: "error" }
      : { status: "ready", data: overview.data };
  return (
    <main className="mx-auto max-w-7xl space-y-6 px-4 py-8 lg:px-6">
      <div>
        <h1 className="font-serif text-2xl">
          {t.adminOperations.overview.title}
        </h1>
        <p className="text-muted-foreground mt-1 text-sm">
          {t.adminOperations.overview.description}
        </p>
      </div>
      <OperationsOverviewStateView
        state={state}
        onRetry={() => void overview.refetch()}
      />
    </main>
  );
}
