"use client";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import type {
  ProjectUsage,
  UpdateQuotaLimits,
} from "@/core/project-governance/usage";

import {
  describeUsageDimension,
  type UsageDimension,
  usageViewCopy,
} from "./project-usage-view-model";

function parseLimit(form: FormData, name: string): number | null {
  const value = form.get(name);
  if (typeof value !== "string" || value.trim() === "") return null;
  return Number(value);
}

export function ProjectQuotaLimitsEditor({
  data,
  pending,
  error,
  onSubmit,
}: {
  data: ProjectUsage;
  pending: boolean;
  error: boolean;
  onSubmit: (input: UpdateQuotaLimits) => void;
}) {
  const { locale, t } = useI18n();
  const labels = t.project.governance.usage;
  const copy = usageViewCopy[locale];
  const configured = data.policy.configured;
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
    <form
      key={data.policy.version}
      className="bg-card rounded-2xl border p-5 shadow-xs sm:p-6"
      onSubmit={(event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        onSubmit({
          expected_version: data.policy.version,
          limits: {
            member_limit: parseLimit(form, "member_limit"),
            storage_bytes_limit: parseLimit(form, "storage_bytes_limit"),
            concurrent_run_limit: parseLimit(form, "concurrent_run_limit"),
            mcp_calls_daily_limit: parseLimit(form, "mcp_calls_daily_limit"),
          },
        });
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        {fields.map((field) => {
          const detail = describeUsageDimension(data, field.dimension, locale);
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
      {error ? (
        <p role="alert" className="mt-4 text-sm text-red-600">
          {labels.updateError}
        </p>
      ) : null}
      <div className="mt-5 flex justify-end border-t pt-5">
        <Button type="submit" disabled={pending}>
          {pending ? labels.saving : labels.save}
        </Button>
      </div>
    </form>
  );
}
