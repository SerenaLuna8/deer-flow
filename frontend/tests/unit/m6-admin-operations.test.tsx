import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { headers } from "next/headers";
import {
  createElement,
  type ComponentType,
  type PropsWithChildren,
} from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  notFound: rs.fn(() => {
    throw Object.assign(new Error("Not found"), { code: "NEXT_NOT_FOUND" });
  }),
  redirect: rs.fn((destination: string) => {
    throw Object.assign(new Error("Redirect"), {
      code: "NEXT_REDIRECT",
      destination,
    });
  }),
  usePathname: () => "/admin/operations",
  useRouter: () => ({ push: rs.fn() }),
}));
rs.mock("next/headers", () => ({ headers: rs.fn() }));
rs.mock("@/core/auth/server", () => ({ getServerSideUser: rs.fn() }));
rs.mock("@/core/static-mode", () => ({ isStaticWebsiteOnly: () => false }));
rs.mock("@/core/api/fetcher", () => ({
  AuthRequiredError: class AuthRequiredError extends Error {},
  fetch: rs.fn(),
}));

import AdminLayout from "@/app/admin/layout";
import AdminRootPage from "@/app/admin/page";
import {
  AdminAuditStateView,
  type AdminAuditState,
} from "@/components/admin/operations/admin-audit";
import {
  AdminJobsStateView,
  parseAdminJobFilters,
  type AdminJobsState,
} from "@/components/admin/operations/admin-jobs";
import {
  AdminOperationsNavigation,
  AdminOperationsShell,
} from "@/components/admin/operations/admin-operations-shell";
import {
  parseAdminProjectFilters,
  AdminProjectsStateView,
  type AdminProjectsState,
} from "@/components/admin/operations/admin-projects";
import {
  OperationsOverview,
  OperationsOverviewStateView,
  type OperationsOverviewState,
} from "@/components/admin/operations/operations-overview";
import * as adminOperationsApi from "@/core/admin-operations/api";
import {
  adminAuditQueryOptions,
  adminJobsQueryOptions,
  adminProjectLifecycleMutationOptions,
  adminProjectsQueryOptions,
  operationsOverviewQueryOptions,
  safeRequeueMutationOptions,
} from "@/core/admin-operations/api";
import {
  adminAuditQueryKey,
  adminJobsQueryKey,
  adminOperationsRoot,
  adminProjectLifecycleMutationKey,
  adminProjectsQueryKey,
  operationsOverviewQueryKey,
  safeRequeueMutationKey,
} from "@/core/admin-operations/query-keys";
import {
  adminAuditPageSchema,
  adminJobPageSchema,
  adminProjectPageSchema,
  operationsOverviewSchema,
  operationsServerErrorSchema,
  type AdminAuditPage,
  type AdminJobPage,
  type AdminProjectPage,
  type OperationsOverviewData,
} from "@/core/admin-operations/types";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { transitionAccountQueries } from "@/core/auth/account-query-client";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { I18nProvider } from "@/core/i18n/context";

const ACCOUNT_A = "11111111-1111-4111-8111-111111111111";
const ACCOUNT_B = "22222222-2222-4222-8222-222222222222";
const PROJECT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const JOB_A = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const DEAD_JOB_A = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";

const overview: OperationsOverviewData = {
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
    projects: 2,
    suspended_projects: 0,
    queued_jobs: 1,
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
};

const projects: AdminProjectPage = {
  items: [
    {
      project_id: PROJECT_A,
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
};

const jobs: AdminJobPage = {
  items: [
    {
      job_id: JOB_A,
      dead_job_id: DEAD_JOB_A,
      project_id: PROJECT_A,
      job_type: "retention_purge",
      status: "dead",
      retry_safety: "safe",
      safe_to_requeue: true,
      public_error_code: "PURGE_FAILED",
      predecessor_dead_job_id: null,
    },
  ],
  next_cursor: null,
};

const audit: AdminAuditPage = {
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
};

function renderWithProviders(
  children: React.ReactNode,
  initialUser: Parameters<typeof AuthProvider>[0]["initialUser"] = {
    id: ACCOUNT_A,
    email: "admin@example.com",
    system_role: "system_admin",
    needs_setup: false,
    oauth_provider: null,
  },
) {
  const queryClient = new QueryClient();
  const TestAuthProvider = AuthProvider as ComponentType<
    PropsWithChildren<{
      initialUser: Parameters<typeof AuthProvider>[0]["initialUser"];
    }>
  >;
  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <TestAuthProvider initialUser={initialUser}>
        <I18nProvider initialLocale="en-US">{children}</I18nProvider>
      </TestAuthProvider>
    </QueryClientProvider>,
  );
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("M6 system operations console", () => {
  test("strictly rejects the retired reliability cutover error", () => {
    expect(
      operationsServerErrorSchema.safeParse({
        code: "RELIABILITY_CUTOVER",
        message: "retired server state",
        request_id: "req-retired",
      }).success,
    ).toBe(false);
  });

  test("keeps every query and mutation key under the exact authenticated account", () => {
    expect(adminOperationsRoot(ACCOUNT_A)).toEqual([
      "account",
      ACCOUNT_A,
      "admin",
      "operations",
    ]);
    expect(operationsOverviewQueryKey(ACCOUNT_A)).toEqual([
      ...adminOperationsRoot(ACCOUNT_A),
      "overview",
    ]);
    expect(
      adminProjectsQueryKey(ACCOUNT_A, "cursor-1", {
        query: "alpha",
        status: "active",
        suspended: false,
      }),
    ).toEqual([
      ...adminOperationsRoot(ACCOUNT_A),
      "projects",
      "cursor-1",
      { query: "alpha", status: "active", suspended: false },
    ]);
    expect(
      adminJobsQueryKey(ACCOUNT_A, "cursor-2", {
        project_id: PROJECT_A,
        status: "dead",
        type: "retention_purge",
      }),
    ).toEqual([
      ...adminOperationsRoot(ACCOUNT_A),
      "jobs",
      "cursor-2",
      {
        project_id: PROJECT_A,
        status: "dead",
        type: "retention_purge",
      },
    ]);
    expect(adminAuditQueryKey(ACCOUNT_A, "cursor-3")).toEqual([
      ...adminOperationsRoot(ACCOUNT_A),
      "audit",
      "cursor-3",
    ]);
    expect(safeRequeueMutationKey(ACCOUNT_A)).toEqual([
      ...adminOperationsRoot(ACCOUNT_A),
      "jobs",
      "mutation",
      "safe-requeue",
    ]);
    expect(adminProjectLifecycleMutationKey(ACCOUNT_A)).toEqual([
      ...adminOperationsRoot(ACCOUNT_A),
      "projects",
      "mutation",
      "lifecycle",
    ]);
    expect(adminOperationsRoot(ACCOUNT_A)).not.toEqual(
      adminOperationsRoot(ACCOUNT_B),
    );
  });

  test("accepts only the canonical auth-disabled account alias", () => {
    expect(adminOperationsRoot("default")).toEqual([
      "account",
      "default",
      "admin",
      "operations",
    ]);
    expect(() => adminOperationsRoot("not-a-uuid")).toThrow();
  });

  test("normalizes public job filters and rejects malformed project coordinates", () => {
    expect(
      parseAdminJobFilters({
        projectId: ` ${PROJECT_A} `,
        status: "dead",
        type: "retention_purge",
      }),
    ).toEqual({
      project_id: PROJECT_A,
      status: "dead",
      type: "retention_purge",
    });
    expect(
      parseAdminJobFilters({
        projectId: "not-a-project",
        status: "",
        type: "",
      }),
    ).toBeNull();
  });

  test("normalizes public project filters without leaking private coordinates", () => {
    expect(
      parseAdminProjectFilters({
        query: "  Alpha Project  ",
        status: "active",
        suspension: "running",
      }),
    ).toEqual({
      query: "Alpha Project",
      status: "active",
      suspended: false,
    });
    expect(
      parseAdminProjectFilters({
        query: "x".repeat(121),
        status: "",
        suspension: "",
      }),
    ).toBeNull();
  });

  test("rejects every private or unknown response field", () => {
    expect(operationsOverviewSchema.parse(overview)).toEqual(overview);
    expect(adminProjectPageSchema.parse(projects)).toEqual(projects);
    expect(adminJobPageSchema.parse(jobs)).toEqual(jobs);
    expect(adminAuditPageSchema.parse(audit)).toEqual(audit);

    expect(() =>
      operationsOverviewSchema.parse({ ...overview, owner_user_id: ACCOUNT_A }),
    ).toThrow();
    expect(() =>
      adminProjectPageSchema.parse({
        ...projects,
        items: [{ ...projects.items[0], description: "must-not-enter-cache" }],
      }),
    ).toThrow();
    for (const privateField of [
      "owner_user_id",
      "run_id",
      "automation_occurrence_id",
      "thread_id",
      "idempotency_key",
      "lease_token_hash",
      "payload",
      "exception",
    ]) {
      expect(() =>
        adminJobPageSchema.parse({
          ...jobs,
          items: [
            {
              ...jobs.items[0],
              [privateField]: "must-not-enter-cache",
            },
          ],
        }),
      ).toThrow();
    }
    expect(() =>
      adminAuditPageSchema.parse({
        ...audit,
        items: [
          {
            ...audit.items[0],
            metadata: { ...audit.items[0]!.metadata, owner_user_id: ACCOUNT_A },
          },
        ],
      }),
    ).toThrow();
  });

  test("accepts closed readiness only with an explicit unavailable aggregate state", () => {
    const closedOverview = {
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
    };

    expect(operationsOverviewSchema.parse(closedOverview)).toEqual(
      closedOverview,
    );
    for (const retiredSchemaState of ["migration_required", "unknown"]) {
      expect(() =>
        operationsOverviewSchema.parse({
          ...closedOverview,
          readiness: {
            ...closedOverview.readiness,
            schema_state: retiredSchemaState,
          },
        }),
      ).toThrow();
    }
    expect(() =>
      operationsOverviewSchema.parse({
        ...closedOverview,
        readiness: { ...closedOverview.readiness, cutover: "ready" },
      }),
    ).toThrow();
    expect(() =>
      operationsOverviewSchema.parse({
        ...closedOverview,
        counts: overview.counts,
        usage: overview.usage,
      }),
    ).toThrow();
  });

  test("accepts a formerly safe retention job after the server records its successor", () => {
    expect(
      adminJobPageSchema.parse({
        items: [{ ...jobs.items[0], safe_to_requeue: false }],
        next_cursor: null,
      }),
    ).toEqual({
      items: [{ ...jobs.items[0], safe_to_requeue: false }],
      next_cursor: null,
    });
  });

  test("executes real account-scoped query options with abortable requests", async () => {
    rs.mocked(fetchWithAuth).mockResolvedValueOnce(response(overview));
    const queryClient = new QueryClient();
    await expect(
      queryClient.fetchQuery(operationsOverviewQueryOptions(ACCOUNT_A)),
    ).resolves.toEqual(overview);
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.stringContaining("/api/admin/operations"),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );

    expect(adminProjectsQueryOptions(ACCOUNT_A).queryKey).toEqual(
      adminProjectsQueryKey(ACCOUNT_A, null, {}),
    );
    expect(adminJobsQueryOptions(ACCOUNT_A).queryKey).toEqual(
      adminJobsQueryKey(ACCOUNT_A, null, {}),
    );
    expect(adminAuditQueryOptions(ACCOUNT_A).queryKey).toEqual(
      adminAuditQueryKey(ACCOUNT_A, null),
    );
    expect(adminProjectLifecycleMutationOptions(ACCOUNT_A).mutationKey).toEqual(
      adminProjectLifecycleMutationKey(ACCOUNT_A),
    );
  });

  test("account transition aborts an in-flight operations query before clearing it", async () => {
    const queryClient = new QueryClient();
    let started!: () => void;
    const ready = new Promise<void>((resolve) => {
      started = resolve;
    });
    let aborted = false;
    rs.mocked(fetchWithAuth).mockImplementationOnce(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            aborted = true;
            reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
          });
          started();
        }),
    );
    const pending = queryClient
      .fetchQuery(operationsOverviewQueryOptions(ACCOUNT_A))
      .catch(() => undefined);
    await ready;

    await transitionAccountQueries(queryClient, ACCOUNT_A, ACCOUNT_B);
    await pending;

    expect(aborted).toBe(true);
    expect(
      queryClient.getQueryData(operationsOverviewQueryKey(ACCOUNT_A)),
    ).toBeUndefined();
  });

  test("account transition aborts and removes a real safe-requeue mutation", async () => {
    const queryClient = new QueryClient();
    let started!: () => void;
    const ready = new Promise<void>((resolve) => {
      started = resolve;
    });
    let aborted = false;
    rs.mocked(fetchWithAuth).mockImplementationOnce(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            aborted = true;
            reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
          });
          started();
        }),
    );
    const mutation = queryClient
      .getMutationCache()
      .build(queryClient, safeRequeueMutationOptions(ACCOUNT_A));
    const pending = mutation
      .execute({
        project_id: PROJECT_A,
        dead_job_id: JOB_A,
        idempotency_key: "a".repeat(64),
        max_attempts: 3,
      })
      .catch(() => undefined);
    await ready;

    await transitionAccountQueries(queryClient, ACCOUNT_A, ACCOUNT_B);
    await pending;

    expect(aborted).toBe(true);
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
  });

  test("server layout gates ordinary users and mounts the platform shell for system admins", async () => {
    rs.mocked(getServerSideUser).mockResolvedValueOnce({
      tag: "authenticated",
      user: {
        id: ACCOUNT_B,
        email: "member@example.com",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      },
    });
    await expect(
      AdminLayout({ children: createElement("p", null, "restricted") }),
    ).rejects.toMatchObject({ code: "NEXT_NOT_FOUND" });

    rs.mocked(getServerSideUser).mockResolvedValueOnce({
      tag: "authenticated",
      user: {
        id: ACCOUNT_A,
        email: "admin@example.com",
        system_role: "system_admin",
        needs_setup: false,
        oauth_provider: null,
      },
    });
    const rendered = await AdminLayout({
      children: createElement("p", null, "authorized"),
    });
    const queryProvider = rendered as React.ReactElement<{
      children: React.ReactNode;
    }>;
    const authProvider = queryProvider.props.children as React.ReactElement<{
      children: React.ReactNode;
    }>;
    const shell = authProvider.props.children as React.ReactElement;
    expect(shell.type).toBe(AdminOperationsShell);

    rs.mocked(getServerSideUser).mockResolvedValueOnce({
      tag: "unauthenticated",
    });
    rs.mocked(headers).mockResolvedValueOnce(new Headers() as never);
    await expect(
      AdminLayout({ children: createElement("p", null, "restricted") }),
    ).rejects.toMatchObject({
      code: "NEXT_REDIRECT",
      destination: "/login?next=%2Fadmin%2Foperations",
    });
  });

  test("redirects the admin root to the operations overview", () => {
    expect(() => AdminRootPage()).toThrow(
      expect.objectContaining({
        code: "NEXT_REDIRECT",
        destination: "/admin/operations",
      }),
    );
  });

  test("does not construct an operations query before the client identity gate", () => {
    const hook = rs.spyOn(adminOperationsApi, "useOperationsOverview");
    expect(() =>
      renderWithProviders(<OperationsOverview />, null),
    ).not.toThrow();
    expect(hook).not.toHaveBeenCalled();
    hook.mockRestore();
  });

  test("renders compact navigation and loading, empty, error, and public data states", () => {
    const navigationLabels = {
      label: "Platform operations navigation",
      overview: "Overview",
      projects: "Projects",
      jobs: "Jobs",
      audit: "Audit",
      assets: "Assets",
    };
    const navigation = renderToStaticMarkup(
      <AdminOperationsNavigation
        pathname="/admin/operations"
        labels={navigationLabels as never}
      />,
    );
    expect(navigation).toContain('aria-label="Platform operations navigation"');
    expect(navigation).not.toContain("overflow-x-auto");
    for (const [href, label] of [
      ["/admin/operations", "Overview"],
      ["/admin/projects", "Projects"],
      ["/admin/jobs", "Jobs"],
      ["/admin/audit", "Audit"],
      ["/admin/assets", "Assets"],
    ]) {
      expect(navigation).toContain(`href="${href}"`);
      expect(navigation).toContain(label);
    }

    const states: React.ReactNode[] = [
      <OperationsOverviewStateView
        key="overview-loading"
        state={{ status: "loading" } satisfies OperationsOverviewState}
      />,
      <OperationsOverviewStateView
        key="overview-ready"
        state={
          { status: "ready", data: overview } satisfies OperationsOverviewState
        }
      />,
      <AdminProjectsStateView
        key="projects-ready"
        state={
          {
            status: "ready",
            data: projects,
          } satisfies AdminProjectsState
        }
      />,
      <AdminJobsStateView
        key="jobs-error"
        state={{ status: "error" } satisfies AdminJobsState}
      />,
      <AdminJobsStateView
        key="jobs-ready"
        state={{ status: "ready", data: jobs } satisfies AdminJobsState}
      />,
      <AdminAuditStateView
        key="audit-empty"
        state={
          {
            status: "ready",
            data: { items: [], next_cursor: null },
          } satisfies AdminAuditState
        }
      />,
      <AdminAuditStateView
        key="audit-ready"
        state={{ status: "ready", data: audit } satisfies AdminAuditState}
      />,
    ];
    const html = renderWithProviders(<>{states}</>);
    expect(html).toContain("Loading platform operations");
    expect(html).toContain("Readiness");
    expect(html).toContain("Degraded");
    expect(html).toContain("Worker fleet");
    expect(html).toContain("Worker processes");
    expect(html).toContain("Worker capacity");
    expect(html).toContain("Scheduler ownership");
    expect(html).toContain("Channel providers");
    expect(html).toContain("Alpha Project");
    expect(html).toContain(`href="/admin/projects/${PROJECT_A}/assets/agents"`);
    expect(html).toContain("治理共享资产");
    expect(html).toContain("Governance details");
    expect(html).toContain("Operations data is unavailable");
    expect(html).toContain("PURGE_FAILED");
    expect(html).toContain("No audit events found");
    expect(html).toContain("Job requeued");
    expect(html).not.toContain("job.requeued");
    expect(html).not.toContain("owner_user_id");
  });

  test("disables the clicked dead-job coordinate while its requeue mutation is pending", () => {
    const props = {
      state: { status: "ready", data: jobs } satisfies AdminJobsState,
      onRequeue: rs.fn(),
      requeueingCoordinate: {
        projectId: PROJECT_A,
        deadJobId: DEAD_JOB_A,
      },
    } satisfies Parameters<typeof AdminJobsStateView>[0];
    const html = renderWithProviders(<AdminJobsStateView {...props} />);

    expect(html).toContain("disabled");
    expect(html).toContain("Requeueing");
  });
});
