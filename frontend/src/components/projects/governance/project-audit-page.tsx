"use client";

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

import { useCurrentProject } from "../project-context";

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
  const { t } = useI18n();
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
    <ol className="space-y-3">
      {state.data.items.map((item) => (
        <li key={item.id} className="bg-card rounded-xl border p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <strong>{item.action}</strong>
            <time
              className="text-muted-foreground text-sm"
              dateTime={item.occurred_at}
            >
              {new Date(item.occurred_at).toLocaleString()}
            </time>
          </div>
          <p className="text-muted-foreground mt-2 text-sm">
            {item.actor} · {item.target_kind} · {item.outcome}
          </p>
          {Object.keys(item.metadata).length > 0 ? (
            <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
              {Object.entries(item.metadata).map(([key, value]) => (
                <div key={key}>
                  <dt className="text-muted-foreground">{key}</dt>
                  <dd>{String(value)}</dd>
                </div>
              ))}
            </dl>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

export function ProjectAuditPage() {
  const project = useCurrentProject();
  const canRead = project.capabilities.includes("project.audit.read");
  const staticMode = isStaticWebsiteOnly();
  const access = usePrivateWorkAccess();

  if (!canRead || staticMode) notFound();
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
