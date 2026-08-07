"use client";

import { useState, type ReactNode } from "react";

import {
  AdminCursorPagination,
  AdminPage,
  AdminPageHeader,
  AdminSection,
  INITIAL_ADMIN_CURSOR_STATE,
  advanceAdminCursor,
  retreatAdminCursor,
} from "@/components/admin/ui/admin-page";
import { describeAuditItem } from "@/components/projects/governance/project-audit-view-model";
import { Button } from "@/components/ui/button";
import { useAdminAudit } from "@/core/admin-operations/api";
import {
  ADMIN_AUDIT_PLATFORM_FILTER,
  ADMIN_OPERATIONS_PAGE_SIZES,
  DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE,
  adminOperationsPageSizeSchema,
  auditFiltersSchema,
  type AdminAuditFilters,
  type AdminAuditPage,
  type AdminOperationsPageSize,
} from "@/core/admin-operations/types";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";

import {
  AdminDataTable,
  AdminEmptyState,
  AdminErrorState,
  AdminLoadingState,
  AdminMobileRecordList,
  AdminProjectFilterSelect,
  AdminStatus,
} from "./admin-operations-ui";

export type AdminAuditState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: AdminAuditPage };

export function parseAdminAuditFilters(input: {
  projectId: string;
}): AdminAuditFilters | null {
  if (input.projectId === ADMIN_AUDIT_PLATFORM_FILTER) {
    return auditFiltersSchema.safeParse({ platform_only: true }).success
      ? { platform_only: true }
      : null;
  }
  const result = auditFiltersSchema.safeParse({
    project_id: input.projectId || undefined,
  });
  return result.success ? result.data : null;
}

function AdminAuditActorCell({
  email,
  roleLabel,
}: {
  email: string | null;
  roleLabel: string;
}) {
  const label = email ?? roleLabel;
  return (
    <p className="truncate text-sm" title={label}>
      {label}
    </p>
  );
}

export function AdminAuditStateView({
  emptyAction,
  state,
  onRetry,
}: {
  emptyAction?: ReactNode;
  state: AdminAuditState;
  onRetry?: () => void;
}) {
  const { locale, t } = useI18n();
  const labels = t.adminOperations.audit;
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
        action={emptyAction}
      />
    );
  }
  return (
    <div className="space-y-3">
      <AdminDataTable
        aria-label={labels.title}
        className="min-w-[72rem] table-fixed"
        containerClassName="hidden xl:block"
      >
        <thead className="bg-muted/45 text-muted-foreground">
          <tr className="border-border/70 border-b">
            <th className="w-[14%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.time}
            </th>
            <th className="w-[18%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.action}
            </th>
            <th className="w-[10%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.outcome}
            </th>
            <th className="w-[18%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.actor}
            </th>
            <th className="w-[12%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.target}
            </th>
            <th className="w-[16%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.project}
            </th>
            <th className="w-[12%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.error}
            </th>
          </tr>
        </thead>
        <tbody className="divide-border/70 divide-y">
          {state.data.items.map((item) => {
            const detail = describeAuditItem(item, locale);
            return (
              <tr
                key={item.id}
                className="hover:bg-muted/25 align-middle transition-colors"
              >
                <td className="px-3 py-2.5">
                  <time
                    className="text-muted-foreground text-xs whitespace-nowrap"
                    dateTime={item.occurred_at}
                  >
                    {detail.occurredAt}
                  </time>
                </td>
                <td className="px-3 py-2.5">
                  <p className="text-sm font-medium">{detail.action}</p>
                </td>
                <td className="px-3 py-2.5">
                  <AdminStatus status={item.outcome}>
                    {detail.outcome}
                  </AdminStatus>
                </td>
                <td className="px-3 py-2.5">
                  <AdminAuditActorCell
                    email={item.actor_email}
                    roleLabel={detail.actor}
                  />
                </td>
                <td className="px-3 py-2.5">
                  <p className="truncate text-sm">{detail.target}</p>
                </td>
                <td className="px-3 py-2.5">
                  {item.project_display_name && item.project_slug ? (
                    <div className="min-w-0">
                      <p
                        className="truncate text-sm font-medium"
                        title={item.project_display_name}
                      >
                        {item.project_display_name}
                      </p>
                      <p
                        className="text-muted-foreground mt-0.5 truncate font-mono text-xs"
                        title={item.project_slug}
                      >
                        {item.project_slug}
                      </p>
                    </div>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
                <td className="px-3 py-2.5">
                  {item.public_error_code ? (
                    <p
                      className="text-destructive truncate font-mono text-xs font-semibold"
                      title={item.public_error_code}
                    >
                      {item.public_error_code}
                    </p>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </AdminDataTable>
      <AdminMobileRecordList aria-label={labels.title}>
        {state.data.items.map((item) => {
          const detail = describeAuditItem(item, locale);
          return (
            <li key={item.id}>
              <article className="space-y-3 p-4">
                <header className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h2 className="text-sm font-semibold">{detail.action}</h2>
                    <time
                      className="text-muted-foreground mt-1 block text-xs"
                      dateTime={item.occurred_at}
                    >
                      {detail.occurredAt}
                    </time>
                  </div>
                  <AdminStatus status={item.outcome}>
                    {detail.outcome}
                  </AdminStatus>
                </header>
                <dl className="grid gap-2 text-xs">
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.columns.actor}
                    </dt>
                    <dd className="mt-0.5">
                      <AdminAuditActorCell
                        email={item.actor_email}
                        roleLabel={detail.actor}
                      />
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.columns.target}
                    </dt>
                    <dd className="mt-0.5 text-sm">{detail.target}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.columns.project}
                    </dt>
                    <dd className="mt-0.5">
                      {item.project_display_name && item.project_slug ? (
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium">
                            {item.project_display_name}
                          </p>
                          <p className="text-muted-foreground mt-0.5 truncate font-mono text-xs">
                            {item.project_slug}
                          </p>
                        </div>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.columns.error}
                    </dt>
                    <dd className="mt-0.5 font-mono font-semibold">
                      {item.public_error_code ? (
                        <span className="text-destructive">
                          {item.public_error_code}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </dd>
                  </div>
                </dl>
              </article>
            </li>
          );
        })}
      </AdminMobileRecordList>
    </div>
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
  const [filters, setFilters] = useState<AdminAuditFilters>({});
  const [pageSize, setPageSize] = useState<AdminOperationsPageSize>(
    DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE,
  );
  const [projectId, setProjectId] = useState("");
  const audit = useAdminAudit(accountId, pager.cursor, filters, pageSize);
  const state: AdminAuditState = audit.isLoading
    ? { status: "loading" }
    : audit.error || !audit.data
      ? { status: "error" }
      : { status: "ready", data: audit.data };
  const resetFilters = () => {
    setProjectId("");
    setPager(INITIAL_ADMIN_CURSOR_STATE);
    setPageSize(DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE);
    setFilters({});
  };
  const applyPageSize = (next: number) => {
    const parsed = adminOperationsPageSizeSchema.safeParse(next);
    if (!parsed.success || parsed.data === pageSize) return;
    setPager(INITIAL_ADMIN_CURSOR_STATE);
    setPageSize(parsed.data);
  };
  const hasFilters = Object.keys(filters).length > 0;
  const canResetFilters =
    hasFilters ||
    pageSize !== DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE ||
    projectId.length > 0;
  return (
    <AdminPage>
      <AdminPageHeader title={t.adminOperations.audit.title} />
      <AdminSection contentClassName="p-3">
        <form
          aria-label={t.adminOperations.audit.filters.label}
          className="grid gap-2 sm:grid-cols-[minmax(12rem,20rem)_auto] sm:items-center"
          onSubmit={(event) => {
            event.preventDefault();
            const parsed = parseAdminAuditFilters({ projectId });
            if (!parsed) return;
            setPager(INITIAL_ADMIN_CURSOR_STATE);
            setFilters(parsed);
          }}
        >
          <AdminProjectFilterSelect
            accountId={accountId}
            value={projectId}
            onChange={setProjectId}
            label={t.adminOperations.audit.filters.project}
            allLabel={t.adminOperations.audit.filters.allProjects}
            platformLabel={t.adminOperations.audit.filters.platformOnly}
            platformValue={ADMIN_AUDIT_PLATFORM_FILTER}
          />
          <div className="flex flex-wrap gap-1">
            <Button type="submit" size="sm">
              {t.adminOperations.audit.filters.apply}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={!canResetFilters}
              onClick={resetFilters}
            >
              {t.adminOperations.audit.filters.clear}
            </Button>
          </div>
        </form>
      </AdminSection>
      <AdminAuditStateView
        state={state}
        onRetry={() => void audit.refetch()}
        emptyAction={
          hasFilters ? (
            <Button type="button" variant="outline" onClick={resetFilters}>
              {localLabels.clearFilters}
            </Button>
          ) : undefined
        }
      />
      {state.status === "ready" ? (
        <AdminCursorPagination
          alwaysVisible
          state={pager}
          nextCursor={audit.data?.next_cursor ?? null}
          busy={audit.isFetching}
          itemCount={audit.data?.items.length ?? 0}
          itemCountLabel={localLabels.itemsOnPage}
          pageSize={pageSize}
          pageSizeOptions={ADMIN_OPERATIONS_PAGE_SIZES}
          pageSizeLabel={localLabels.pageSize}
          pageSizeOptionLabel={localLabels.pageSizeOption}
          previousLabel={localLabels.previousPage}
          nextLabel={localLabels.nextPage}
          pageLabel={localLabels.page}
          onPageSizeChange={applyPageSize}
          onPrevious={() => setPager((current) => retreatAdminCursor(current))}
          onNext={() =>
            setPager((current) =>
              advanceAdminCursor(current, audit.data?.next_cursor ?? null),
            )
          }
        />
      ) : null}
    </AdminPage>
  );
}
