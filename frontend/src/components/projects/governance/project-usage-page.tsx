"use client";

import { notFound } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useProjectUsage,
  useUpdateProjectQuotaLimits,
  type ProjectUsage,
} from "@/core/project-governance/usage";
import { isStaticWebsiteOnly } from "@/core/static-mode";

import { useCurrentProject } from "../project-context";

const DIMENSION_LABELS = {
  members: "Members",
  storage_bytes: "Storage bytes",
  concurrent_runs: "Concurrent runs",
  mcp_calls_daily: "Daily MCP calls",
} as const;

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
  if (state.status === "loading") {
    return (
      <section
        aria-busy="true"
        aria-label="Loading usage"
        className="space-y-4"
      >
        <p>Loading usage</p>
        <Skeleton className="h-28 w-full rounded-xl" />
      </section>
    );
  }
  if (state.status === "error") {
    return (
      <section role="alert" className="rounded-xl border p-6">
        <h2 className="font-semibold">Usage is unavailable</h2>
        <p className="text-muted-foreground mt-2 text-sm">
          The project usage service could not be read safely.
        </p>
        {onRetry ? (
          <Button
            className="mt-4"
            type="button"
            variant="outline"
            onClick={onRetry}
          >
            Retry
          </Button>
        ) : null}
      </section>
    );
  }
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {state.data.dimensions.map((item) => (
        <section key={item.dimension} className="bg-card rounded-xl border p-5">
          <div className="flex items-center justify-between gap-3">
            <h2 className="font-semibold">
              {DIMENSION_LABELS[item.dimension]}
            </h2>
            {item.warning_threshold_reached ? (
              <span className="text-amber-700 dark:text-amber-300">
                80% threshold reached
              </span>
            ) : null}
          </div>
          <dl className="mt-4 grid grid-cols-3 gap-3 text-sm">
            <div>
              <dt className="text-muted-foreground">Used</dt>
              <dd>{item.used}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Reserved</dt>
              <dd>{item.reserved}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Limit</dt>
              <dd>{item.limit}</dd>
            </div>
          </dl>
        </section>
      ))}
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
  const usage = useProjectUsage(canRead && !staticMode);
  const update = useUpdateProjectQuotaLimits();

  if (!canRead || staticMode) notFound();
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
  return (
    <div className="space-y-6">
      <ProjectUsageStateView state={{ status: "ready", data: usage.data }} />
      <form
        key={usage.data.policy.version}
        className="bg-card rounded-xl border p-5"
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
        <h2 className="font-semibold">Tighten project limits</h2>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {(
            [
              ["member_limit", "Members"],
              ["storage_bytes_limit", "Storage bytes"],
              ["concurrent_run_limit", "Concurrent runs"],
              ["mcp_calls_daily_limit", "Daily MCP calls"],
            ] as const
          ).map(([name, label]) => (
            <label key={name} className="grid gap-2 text-sm">
              {label}
              <input
                className="border-input bg-background h-9 rounded-md border px-3"
                type="number"
                min={0}
                name={name}
                defaultValue={configured[name] ?? ""}
              />
            </label>
          ))}
        </div>
        {update.error ? (
          <p role="alert" className="mt-4 text-sm text-red-600">
            Limits were not updated. Refresh and retry.
          </p>
        ) : null}
        <Button className="mt-5" type="submit" disabled={update.isPending}>
          {update.isPending ? "Saving…" : "Save limits"}
        </Button>
      </form>
    </div>
  );
}
