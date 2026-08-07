import { describe, expect, test } from "@rstest/core";

import { parseAdminAuditFilters } from "@/components/admin/operations/admin-audit";
import { parseAdminJobFilters } from "@/components/admin/operations/admin-jobs";
import { adminAuditQueryKey } from "@/core/admin-operations/query-keys";
import {
  ADMIN_AUDIT_PLATFORM_FILTER,
  ADMIN_OPERATIONS_PAGE_SIZES,
  DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE,
  adminAuditItemSchema,
  auditFiltersSchema,
  jobFiltersSchema,
} from "@/core/admin-operations/types";

const PROJECT_ID = "22222222-2222-4222-8222-222222222222";

describe("admin audit filters", () => {
  test("parses project id and platform-only selection", () => {
    expect(parseAdminAuditFilters({ projectId: PROJECT_ID })).toEqual({
      project_id: PROJECT_ID,
    });
    expect(
      parseAdminAuditFilters({ projectId: ADMIN_AUDIT_PLATFORM_FILTER }),
    ).toEqual({
      platform_only: true,
    });
    expect(parseAdminAuditFilters({ projectId: "" })).toEqual({});
    expect(parseAdminAuditFilters({ projectId: "not-a-uuid" })).toBeNull();
    expect(auditFiltersSchema.parse({})).toEqual({});
    expect(() =>
      auditFiltersSchema.parse({
        project_id: PROJECT_ID,
        platform_only: true,
      }),
    ).toThrow();
  });

  test("pins filters and page size in the query key", () => {
    expect(DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE).toBe(20);
    expect(ADMIN_OPERATIONS_PAGE_SIZES).toContain(20);
    expect(
      adminAuditQueryKey(
        "11111111-1111-4111-8111-111111111111",
        null,
        { platform_only: true },
        10,
      ).slice(-2),
    ).toEqual([{ platform_only: true }, 10]);
  });

  test("requires project fields together", () => {
    const base = {
      id: "11111111-1111-4111-8111-111111111111",
      occurred_at: "2026-08-07T00:00:00Z",
      actor: "gateway" as const,
      action: "project.created" as const,
      target_kind: "project" as const,
      outcome: "success" as const,
      public_error_code: null,
      metadata: {},
    };
    expect(
      adminAuditItemSchema.parse({
        ...base,
        actor_user_id: null,
        actor_email: null,
        project_id: null,
        project_slug: null,
        project_display_name: null,
      }).project_id,
    ).toBeNull();
    expect(
      adminAuditItemSchema.parse({
        ...base,
        actor: "system_admin",
        actor_user_id: "11111111-1111-4111-8111-111111111111",
        actor_email: "admin@example.com",
        project_id: null,
        project_slug: null,
        project_display_name: null,
      }).actor_email,
    ).toBe("admin@example.com");
    expect(() =>
      adminAuditItemSchema.parse({
        ...base,
        actor_user_id: null,
        actor_email: null,
        project_id: PROJECT_ID,
        project_slug: null,
        project_display_name: "Alpha",
      }),
    ).toThrow();
  });
});

describe("admin job filters", () => {
  test("parses project id selection with other filters", () => {
    expect(
      parseAdminJobFilters({
        projectId: PROJECT_ID,
        status: "succeeded",
        type: "private_run",
      }),
    ).toEqual({
      project_id: PROJECT_ID,
      status: "succeeded",
      type: "private_run",
    });
    expect(
      jobFiltersSchema.parse({
        project_id: PROJECT_ID,
      }).project_id,
    ).toBe(PROJECT_ID);
  });
});
