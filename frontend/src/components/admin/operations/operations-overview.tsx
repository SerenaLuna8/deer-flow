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
  adminStatusTone,
} from "./admin-operations-ui";

export type OperationsOverviewState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: OperationsOverviewData };

const READINESS_COMPONENTS = [
  "database",
  "schema",
  "worker_fleet",
  "private_run_worker_fleet",
  "run_skill_writer",
  "scheduler",
  "stream",
  "quota",
  "audit",
] as const;

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
  const readinessState = (value: string) => {
    const states = labels.readiness.states;
    return value in states
      ? states[value as keyof typeof states]
      : states.unknown;
  };
  const componentStatuses = {
    database: readiness.database,
    schema: readiness.schema_state,
    worker_fleet: readiness.worker_fleet,
    private_run_worker_fleet: readiness.private_run_worker_fleet,
    run_skill_writer: readiness.run_skill_writer_ready
      ? "ready"
      : "unavailable",
    scheduler: readiness.scheduler,
    stream: readiness.stream,
    quota: readiness.quota,
    audit: readiness.audit,
  } as const;
  const fleetFacts = [
    {
      label: labels.readiness.workerCount,
      value: readiness.worker_count,
    },
    {
      label: labels.readiness.workerCapacity,
      value: readiness.worker_capacity,
    },
    {
      label: labels.readiness.privateRunWorkerCount,
      value: readiness.private_run_worker_count,
    },
    {
      label: labels.readiness.privateRunWorkerCapacity,
      value: readiness.private_run_worker_capacity,
    },
    {
      label: labels.readiness.oldestHeartbeat,
      value:
        readiness.worker_oldest_heartbeat_age_seconds === null
          ? labels.readiness.notReported
          : labels.readiness.secondsAgo.replace(
              "{seconds}",
              String(readiness.worker_oldest_heartbeat_age_seconds),
            ),
    },
    {
      label: labels.readiness.schedulerOwnership,
      value: readinessState(readiness.scheduler_ownership),
    },
    {
      label: labels.readiness.runSkillWriterMode,
      value: readinessState(readiness.run_skill_writer_mode),
    },
    {
      label: labels.readiness.runSkillWriterArtifact,
      value: readiness.run_skill_writer_artifact_version,
    },
    {
      label: labels.readiness.legacyPolicyDigest,
      value: readiness.run_skill_legacy_policy_digest,
    },
  ] as const;

  const readinessView = (
    <AdminSection
      title={labels.readiness.title}
      actions={
        <AdminStatus status={readiness.status}>
          {readinessState(readiness.status)}
        </AdminStatus>
      }
      contentClassName="space-y-4 p-4"
    >
      <ul
        data-slot="admin-readiness-grid"
        className="flex flex-wrap gap-2"
        aria-label={labels.readiness.title}
      >
        {READINESS_COMPONENTS.map((component) => {
          const value = componentStatuses[component];
          const tone = adminStatusTone(value);
          return (
            <li
              key={component}
              className={cn(
                "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium",
                tone === "healthy" &&
                  "border-border/80 bg-muted/40 text-foreground",
                tone === "warning" &&
                  "border-chart-4/30 bg-chart-4/10 text-foreground",
                tone === "danger" &&
                  "border-destructive/25 bg-destructive/10 text-destructive",
                tone === "neutral" &&
                  "border-border bg-muted text-muted-foreground",
              )}
            >
              <span
                aria-hidden
                className={cn(
                  "size-1.5 rounded-full",
                  tone === "healthy" && "bg-success",
                  tone === "warning" && "bg-chart-4",
                  tone === "danger" && "bg-destructive",
                  tone === "neutral" && "bg-muted-foreground/60",
                )}
              />
              <span>{labels.readiness.components[component]}</span>
              <span className="text-muted-foreground">
                {readinessState(value)}
              </span>
            </li>
          );
        })}
      </ul>
      <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {fleetFacts.map((fact) => (
          <div
            key={fact.label}
            className="bg-muted/35 min-w-0 rounded-lg px-3.5 py-3"
          >
            <dt className="text-muted-foreground text-xs font-medium">
              {fact.label}
            </dt>
            <dd className="mt-1.5 text-sm font-semibold tracking-tight break-all tabular-nums">
              {fact.value}
            </dd>
          </div>
        ))}
      </dl>
    </AdminSection>
  );

  if (state.data.data_status === "unavailable") {
    return (
      <div className="space-y-4">
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
    [labels.counts.readyJobs, state.data.counts.ready_jobs],
    [
      labels.counts.oldestReadyJobAge,
      state.data.counts.oldest_ready_job_age_seconds ??
        labels.readiness.notReported,
    ],
    [labels.counts.staleLeases, state.data.counts.stale_leases],
    [
      labels.counts.waitingForWorkerRuns,
      state.data.counts.waiting_for_worker_runs,
    ],
    [
      labels.counts.waitingForTerminalizationRuns,
      state.data.counts.waiting_for_terminalization_runs,
    ],
  ] as const;

  return (
    <div className="space-y-4">
      <AdminMetricGrid aria-label={labels.title} className="xl:grid-cols-5">
        {counts.map(([label, value]) => (
          <AdminMetric
            key={label}
            className="px-4 py-4"
            label={label}
            value={value}
          />
        ))}
      </AdminMetricGrid>

      {readinessView}

      <AdminSection title={labels.usage.title} contentClassName="p-3">
        <ul className="grid gap-2 sm:grid-cols-2">
          {state.data.usage.map((item) => {
            const used = formatUsageValue(item.dimension, item.used, locale);
            const reserved = formatUsageValue(
              item.dimension,
              item.reserved,
              locale,
            );
            return (
              <li
                key={item.dimension}
                className="bg-muted/35 flex min-w-0 items-baseline justify-between gap-4 rounded-lg px-3.5 py-3.5"
              >
                <p className="text-sm font-medium">
                  {labels.usage[item.dimension]}
                </p>
                <p
                  className="text-sm font-semibold tracking-tight tabular-nums"
                  aria-label={`${labels.usage.used} ${used}, ${labels.usage.reserved} ${reserved}`}
                >
                  <span>{used}</span>
                  <span className="text-muted-foreground mx-1.5 font-normal">
                    /
                  </span>
                  <span className="text-muted-foreground font-medium">
                    {reserved}
                  </span>
                </p>
              </li>
            );
          })}
        </ul>
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
            className="grid gap-px bg-transparent p-3 sm:grid-cols-2 lg:grid-cols-4"
          >
            {state.data.channel_providers.map((provider) => (
              <li
                key={provider.provider}
                className="bg-muted/35 flex min-w-0 items-center justify-between gap-3 rounded-lg px-3.5 py-3"
              >
                <p className="truncate text-sm font-medium">
                  {provider.provider}
                </p>
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
      <AdminPageHeader title={t.adminOperations.overview.title} />
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
