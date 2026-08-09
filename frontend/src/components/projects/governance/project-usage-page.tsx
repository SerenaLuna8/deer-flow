"use client";

import { ActivityIcon, DatabaseIcon, GaugeIcon, UsersIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { ProjectClientScope } from "@/core/private-work/types";
import {
  useProjectUsage,
  type ProjectUsage,
} from "@/core/project-governance/usage";

import {
  describeUsageDimension,
  usageViewCopy,
} from "./project-usage-view-model";

export type ProjectUsageState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: ProjectUsage };

export function ProjectUsageStateView({
  state,
  onRetry,
}: {
  state: ProjectUsageState;
  onRetry?: () => void;
}) {
  const { locale, t } = useI18n();
  const labels = t.project.governance.usage;
  if (state.status === "loading") {
    return (
      <section
        aria-busy="true"
        aria-label={labels.loading}
        className="space-y-4"
      >
        <p>{labels.loading}</p>
        <Skeleton className="h-28 w-full rounded-xl" />
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
            {t.project.governance.retry}
          </Button>
        ) : null}
      </section>
    );
  }
  const copy = usageViewCopy[locale];
  const iconByDimension = {
    members: UsersIcon,
    storage_bytes: DatabaseIcon,
    concurrent_runs: ActivityIcon,
    mcp_calls_daily: GaugeIcon,
  } as const;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2">
        {state.data.dimensions.map((item) => {
          const detail = describeUsageDimension(
            state.data,
            item.dimension,
            locale,
          );
          const Icon = iconByDimension[item.dimension];
          return (
            <section
              key={item.dimension}
              className="bg-card rounded-2xl border p-5 shadow-xs"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex min-w-0 items-center gap-3">
                  <span className="bg-muted flex size-9 shrink-0 items-center justify-center rounded-xl">
                    <Icon aria-hidden className="size-4" />
                  </span>
                  <div>
                    <h2 className="font-semibold">
                      {labels.dimensions[item.dimension]}
                    </h2>
                    <p className="text-muted-foreground mt-0.5 text-xs">
                      {detail.bucket}
                    </p>
                  </div>
                </div>
                <div className="flex flex-wrap justify-end gap-1.5">
                  {item.warning_threshold_reached ? (
                    <span className="rounded-full bg-amber-100 px-2.5 py-1 text-xs font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-200">
                      {labels.thresholdReached}
                    </span>
                  ) : null}
                  <span className="bg-muted text-muted-foreground rounded-full px-2.5 py-1 text-xs">
                    {detail.inheritsPlatformLimit
                      ? copy.inheritedLimit
                      : copy.configuredLimit}
                  </span>
                </div>
              </div>

              <div className="mt-6 flex items-end justify-between gap-3">
                <div>
                  <p className="text-muted-foreground text-xs">
                    {copy.currentOccupancy}
                  </p>
                  <p className="mt-1 text-2xl font-semibold tracking-tight tabular-nums">
                    {detail.consumed}
                    <span className="text-muted-foreground ml-1.5 text-sm font-normal">
                      / {detail.limit}
                    </span>
                  </p>
                </div>
                <strong className="text-sm tabular-nums">
                  {detail.progressText}
                </strong>
              </div>
              <Progress
                aria-label={`${labels.dimensions[item.dimension]} ${copy.progressLabel}`}
                aria-valuenow={detail.progressValue}
                className="mt-3 h-2"
                value={detail.progressValue}
              />

              <dl className="mt-5 grid grid-cols-3 gap-3 border-t pt-4 text-sm">
                <div>
                  <dt className="text-muted-foreground text-xs">
                    {labels.used}
                  </dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {detail.used}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground text-xs">
                    {labels.reserved}
                  </dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {detail.reserved}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground text-xs">
                    {copy.effectiveLimit}
                  </dt>
                  <dd className="mt-1 font-medium tabular-nums">
                    {detail.effectiveLimit}
                  </dd>
                </div>
              </dl>
            </section>
          );
        })}
      </div>
    </div>
  );
}

function useProjectUsageState(scope: ProjectClientScope): {
  state: ProjectUsageState;
  onRetry: () => void;
} {
  const usage = useProjectUsage(scope);
  if (usage.isLoading) {
    return {
      state: { status: "loading" },
      onRetry: () => void usage.refetch(),
    };
  }
  if (usage.error || !usage.data) {
    return {
      state: { status: "error" },
      onRetry: () => void usage.refetch(),
    };
  }
  return {
    state: { status: "ready", data: usage.data },
    onRetry: () => void usage.refetch(),
  };
}

/** Live occupancy cards for the project overview (below Token usage). */
export function ProjectUsageDimensionsSection() {
  const { t } = useI18n();
  const labels = t.project.governance.usage;
  const access = usePrivateWorkAccess();
  const { state, onRetry } = useProjectUsageState(access.scope);

  return (
    <section data-testid="project-usage-dimensions" className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold">{labels.title}</h2>
        <p className="text-muted-foreground mt-1 text-sm">
          {labels.description}
        </p>
      </div>
      <ProjectUsageStateView state={state} onRetry={onRetry} />
    </section>
  );
}
