"use client";

import { ActivityIcon, DatabaseIcon, GaugeIcon, UsersIcon } from "lucide-react";
import { notFound } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type {
  PrivateWorkAccess,
  ProjectClientScope,
} from "@/core/private-work/types";
import {
  useProjectUsage,
  useUpdateProjectQuotaLimits,
  type ProjectUsage,
} from "@/core/project-governance/usage";
import { isStaticWebsiteOnly } from "@/core/static-mode";

import { ProjectAccessDenied } from "../project-access-denied";
import { useCurrentProject } from "../project-context";

import {
  describeUsageDimension,
  type UsageDimension,
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
      <section className="bg-muted/35 flex flex-col gap-2 rounded-2xl border px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold">{copy.effectiveLimit}</h2>
          <p className="text-muted-foreground mt-1 text-sm leading-6">
            {copy.policyExplanation}
          </p>
        </div>
        <span className="bg-background text-muted-foreground w-fit rounded-full border px-3 py-1 text-xs">
          {locale === "zh-CN"
            ? `策略版本 ${state.data.policy.version}`
            : `Policy version ${state.data.policy.version}`}
        </span>
      </section>

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

function parseLimit(form: FormData, name: string): number | null {
  const value = form.get(name);
  if (typeof value !== "string" || value.trim() === "") return null;
  return Number(value);
}

export function ProjectUsagePage() {
  const project = useCurrentProject();
  const canRead = project.capabilities.includes("project.usage.read");
  const staticMode = isStaticWebsiteOnly();
  const access = usePrivateWorkAccess();

  if (staticMode) notFound();
  if (!canRead) {
    return (
      <ProjectAccessDenied projectSlug={project.slug} area="项目用量与限额" />
    );
  }
  return <AuthorizedProjectUsagePage access={access} scope={access.scope} />;
}

function AuthorizedProjectUsagePage({
  access,
  scope,
}: {
  access: PrivateWorkAccess;
  scope: ProjectClientScope;
}) {
  const { locale, t } = useI18n();
  const labels = t.project.governance.usage;
  const copy = usageViewCopy[locale];
  const usage = useProjectUsage(scope);
  const update = useUpdateProjectQuotaLimits(access);

  if (usage.isLoading) {
    return <ProjectUsageStateView state={{ status: "loading" }} />;
  }
  if (usage.error || !usage.data) {
    return (
      <ProjectUsageStateView
        state={{ status: "error" }}
        onRetry={() => void usage.refetch()}
      />
    );
  }

  const configured = usage.data.policy.configured;
  const fields = [
    {
      name: "member_limit",
      dimension: "members",
      label: labels.dimensions.members,
      minimum: 1,
    },
    {
      name: "storage_bytes_limit",
      dimension: "storage_bytes",
      label: labels.dimensions.storage_bytes,
      minimum: 0,
    },
    {
      name: "concurrent_run_limit",
      dimension: "concurrent_runs",
      label: labels.dimensions.concurrent_runs,
      minimum: 1,
    },
    {
      name: "mcp_calls_daily_limit",
      dimension: "mcp_calls_daily",
      label: labels.dimensions.mcp_calls_daily,
      minimum: 0,
    },
  ] as const satisfies ReadonlyArray<{
    name: keyof typeof configured;
    dimension: UsageDimension;
    label: string;
    minimum: number;
  }>;

  return (
    <div className="space-y-6">
      <ProjectUsageStateView state={{ status: "ready", data: usage.data }} />
      <form
        key={usage.data.policy.version}
        className="bg-card rounded-2xl border p-5 shadow-xs sm:p-6"
        onSubmit={(event) => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          update.mutate({
            expected_version: usage.data.policy.version,
            limits: {
              member_limit: parseLimit(form, "member_limit"),
              storage_bytes_limit: parseLimit(form, "storage_bytes_limit"),
              concurrent_run_limit: parseLimit(form, "concurrent_run_limit"),
              mcp_calls_daily_limit: parseLimit(form, "mcp_calls_daily_limit"),
            },
          });
        }}
      >
        <div className="max-w-2xl">
          <h2 className="font-semibold">{labels.tightenTitle}</h2>
          <p className="text-muted-foreground mt-2 text-sm leading-6">
            {copy.editorDescription}
          </p>
        </div>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {fields.map((field) => {
            const detail = describeUsageDimension(
              usage.data,
              field.dimension,
              locale,
            );
            return (
              <label
                key={field.name}
                className="bg-muted/25 grid gap-2 rounded-xl border p-4 text-sm"
              >
                <span className="font-medium">{field.label}</span>
                <input
                  className="border-input bg-background h-10 rounded-lg border px-3 tabular-nums"
                  type="number"
                  min={field.minimum}
                  name={field.name}
                  defaultValue={configured[field.name] ?? ""}
                />
                <span className="text-muted-foreground text-xs leading-5">
                  {detail.inheritsPlatformLimit
                    ? copy.inheritedValue
                    : `${copy.configuredValue} ${detail.configuredLimit}`}
                  {" · "}
                  {copy.effectiveValue} {detail.effectiveLimit}
                  {field.dimension === "storage_bytes"
                    ? ` · ${copy.bytesInputHint}`
                    : ""}
                </span>
              </label>
            );
          })}
        </div>
        {update.error ? (
          <p role="alert" className="mt-4 text-sm text-red-600">
            {labels.updateError}
          </p>
        ) : null}
        <div className="mt-5 flex justify-end border-t pt-5">
          <Button type="submit" disabled={update.isPending}>
            {update.isPending ? labels.saving : labels.save}
          </Button>
        </div>
      </form>
    </div>
  );
}
