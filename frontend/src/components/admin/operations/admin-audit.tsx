"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAdminAudit } from "@/core/admin-operations/api";
import type { AdminAuditPage } from "@/core/admin-operations/types";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";

export type AdminAuditState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: AdminAuditPage };

export function AdminAuditStateView({
  state,
  onRetry,
}: {
  state: AdminAuditState;
  onRetry?: () => void;
}) {
  const { t } = useI18n();
  const labels = t.adminOperations.audit;
  if (state.status === "loading")
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
  if (state.status === "error")
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
  if (state.data.items.length === 0)
    return (
      <section className="rounded-xl border p-8 text-center">
        <h2 className="font-semibold">{labels.emptyTitle}</h2>
        <p className="text-muted-foreground mt-2 text-sm">
          {labels.emptyDescription}
        </p>
      </section>
    );
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

export function AdminAudit() {
  const { user } = useAuth();
  if (!user || user.system_role !== "system_admin") return null;
  return <AuthorizedAdminAudit accountId={user.id} />;
}

function AuthorizedAdminAudit({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const [cursor, setCursor] = useState<string | null>(null);
  const audit = useAdminAudit(accountId, cursor);
  const state: AdminAuditState = audit.isLoading
    ? { status: "loading" }
    : audit.error || !audit.data
      ? { status: "error" }
      : { status: "ready", data: audit.data };
  return (
    <main className="mx-auto max-w-7xl space-y-6 px-4 py-8 lg:px-6">
      <div>
        <h1 className="font-serif text-2xl">{t.adminOperations.audit.title}</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          {t.adminOperations.audit.description}
        </p>
      </div>
      <AdminAuditStateView state={state} onRetry={() => void audit.refetch()} />
      {audit.data?.next_cursor ? (
        <Button
          type="button"
          variant="outline"
          onClick={() => setCursor(audit.data?.next_cursor ?? null)}
        >
          {t.adminOperations.audit.older}
        </Button>
      ) : null}
    </main>
  );
}
