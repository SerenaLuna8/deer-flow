"use client";

import { ShieldCheckIcon } from "lucide-react";
import { useState } from "react";

import {
  AdminCursorPagination,
  AdminPage,
  AdminPageHeader,
  INITIAL_ADMIN_CURSOR_STATE,
  advanceAdminCursor,
  retreatAdminCursor,
} from "@/components/admin/ui/admin-page";
import { describeAuditItem } from "@/components/projects/governance/project-audit-view-model";
import { useAdminAudit } from "@/core/admin-operations/api";
import type { AdminAuditPage } from "@/core/admin-operations/types";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";

import {
  AdminEmptyState,
  AdminErrorState,
  AdminLoadingState,
  AdminRecordList,
  AdminStatus,
  AdminTechnicalValue,
} from "./admin-operations-ui";

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
  const { locale, t } = useI18n();
  const labels = t.adminOperations.audit;
  const localLabels = t.adminOperations.ui;
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
  if (state.data.items.length === 0) {
    return (
      <AdminEmptyState
        title={labels.emptyTitle}
        description={labels.emptyDescription}
      />
    );
  }
  return (
    <AdminRecordList data-slot="admin-audit-feed">
      {state.data.items.map((item) => {
        const detail = describeAuditItem(item, locale);
        return (
          <li key={item.id}>
            <article className="grid grid-cols-[2rem_minmax(0,1fr)] gap-x-3 gap-y-2 px-4 py-4 sm:grid-cols-[2rem_minmax(0,1fr)_auto] sm:px-5">
              <span
                aria-hidden
                className="border-border bg-muted text-muted-foreground flex size-8 items-center justify-center rounded-full border"
              >
                <ShieldCheckIcon className="size-4" />
              </span>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-semibold">{detail.action}</h2>
                  <AdminStatus status={item.outcome}>
                    {detail.outcome}
                  </AdminStatus>
                </div>
                <p className="text-muted-foreground mt-1 text-sm">
                  {detail.actor} · {detail.target}
                </p>
                <div className="mt-2">
                  <span className="text-muted-foreground mr-2 text-xs">
                    {localLabels.eventId}
                  </span>
                  <AdminTechnicalValue
                    compact
                    value={item.id}
                    copyLabel={localLabels.copy}
                    copiedLabel={localLabels.copied}
                  />
                </div>
                {detail.publicErrorCode ? (
                  <dl className="border-destructive/20 bg-destructive/5 mt-3 w-fit rounded-md border px-3 py-2">
                    <dt className="text-muted-foreground text-xs">
                      {localLabels.publicErrorCode}
                    </dt>
                    <dd className="text-destructive mt-1 font-mono text-xs font-semibold">
                      {detail.publicErrorCode}
                    </dd>
                  </dl>
                ) : null}
                {detail.metadata.length > 0 ? (
                  <dl
                    data-slot="admin-audit-metadata"
                    className="border-border/70 mt-3 flex flex-wrap gap-x-5 gap-y-2 border-t pt-3 text-xs"
                  >
                    {detail.metadata.map((entry) => (
                      <div
                        key={entry.label}
                        className="flex min-w-0 items-baseline gap-1.5"
                      >
                        <dt className="text-muted-foreground shrink-0">
                          {entry.label}
                        </dt>
                        <dd className="min-w-0 font-medium break-words">
                          {entry.value}
                        </dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
              </div>
              <time
                className="text-muted-foreground col-start-2 text-xs whitespace-nowrap sm:col-start-auto"
                dateTime={item.occurred_at}
              >
                {detail.occurredAt}
              </time>
            </article>
          </li>
        );
      })}
    </AdminRecordList>
  );
}

export function AdminAudit() {
  const { user } = useAuth();
  if (user?.system_role !== "system_admin") return null;
  return <AuthorizedAdminAudit accountId={user.id} />;
}

function AuthorizedAdminAudit({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const localLabels = t.adminOperations.ui;
  const [pager, setPager] = useState(INITIAL_ADMIN_CURSOR_STATE);
  const audit = useAdminAudit(accountId, pager.cursor);
  const state: AdminAuditState = audit.isLoading
    ? { status: "loading" }
    : audit.error || !audit.data
      ? { status: "error" }
      : { status: "ready", data: audit.data };
  return (
    <AdminPage>
      <AdminPageHeader
        title={t.adminOperations.audit.title}
        description={t.adminOperations.audit.description}
      />
      <AdminAuditStateView state={state} onRetry={() => void audit.refetch()} />
      <AdminCursorPagination
        state={pager}
        nextCursor={audit.data?.next_cursor ?? null}
        busy={audit.isFetching}
        previousLabel={localLabels.previousPage}
        nextLabel={t.adminOperations.audit.older}
        pageLabel={localLabels.page}
        onPrevious={() => setPager((current) => retreatAdminCursor(current))}
        onNext={() =>
          setPager((current) =>
            advanceAdminCursor(current, audit.data?.next_cursor ?? null),
          )
        }
      />
    </AdminPage>
  );
}
