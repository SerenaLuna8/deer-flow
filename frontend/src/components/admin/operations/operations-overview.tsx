"use client";

import {
  AdminMetric,
  AdminMetricGrid,
  AdminPage,
  AdminPageHeader,
  AdminSection,
} from "@/components/admin/ui/admin-page";
import { useOperationsOverview } from "@/core/admin-operations/api";
import type { OperationsOverviewData } from "@/core/admin-operations/types";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

import {
  AdminEmptyState,
  AdminErrorState,
  AdminLoadingState,
  AdminStatus,
} from "./admin-operations-ui";

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
  const { locale, t } = useI18n();
  const labels = t.adminOperations.overview;
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
    <AdminSection
      title={labels.readiness.title}
      actions={
        <AdminStatus status={readiness.status}>
          {readinessState(readiness.status)}
        </AdminStatus>
      }
      contentClassName="p-0"
    >
      <dl
        data-slot="admin-readiness-grid"
        className="bg-border grid gap-px text-sm sm:grid-cols-2 xl:grid-cols-7"
      >
        {readinessComponents.map(([component, value], index) => (
          <div
            key={component}
            className={
              index === readinessComponents.length - 1
                ? "bg-card flex min-w-0 items-center justify-between gap-3 px-4 py-3 sm:col-span-2 sm:block xl:col-span-1"
                : "bg-card flex min-w-0 items-center justify-between gap-3 px-4 py-3 sm:block"
            }
          >
            <dt className="text-muted-foreground text-xs font-medium">
              {labels.readiness.components[component]}
            </dt>
            <dd className="mt-0 sm:mt-2">
              <AdminStatus status={value}>{readinessState(value)}</AdminStatus>
            </dd>
          </div>
        ))}
      </dl>
      <dl className="border-border bg-muted/30 grid border-t text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div className="border-border/70 px-4 py-3 sm:border-r">
          <dt className="text-muted-foreground text-xs">
            {labels.readiness.workerCount}
          </dt>
          <dd className="mt-1 font-medium tabular-nums">
            {readiness.worker_count}
          </dd>
        </div>
        <div className="border-border/70 px-4 py-3 lg:border-r">
          <dt className="text-muted-foreground text-xs">
            {labels.readiness.workerCapacity}
          </dt>
          <dd className="mt-1 font-medium tabular-nums">
            {readiness.worker_capacity}
          </dd>
        </div>
        <div className="border-border/70 px-4 py-3 sm:border-r">
          <dt className="text-muted-foreground text-xs">
            {labels.readiness.oldestHeartbeat}
          </dt>
          <dd className="mt-1 font-medium">
            {readiness.worker_oldest_heartbeat_age_seconds === null
              ? labels.readiness.notReported
              : labels.readiness.secondsAgo.replace(
                  "{seconds}",
                  String(readiness.worker_oldest_heartbeat_age_seconds),
                )}
          </dd>
        </div>
        <div className="px-4 py-3">
          <dt className="text-muted-foreground text-xs">
            {labels.readiness.schedulerOwnership}
          </dt>
          <dd className="mt-1 font-medium">
            {readinessState(readiness.scheduler_ownership)}
          </dd>
        </div>
      </dl>
    </AdminSection>
  );
  if (state.data.data_status === "unavailable") {
    return (
      <div className="space-y-5">
        {readinessView}
        <AdminErrorState
          title={labels.unavailableTitle}
          description={labels.unavailableDescription}
          retryLabel={t.adminOperations.retry}
          onRetry={onRetry}
        />
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
    <div className="space-y-5">
      <AdminMetricGrid aria-label={labels.title} className="xl:grid-cols-5">
        {counts.map(([label, value], index) => (
          <AdminMetric
            key={label}
            className={
              index === counts.length - 1
                ? "sm:col-span-2 xl:col-span-1"
                : undefined
            }
            label={label}
            value={value}
          />
        ))}
      </AdminMetricGrid>
      {readinessView}
      <AdminSection
        title={labels.usage.title}
        aria-label={labels.usage.title}
        contentClassName="grid gap-px bg-border p-0 sm:grid-cols-2"
      >
        {state.data.usage.map((item) => (
          <div
            key={item.dimension}
            className="bg-card grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-5 px-4 py-4 text-sm"
          >
            <p className="font-medium">{labels.usage[item.dimension]}</p>
            <dl className="contents">
              <div className="text-right">
                <dt className="text-muted-foreground text-xs">
                  {labels.usage.used}
                </dt>
                <dd className="mt-1 font-semibold tabular-nums">
                  {formatUsageValue(item.dimension, item.used, locale)}
                </dd>
              </div>
              <div className="text-right">
                <dt className="text-muted-foreground text-xs">
                  {labels.usage.reserved}
                </dt>
                <dd className="mt-1 font-semibold tabular-nums">
                  {formatUsageValue(item.dimension, item.reserved, locale)}
                </dd>
              </div>
            </dl>
          </div>
        ))}
      </AdminSection>
      <AdminSection
        title={labels.channels.title}
        contentClassName={
          state.data.channel_providers.length === 0 ? "p-4" : "p-0"
        }
      >
        {state.data.channel_providers.length === 0 ? (
          <AdminEmptyState
            title={labels.channels.emptyTitle}
            description={labels.channels.empty}
            variant="inline"
          />
        ) : (
          <ul
            data-slot="admin-channel-grid"
            className="bg-border grid gap-px sm:grid-cols-2 lg:grid-cols-3"
          >
            {state.data.channel_providers.map((provider, index) => (
              <li
                key={provider.provider}
                className={cn(
                  "bg-card flex min-w-0 items-start justify-between gap-3 px-4 py-3",
                  index === state.data.channel_providers.length - 1 &&
                    state.data.channel_providers.length % 2 === 1 &&
                    "sm:col-span-2",
                  index === state.data.channel_providers.length - 1 &&
                    state.data.channel_providers.length % 3 === 1 &&
                    "lg:col-span-3",
                  index === state.data.channel_providers.length - 1 &&
                    state.data.channel_providers.length % 3 === 2 &&
                    "lg:col-span-2",
                )}
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">
                    {provider.provider}
                  </p>
                  <p className="text-muted-foreground mt-1 text-xs">
                    {labels.channels.checkedAt.replace(
                      "{time}",
                      new Date(provider.checked_at).toLocaleString(locale),
                    )}
                  </p>
                </div>
                <AdminStatus status={provider.status}>
                  {readinessState(provider.status)}
                </AdminStatus>
              </li>
            ))}
          </ul>
        )}
      </AdminSection>
    </div>
  );
}

export function OperationsOverview() {
  const { user } = useAuth();
  if (user?.system_role !== "system_admin") return null;
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
    <AdminPage>
      <AdminPageHeader
        title={t.adminOperations.overview.title}
        description={t.adminOperations.overview.description}
      />
      <OperationsOverviewStateView
        state={state}
        onRetry={() => void overview.refetch()}
      />
    </AdminPage>
  );
}

function formatUsageValue(
  dimension: NonNullable<OperationsOverviewData["usage"]>[number]["dimension"],
  value: number,
  locale: string,
) {
  if (dimension !== "storage_bytes") {
    return new Intl.NumberFormat(locale).format(value);
  }
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"] as const;
  let scaled = value;
  let unit: (typeof units)[number] = units[0];
  for (const candidate of units) {
    scaled /= 1024;
    unit = candidate;
    if (scaled < 1024 || candidate === units.at(-1)) break;
  }
  return `${new Intl.NumberFormat(locale, {
    maximumFractionDigits: scaled < 10 ? 1 : 0,
  }).format(scaled)} ${unit}`;
}
