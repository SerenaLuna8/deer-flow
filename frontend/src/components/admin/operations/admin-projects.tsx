"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAdminProjects } from "@/core/admin-operations/api";
import type { AdminProjectPage } from "@/core/admin-operations/types";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";

export type AdminProjectsState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: AdminProjectPage };

export function AdminProjectsStateView({
  state,
  onRetry,
}: {
  state: AdminProjectsState;
  onRetry?: () => void;
}) {
  const { t } = useI18n();
  const labels = t.adminOperations.projects;
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
            {t.adminOperations.retry}
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
      {state.data.items.map((project) => (
        <li key={project.project_id} className="bg-card rounded-xl border p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <code className="text-sm">{project.project_id}</code>
            <span className="text-muted-foreground text-sm">
              {project.status}
              {project.is_suspended ? ` · ${labels.suspended}` : ""}
            </span>
          </div>
          <time
            className="text-muted-foreground mt-2 block text-xs"
            dateTime={project.updated_at}
          >
            {new Date(project.updated_at).toLocaleString()}
          </time>
        </li>
      ))}
    </ol>
  );
}

export function AdminProjects() {
  const { user } = useAuth();
  if (!user || user.system_role !== "system_admin") return null;
  return <AuthorizedAdminProjects accountId={user.id} />;
}

function AuthorizedAdminProjects({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const [cursor, setCursor] = useState<string | null>(null);
  const projects = useAdminProjects(accountId, cursor);
  const state: AdminProjectsState = projects.isLoading
    ? { status: "loading" }
    : projects.error || !projects.data
      ? { status: "error" }
      : { status: "ready", data: projects.data };
  return (
    <main className="mx-auto max-w-7xl space-y-6 px-4 py-8 lg:px-6">
      <div>
        <h1 className="font-serif text-2xl">
          {t.adminOperations.projects.title}
        </h1>
        <p className="text-muted-foreground mt-1 text-sm">
          {t.adminOperations.projects.description}
        </p>
      </div>
      <AdminProjectsStateView
        state={state}
        onRetry={() => void projects.refetch()}
      />
      {projects.data?.next_cursor ? (
        <Button
          type="button"
          variant="outline"
          onClick={() => setCursor(projects.data?.next_cursor ?? null)}
        >
          {t.adminOperations.projects.older}
        </Button>
      ) : null}
    </main>
  );
}
