import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ProjectAuditStateView } from "@/components/projects/governance/project-audit-page";
import { ProjectUsageStateView } from "@/components/projects/governance/project-usage-page";
import { projectNavigationItems } from "@/components/projects/project-nav";
import {
  auditPageSchema,
  projectAuditQueryKey,
  type ProjectAuditPage,
} from "@/core/project-governance/audit";
import {
  projectUsageQueryKey,
  usageResponseSchema,
  type ProjectUsage,
} from "@/core/project-governance/usage";
import type { Project } from "@/core/projects/types";

const ACCOUNT_A = "11111111-1111-4111-8111-111111111111";
const ACCOUNT_B = "22222222-2222-4222-8222-222222222222";
const PROJECT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PROJECT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const adminProject: Project = {
  id: PROJECT_A,
  slug: "alpha",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: ["project.read", "project.usage.read", "project.audit.read"],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

const usage: ProjectUsage = {
  policy: {
    version: 0,
    configured: {
      member_limit: null,
      storage_bytes_limit: null,
      concurrent_run_limit: null,
      mcp_calls_daily_limit: null,
    },
    effective: {
      member_limit: 20,
      storage_bytes_limit: 5_368_709_120,
      concurrent_run_limit: 3,
      mcp_calls_daily_limit: 10_000,
    },
  },
  dimensions: [
    {
      dimension: "members",
      bucket: "lifetime",
      used: 0,
      reserved: 2,
      limit: 20,
      warning_threshold_reached: false,
    },
  ],
};

const auditPage: ProjectAuditPage = {
  items: [
    {
      id: "33333333-3333-4333-8333-333333333333",
      occurred_at: "2026-07-17T10:00:00Z",
      actor: "user",
      action: "quota.policy_updated",
      target_kind: "quota",
      outcome: "success",
      public_error_code: null,
      metadata: {
        member_limit: 10,
        storage_bytes_limit: null,
        concurrent_run_limit: 2,
        mcp_calls_daily_limit: null,
        version: 1,
      },
    },
  ],
  next_cursor: null,
};

describe("M6 project governance", () => {
  test("keeps every query key under its exact account and project prefix", () => {
    expect(
      projectUsageQueryKey({ accountId: ACCOUNT_A, projectId: PROJECT_A }),
    ).toEqual([
      "account",
      ACCOUNT_A,
      "project",
      PROJECT_A,
      "governance",
      "usage",
    ]);
    expect(
      projectAuditQueryKey(
        { accountId: ACCOUNT_B, projectId: PROJECT_B },
        "cursor-2",
        25,
      ),
    ).toEqual([
      "account",
      ACCOUNT_B,
      "project",
      PROJECT_B,
      "governance",
      "audit",
      "cursor-2",
      25,
    ]);
    expect(
      projectUsageQueryKey({ accountId: ACCOUNT_A, projectId: PROJECT_A }),
    ).not.toEqual(
      projectUsageQueryKey({ accountId: ACCOUNT_A, projectId: PROJECT_B }),
    );
  });

  test("accepts only strict public usage and audit response fields", () => {
    expect(usageResponseSchema.parse(usage)).toEqual(usage);
    expect(auditPageSchema.parse(auditPage)).toEqual(auditPage);
    expect(() =>
      usageResponseSchema.parse({ ...usage, owner_user_id: ACCOUNT_A }),
    ).toThrow();
    expect(() =>
      auditPageSchema.parse({
        ...auditPage,
        items: [
          {
            ...auditPage.items[0],
            target_ref_hmac: "secret-digest",
          },
        ],
      }),
    ).toThrow();
  });

  test("gates each navigation link by exact capability, M6 readiness, and static mode", () => {
    const ready = projectNavigationItems(
      adminProject,
      false,
      false,
      false,
      false,
      false,
      true,
      true,
    );
    expect(ready).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Usage" }),
        expect.objectContaining({ label: "Audit" }),
      ]),
    );
    expect(
      projectNavigationItems(
        { ...adminProject, capabilities: ["project.read"] },
        false,
        false,
        false,
        false,
        false,
        true,
        true,
      ),
    ).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Usage" }),
        expect.objectContaining({ label: "Audit" }),
      ]),
    );
    expect(
      projectNavigationItems(
        adminProject,
        false,
        false,
        false,
        false,
        false,
        false,
        false,
      ),
    ).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Usage" }),
        expect.objectContaining({ label: "Audit" }),
      ]),
    );
    expect(
      projectNavigationItems(
        adminProject,
        false,
        false,
        false,
        false,
        true,
        true,
        true,
      ),
    ).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ label: "Usage" }),
        expect.objectContaining({ label: "Audit" }),
      ]),
    );
  });

  test("renders loading, empty, error, and public data states without secrets", () => {
    expect(
      renderToStaticMarkup(
        <ProjectUsageStateView state={{ status: "loading" }} />,
      ),
    ).toContain("Loading usage");
    expect(
      renderToStaticMarkup(
        <ProjectUsageStateView
          state={{ status: "error" }}
          onRetry={() => undefined}
        />,
      ),
    ).toContain("Usage is unavailable");
    const usageHtml = renderToStaticMarkup(
      <ProjectUsageStateView state={{ status: "ready", data: usage }} />,
    );
    expect(usageHtml).toContain("Members");
    expect(usageHtml).not.toContain("owner_user_id");

    expect(
      renderToStaticMarkup(
        <ProjectAuditStateView state={{ status: "loading" }} />,
      ),
    ).toContain("Loading audit");
    expect(
      renderToStaticMarkup(
        <ProjectAuditStateView
          state={{ status: "ready", data: { items: [], next_cursor: null } }}
        />,
      ),
    ).toContain("No audit events");
    expect(
      renderToStaticMarkup(
        <ProjectAuditStateView
          state={{ status: "error" }}
          onRetry={() => undefined}
        />,
      ),
    ).toContain("Audit is unavailable");
    const auditHtml = renderToStaticMarkup(
      <ProjectAuditStateView state={{ status: "ready", data: auditPage }} />,
    );
    expect(auditHtml).toContain("quota.policy_updated");
    expect(auditHtml).not.toMatch(/target_ref|owner_user_id|secret-digest/u);
  });

  test("keeps both direct pages behind the non-static route gate", () => {
    for (const route of ["usage", "audit"]) {
      const source = readFileSync(
        resolve(
          process.cwd(),
          `src/app/projects/[project_slug]/settings/${route}/page.tsx`,
        ),
        "utf8",
      );
      expect(source).toContain("if (isStaticWebsiteOnly()) notFound();");
    }
  });
});
