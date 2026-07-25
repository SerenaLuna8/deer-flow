"use client";

import { CheckCircle2Icon, CircleXIcon, ShieldAlertIcon } from "lucide-react";
import { notFound } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { ProjectClientScope } from "@/core/private-work/types";
import {
  useProjectAudit,
  type ProjectAuditPage as ProjectAuditPageData,
} from "@/core/project-governance/audit";
import { isStaticWebsiteOnly } from "@/core/static-mode";

import { ProjectAccessDenied } from "../project-access-denied";
import { useCurrentProject } from "../project-context";

import { describeAuditItem } from "./project-audit-view-model";

export type ProjectAuditState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: ProjectAuditPageData };

export function ProjectAuditStateView({
  state,
  onRetry,
}: {
  state: ProjectAuditState;
  onRetry?: () => void;
}) {
  const { locale, t } = useI18n();
  const labels = t.project.governance.audit;
  if (state.status === "loading") {
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
  if (state.data.items.length === 0) {
    return (
      <section className="rounded-xl border p-8 text-center">
        <h2 className="font-semibold">{labels.emptyTitle}</h2>
        <p className="text-muted-foreground mt-2 text-sm">
          {labels.emptyDescription}
        </p>
      </section>
    );
  }
  return (
    <ol className="before:bg-border relative space-y-4 before:absolute before:top-6 before:bottom-6 before:left-[1.15rem] before:w-px sm:before:left-[1.4rem]">
      {state.data.items.map((item) => {
        const detail = describeAuditItem(item, locale);
        const OutcomeIcon =
          item.outcome === "success"
            ? CheckCircle2Icon
            : item.outcome === "rejected"
              ? ShieldAlertIcon
              : CircleXIcon;
        return (
          <li
            key={item.id}
            data-action={item.action}
            className="relative grid grid-cols-[2.3rem_minmax(0,1fr)] gap-3 sm:grid-cols-[2.8rem_minmax(0,1fr)] sm:gap-4"
          >
            <span
              className={
                item.outcome === "success"
                  ? "bg-background relative z-10 flex size-9 items-center justify-center rounded-full border text-emerald-600 sm:size-11 dark:text-emerald-400"
                  : item.outcome === "rejected"
                    ? "bg-background relative z-10 flex size-9 items-center justify-center rounded-full border text-amber-600 sm:size-11 dark:text-amber-300"
                    : "bg-background relative z-10 flex size-9 items-center justify-center rounded-full border text-red-600 sm:size-11 dark:text-red-400"
              }
            >
              <OutcomeIcon aria-hidden className="size-4 sm:size-5" />
            </span>

            <article className="bg-card rounded-2xl border p-5 shadow-xs">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <h2 className="font-semibold">{detail.action}</h2>
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                    <span className="bg-muted rounded-full px-2.5 py-1">
                      {detail.actor}
                    </span>
                    <span className="text-muted-foreground">
                      {locale === "zh-CN" ? "作用于" : "Target"} {detail.target}
                    </span>
                    <span
                      className={
                        item.outcome === "success"
                          ? "text-emerald-700 dark:text-emerald-300"
                          : item.outcome === "rejected"
                            ? "text-amber-700 dark:text-amber-300"
                            : "text-red-700 dark:text-red-300"
                      }
                    >
                      {detail.outcome}
                    </span>
                  </div>
                </div>
                <time
                  className="text-muted-foreground shrink-0 text-xs tabular-nums"
                  dateTime={item.occurred_at}
                >
                  {detail.occurredAt}
                </time>
              </div>

              {detail.metadata.length > 0 ? (
                <dl className="bg-muted/30 mt-4 grid gap-x-6 gap-y-3 rounded-xl border px-4 py-3 text-sm sm:grid-cols-2">
                  {detail.metadata.map((entry) => (
                    <div key={entry.label} className="min-w-0">
                      <dt className="text-muted-foreground text-xs">
                        {entry.label}
                      </dt>
                      <dd className="mt-1 font-medium break-words tabular-nums">
                        {entry.value}
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : null}

              {detail.publicErrorCode ? (
                <p className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200">
                  {locale === "zh-CN" ? "公开错误代码" : "Public error code"}
                  {": "}
                  <code>{detail.publicErrorCode}</code>
                </p>
              ) : null}
            </article>
          </li>
        );
      })}
    </ol>
  );
}

export function ProjectAuditPage() {
  const project = useCurrentProject();
  const canRead = project.capabilities.includes("project.audit.read");
  const staticMode = isStaticWebsiteOnly();
  const access = usePrivateWorkAccess();

  if (staticMode) notFound();
  if (!canRead) {
    return <ProjectAccessDenied projectSlug={project.slug} area="项目审计" />;
  }
  return (
    <AuthorizedProjectAuditPage
      key={access.scope.projectId}
      scope={access.scope}
    />
  );
}

function AuthorizedProjectAuditPage({ scope }: { scope: ProjectClientScope }) {
  const { t } = useI18n();
  const [cursor, setCursor] = useState<string | null>(null);
  const audit = useProjectAudit(scope, cursor, 50);

  if (audit.isLoading)
    return <ProjectAuditStateView state={{ status: "loading" }} />;
  if (audit.error || !audit.data) {
    return (
      <ProjectAuditStateView
        state={{ status: "error" }}
        onRetry={() => void audit.refetch()}
      />
    );
  }
  return (
    <div className="space-y-5">
      <ProjectAuditStateView state={{ status: "ready", data: audit.data }} />
      {audit.data.next_cursor ? (
        <Button
          type="button"
          variant="outline"
          onClick={() => setCursor(audit.data.next_cursor)}
        >
          {t.project.governance.audit.olderEvents}
        </Button>
      ) : null}
    </div>
  );
}
