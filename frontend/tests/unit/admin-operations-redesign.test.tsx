import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AdminAuditStateView,
  type AdminAuditState,
} from "@/components/admin/operations/admin-audit";
import {
  AdminJobsStateView,
  type AdminJobsState,
} from "@/components/admin/operations/admin-jobs";
import {
  ADMIN_NAVIGATION_EXPANDED_STORAGE_KEY,
  AdminOperationsNavigation,
  adminDesktopSidebarLayout,
  parseAdminNavigationExpanded,
} from "@/components/admin/operations/admin-operations-shell";
import {
  AdminInlineAlert,
  AdminStatus,
  AdminTechnicalValue,
  adminStatusTone,
} from "@/components/admin/operations/admin-operations-ui";
import {
  AdminProjectsStateView,
  type AdminProjectsState,
} from "@/components/admin/operations/admin-projects";
import {
  OperationsOverviewStateView,
  type OperationsOverviewState,
} from "@/components/admin/operations/operations-overview";
import {
  AdminCursorPagination,
  advanceAdminCursor,
  retreatAdminCursor,
} from "@/components/admin/ui/admin-page";
import { I18nProvider } from "@/core/i18n/context";

const PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

function render(view: React.ReactNode, locale: "en-US" | "zh-CN" = "en-US") {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>{view}</I18nProvider>,
  );
}

describe("admin operations visual system", () => {
  test("uses one dense metric and readiness language for the overview", () => {
    const html = render(
      <OperationsOverviewStateView
        state={
          {
            status: "ready",
            data: {
              readiness: {
                status: "degraded",
                database: "ready",
                schema: "ready",
                schema_state: "ready",
                worker_fleet: "unavailable",
                scheduler: "disabled",
                stream: "polling",
                quota: "ready",
                audit: "ready",
                role: "gateway",
                worker_count: 0,
                worker_capacity: 0,
                worker_oldest_heartbeat_age_seconds: null,
                scheduler_ownership: "disabled",
              },
              data_status: "available",
              counts: {
                projects: 1,
                suspended_projects: 0,
                queued_jobs: 2,
                running_jobs: 0,
                dead_jobs: 1,
              },
              usage: [
                { dimension: "members", used: 2, reserved: 0 },
                { dimension: "storage_bytes", used: 1024, reserved: 0 },
                { dimension: "concurrent_runs", used: 0, reserved: 1 },
                { dimension: "mcp_calls_daily", used: 3, reserved: 0 },
              ],
              channel_providers: [],
            },
          } satisfies OperationsOverviewState
        }
      />,
    );

    expect(html).toContain('data-slot="admin-metric-grid"');
    expect(html).toContain('data-slot="admin-readiness-grid"');
    expect(html).toContain('data-slot="admin-status"');
    expect(html).toContain('data-status="degraded"');
    expect(html).toContain('data-status="unavailable"');
    expect(html.match(/sm:col-span-2/g) ?? []).toHaveLength(2);
    expect(html.match(/xl:col-span-1/g) ?? []).toHaveLength(2);
    expect(html).toContain('aria-label="Aggregate usage"');
    expect(html.match(/Channel providers/g) ?? []).toHaveLength(1);
    expect(html).toContain("No provider health reports");

    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/operations/operations-overview.tsx",
      ),
      "utf8",
    );
    expect(source).toContain('data-slot="admin-channel-grid"');
    expect(source).toContain("sm:grid-cols-2 lg:grid-cols-3");
    expect(source).toContain("lg:col-span-2");
    expect(source).toContain("lg:col-span-3");
    expect(source).not.toContain(
      "xl:grid-cols-[minmax(0,1.4fr)_minmax(20rem,0.6fr)]",
    );
  });

  test("uses consistent loading, empty, and dense table surfaces", () => {
    const loading = render(
      <OperationsOverviewStateView
        state={{ status: "loading" } satisfies OperationsOverviewState}
      />,
    );
    const empty = render(
      <AdminAuditStateView
        state={
          {
            status: "ready",
            data: { items: [], next_cursor: null },
          } satisfies AdminAuditState
        }
      />,
    );
    const projects = render(
      <AdminProjectsStateView
        state={
          {
            status: "ready",
            data: {
              items: [
                {
                  project_id: PROJECT_ID,
                  slug: "alpha-project",
                  display_name: "Alpha Project",
                  status: "active",
                  is_suspended: false,
                  state_version: 1,
                  created_at: "2026-07-17T05:00:00Z",
                  updated_at: "2026-07-17T05:30:00Z",
                  deletion_effective_at: null,
                },
              ],
              next_cursor: null,
            },
          } satisfies AdminProjectsState
        }
      />,
    );

    expect(loading).toContain('data-slot="admin-loading-state"');
    expect(empty).toContain('data-slot="admin-empty-state"');
    expect(projects).toContain('data-slot="admin-data-table"');
    expect(projects).toContain('data-slot="admin-mobile-record-list"');
    expect(projects).toContain("hidden xl:block");
    expect(projects).toContain("xl:hidden");
    expect(projects).toContain("<table");
    expect(projects).toContain("<thead");
    expect(projects).toContain("Project ID");
    expect(projects).toContain("Updated");
    expect(projects).toContain('data-status="active"');
    expect(projects).toContain("<details");
  });

  test("keeps job recovery and audit evidence visible in compact rows", () => {
    const jobs = render(
      <AdminJobsStateView
        onRequeue={() => undefined}
        state={
          {
            status: "ready",
            data: {
              items: [
                {
                  job_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                  dead_job_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                  project_id: PROJECT_ID,
                  job_type: "retention_purge",
                  status: "dead",
                  retry_safety: "safe",
                  safe_to_requeue: true,
                  public_error_code: "PURGE_FAILED",
                  predecessor_dead_job_id: null,
                },
              ],
              next_cursor: null,
            },
          } satisfies AdminJobsState
        }
      />,
    );
    const audit = render(
      <AdminAuditStateView
        state={
          {
            status: "ready",
            data: {
              items: [
                {
                  id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                  occurred_at: "2026-07-17T05:31:00Z",
                  actor: "system_admin",
                  action: "job.requeued",
                  target_kind: "job",
                  outcome: "success",
                  public_error_code: null,
                  metadata: {
                    job_type: "retention_purge",
                    attempt_count: 0,
                    retry_safety: "safe",
                  },
                },
              ],
              next_cursor: null,
            },
          } satisfies AdminAuditState
        }
      />,
    );

    expect(jobs).toContain('data-slot="admin-data-table"');
    expect(jobs).toContain('data-slot="admin-mobile-record-list"');
    expect(jobs).toContain("hidden xl:block");
    expect(jobs).toContain("xl:hidden");
    expect(jobs).toContain("<table");
    expect(jobs).toContain("<thead");
    expect(jobs).toContain("Job ID");
    expect(jobs).toContain("Project ID");
    expect(jobs).toContain('data-status="dead"');
    expect(jobs).toContain("Requeue safe job");
    expect(jobs).toContain("PURGE_FAILED");
    expect(audit).toContain('data-slot="admin-audit-feed"');
    expect(audit).toContain('data-slot="admin-audit-metadata"');
    expect(audit).toContain('data-status="success"');
    expect(audit).toContain("Job requeued");
  });

  test("keeps model navigation and operational values locale-consistent", () => {
    const navigation = renderToStaticMarkup(
      <AdminOperationsNavigation
        pathname="/admin/settings/models"
        labels={{
          label: "平台运维导航",
          overview: "概览",
          projects: "项目",
          jobs: "任务",
          audit: "审计",
          assets: "资产",
          systemSettings: "系统配置",
          settings: "模型设置",
        }}
      />,
    );
    const jobs = render(
      <AdminJobsStateView
        state={
          {
            status: "ready",
            data: {
              items: [
                {
                  job_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                  dead_job_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                  project_id: PROJECT_ID,
                  job_type: "retention_purge",
                  status: "dead",
                  retry_safety: "safe",
                  safe_to_requeue: true,
                  public_error_code: null,
                  predecessor_dead_job_id: null,
                },
              ],
              next_cursor: null,
            },
          } satisfies AdminJobsState
        }
      />,
      "zh-CN",
    );
    const projects = render(
      <AdminProjectsStateView
        state={
          {
            status: "ready",
            data: {
              items: [
                {
                  project_id: PROJECT_ID,
                  slug: "alpha-project",
                  display_name: "Alpha Project",
                  status: "active",
                  is_suspended: false,
                  state_version: 1,
                  created_at: "2026-07-17T05:00:00Z",
                  updated_at: "2026-07-17T05:30:00Z",
                  deletion_effective_at: null,
                },
              ],
              next_cursor: null,
            },
          } satisfies AdminProjectsState
        }
      />,
      "en-US",
    );

    expect(navigation).toContain('href="/admin/settings/system"');
    expect(navigation).toContain('href="/admin/settings/models"');
    expect(navigation).toContain('aria-current="page"');
    expect(navigation).toContain('data-navigation-heading="operations"');
    expect(navigation).toContain('data-navigation-heading="governance"');
    expect(navigation).toContain("模型设置");
    expect(navigation).toContain("系统配置");
    expect(jobs).toContain("保留期清理");
    expect(jobs).toContain("已终止");
    expect(jobs).toContain("可安全重试");
    expect(jobs).not.toContain(">retention_purge<");
    expect(jobs).not.toContain(">dead<");
    expect(jobs).not.toContain(">safe<");
    expect(projects).toContain("Govern shared assets");
    expect(projects).not.toContain("治理共享资产");
  });

  test("uses accessible tooltips for every collapsed desktop navigation item", () => {
    const navigation = renderToStaticMarkup(
      <AdminOperationsNavigation compact pathname="/admin/operations" />,
    );

    expect(navigation).toContain('data-slot="tooltip-trigger"');
    expect(navigation).toContain('aria-label="Overview"');
    expect(navigation).toContain('aria-label="Projects"');
    expect(navigation).toContain('aria-label="Jobs"');
    expect(navigation).toContain('aria-label="Audit"');
    expect(navigation).toContain('aria-label="Assets"');
    expect(navigation).toContain('aria-label="System settings"');
    expect(navigation).toContain('aria-label="Model settings"');
    expect(navigation).toContain("overflow-x-hidden");
    expect(navigation).not.toContain("before:left-[-0.75rem]");
  });

  test("keeps a deterministic collapsed server layout and parses only explicit persisted expansion", () => {
    expect(ADMIN_NAVIGATION_EXPANDED_STORAGE_KEY).toBe(
      "deer-flow:admin-navigation-expanded",
    );
    expect(parseAdminNavigationExpanded(null)).toBe(false);
    expect(parseAdminNavigationExpanded("false")).toBe(false);
    expect(parseAdminNavigationExpanded("true")).toBe(true);
    expect(parseAdminNavigationExpanded("unexpected")).toBe(false);
    expect(adminDesktopSidebarLayout(false)).toEqual({
      contentPadding: "lg:pl-16",
      railWidth: "w-16",
    });
    expect(adminDesktopSidebarLayout(true)).toEqual({
      contentPadding: "lg:pl-60",
      railWidth: "w-60",
    });
  });

  test("tracks a reversible cursor history instead of replacing the current page", () => {
    const initial = { cursor: null, history: [] };
    const secondPage = advanceAdminCursor(initial, "cursor-2");
    const thirdPage = advanceAdminCursor(secondPage, "cursor-3");

    expect(secondPage).toEqual({
      cursor: "cursor-2",
      history: [null],
    });
    expect(thirdPage).toEqual({
      cursor: "cursor-3",
      history: [null, "cursor-2"],
    });
    expect(retreatAdminCursor(thirdPage)).toEqual(secondPage);
    expect(retreatAdminCursor(initial)).toEqual(initial);

    const html = renderToStaticMarkup(
      <AdminCursorPagination
        state={thirdPage}
        nextCursor="cursor-4"
        previousLabel="Newer"
        nextLabel="Older"
        pageLabel={(page) => `Page ${page}`}
        onPrevious={() => undefined}
        onNext={() => undefined}
      />,
    );
    expect(html).toContain('data-slot="admin-cursor-pagination"');
    expect(html).toContain("Newer");
    expect(html).toContain("Page 3");
    expect(html).toContain("Older");
  });

  test("keeps dangerous and suspended states semantically distinct", () => {
    expect(adminStatusTone("unsafe")).toBe("danger");
    expect(adminStatusTone("rejected")).toBe("danger");
    expect(adminStatusTone("suspended")).toBe("warning");
    expect(adminStatusTone("cancelled")).toBe("neutral");

    const html = renderToStaticMarkup(
      <AdminStatus status="unsafe">Unsafe to retry</AdminStatus>,
    );
    expect(html).toContain('data-tone="danger"');
    expect(html).toContain("Unsafe to retry");
  });

  test("keeps unavailable operations recoverable", () => {
    const html = render(
      <OperationsOverviewStateView
        onRetry={() => undefined}
        state={{
          status: "ready",
          data: {
            readiness: {
              status: "closed",
              database: "ready",
              schema: "unavailable",
              schema_state: "unavailable",
              worker_fleet: "closed",
              scheduler: "closed",
              stream: "closed",
              quota: "closed",
              audit: "closed",
              role: "gateway",
              worker_count: 0,
              worker_capacity: 0,
              worker_oldest_heartbeat_age_seconds: null,
              scheduler_ownership: "disabled",
            },
            data_status: "unavailable",
            counts: null,
            usage: null,
            channel_providers: [],
          },
        }}
      />,
    );

    expect(html).toContain('data-slot="admin-error-state"');
    expect(html).toContain(">Retry<");
  });

  test("renders the safe public audit error code and complete technical values", () => {
    const audit = render(
      <AdminAuditStateView
        state={{
          status: "ready",
          data: {
            items: [
              {
                id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                occurred_at: "2026-07-17T05:31:00Z",
                actor: "worker",
                action: "job.dead",
                target_kind: "job",
                outcome: "failed",
                public_error_code: "PURGE_FAILED",
                metadata: {
                  job_type: "retention_purge",
                  public_error_code: "PURGE_FAILED",
                  attempt_count: 3,
                  retry_safety: "unsafe",
                },
              },
            ],
            next_cursor: null,
          },
        }}
      />,
    );
    const technicalValue = renderToStaticMarkup(
      <AdminTechnicalValue
        value="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        copyLabel="Copy"
        copiedLabel="Copied"
      />,
    );
    const alert = renderToStaticMarkup(
      <AdminInlineAlert id="filter-error">Invalid filter</AdminInlineAlert>,
    );

    expect(audit).toContain("PURGE_FAILED");
    expect(technicalValue).toContain('data-slot="admin-technical-value"');
    expect(technicalValue).toContain("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
    expect(technicalValue).toContain('aria-label="Copy"');
    expect(alert).toContain('id="filter-error"');
  });
});
