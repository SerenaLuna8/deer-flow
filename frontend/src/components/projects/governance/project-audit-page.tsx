"use client";

import { notFound } from "next/navigation";
import { useState } from "react";
import { z } from "zod";

import {
  AdminDataTable,
  AdminMobileRecordList,
  AdminStatus,
} from "@/components/admin/operations/admin-operations-ui";
import {
  AdminCursorPagination,
  INITIAL_ADMIN_CURSOR_STATE,
  advanceAdminCursor,
  retreatAdminCursor,
} from "@/components/admin/ui/admin-page";
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

const PROJECT_AUDIT_PAGE_SIZES = [10, 20, 50, 100] as const;
const DEFAULT_PROJECT_AUDIT_PAGE_SIZE = 20;
const projectAuditPageSizeSchema = z.union([
  z.literal(10),
  z.literal(20),
  z.literal(50),
  z.literal(100),
]);
type ProjectAuditPageSize = z.infer<typeof projectAuditPageSizeSchema>;

export type ProjectAuditState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; data: ProjectAuditPageData };

function formatMetadataSummary(
  metadata: Array<{ label: string; value: string }>,
): string {
  if (metadata.length === 0) return "—";
  return metadata.map((entry) => `${entry.label}: ${entry.value}`).join(" · ");
}

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
    <div className="space-y-3">
      <AdminDataTable
        aria-label={labels.title}
        className="min-w-[56rem] table-fixed"
        containerClassName="hidden overflow-x-auto md:block"
      >
        <thead className="bg-muted/45 text-muted-foreground sticky top-0 z-10">
          <tr className="border-border/70 border-b">
            <th className="bg-muted/45 w-[16%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.time}
            </th>
            <th className="bg-muted/45 w-[20%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.action}
            </th>
            <th className="bg-muted/45 w-[12%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.outcome}
            </th>
            <th className="bg-muted/45 w-[12%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.actor}
            </th>
            <th className="bg-muted/45 w-[12%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.target}
            </th>
            <th className="bg-muted/45 w-[18%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.details}
            </th>
            <th className="bg-muted/45 w-[10%] px-3 py-2.5 text-xs font-medium">
              {labels.columns.error}
            </th>
          </tr>
        </thead>
        <tbody className="divide-border/70 divide-y">
          {state.data.items.map((item) => {
            const detail = describeAuditItem(item, locale);
            const detailsSummary = formatMetadataSummary(detail.metadata);
            return (
              <tr
                key={item.id}
                data-action={item.action}
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
                  <p className="text-sm">{detail.actor}</p>
                </td>
                <td className="px-3 py-2.5">
                  <p className="truncate text-sm">{detail.target}</p>
                </td>
                <td className="px-3 py-2.5">
                  <p
                    className="text-muted-foreground truncate text-xs"
                    title={detailsSummary === "—" ? undefined : detailsSummary}
                  >
                    {detailsSummary}
                  </p>
                </td>
                <td className="px-3 py-2.5">
                  {detail.publicErrorCode ? (
                    <p
                      className="text-destructive truncate font-mono text-xs font-semibold"
                      title={detail.publicErrorCode}
                    >
                      {detail.publicErrorCode}
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

      <AdminMobileRecordList aria-label={labels.title} className="md:hidden">
        {state.data.items.map((item) => {
          const detail = describeAuditItem(item, locale);
          const detailsSummary = formatMetadataSummary(detail.metadata);
          return (
            <li key={item.id} data-action={item.action}>
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
                    <dd className="mt-0.5 text-sm">{detail.actor}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.columns.target}
                    </dt>
                    <dd className="mt-0.5 text-sm">{detail.target}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.columns.details}
                    </dt>
                    <dd className="mt-0.5 text-sm break-words">
                      {detailsSummary}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">
                      {labels.columns.error}
                    </dt>
                    <dd className="mt-0.5 font-mono font-semibold">
                      {detail.publicErrorCode ? (
                        <span className="text-destructive">
                          {detail.publicErrorCode}
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

export function ProjectAuditPage() {
  const project = useCurrentProject();
  const { t } = useI18n();
  const canRead = project.capabilities.includes("project.audit.read");
  const staticMode = isStaticWebsiteOnly();
  const access = usePrivateWorkAccess();

  if (staticMode) notFound();
  if (!canRead) {
    return (
      <ProjectAccessDenied
        projectSlug={project.slug}
        area={t.project.governance.audit.title}
      />
    );
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
  const labels = t.project.governance.audit;
  const [pager, setPager] = useState(INITIAL_ADMIN_CURSOR_STATE);
  const [pageSize, setPageSize] = useState<ProjectAuditPageSize>(
    DEFAULT_PROJECT_AUDIT_PAGE_SIZE,
  );
  const audit = useProjectAudit(scope, pager.cursor, pageSize);
  const state: ProjectAuditState = audit.isLoading
    ? { status: "loading" }
    : audit.error || !audit.data
      ? { status: "error" }
      : { status: "ready", data: audit.data };

  const applyPageSize = (next: number) => {
    const parsed = projectAuditPageSizeSchema.safeParse(next);
    if (!parsed.success || parsed.data === pageSize) return;
    setPager(INITIAL_ADMIN_CURSOR_STATE);
    setPageSize(parsed.data);
  };

  return (
    <div className="space-y-3">
      <ProjectAuditStateView
        state={state}
        onRetry={() => void audit.refetch()}
      />
      {state.status === "ready" ? (
        <AdminCursorPagination
          alwaysVisible
          state={pager}
          nextCursor={audit.data?.next_cursor ?? null}
          busy={audit.isFetching}
          itemCount={audit.data?.items.length ?? 0}
          itemCountLabel={labels.itemsOnPage}
          pageSize={pageSize}
          pageSizeOptions={PROJECT_AUDIT_PAGE_SIZES}
          pageSizeLabel={labels.pageSize}
          pageSizeOptionLabel={labels.pageSizeOption}
          previousLabel={labels.previousPage}
          nextLabel={labels.nextPage}
          pageLabel={labels.page}
          onPageSizeChange={applyPageSize}
          onPrevious={() => setPager((current) => retreatAdminCursor(current))}
          onNext={() =>
            setPager((current) =>
              advanceAdminCursor(current, audit.data?.next_cursor ?? null),
            )
          }
        />
      ) : null}
    </div>
  );
}
