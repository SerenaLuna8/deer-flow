import {
  accountIdSchema,
  adminJobPageSizeSchema,
  adminOperationsPageSizeSchema,
  adminProjectsPageSizeSchema,
  auditFiltersSchema,
  DEFAULT_ADMIN_JOB_PAGE_SIZE,
  DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE,
  jobFiltersSchema,
  projectFiltersSchema,
  type AdminAuditFilters,
  type AdminJobFilters,
  type AdminJobPageSize,
  type AdminOperationsPageSize,
  type AdminProjectFilters,
} from "./types";

export function adminOperationsRoot(accountId: string) {
  return [
    "account",
    accountIdSchema.parse(accountId),
    "admin",
    "operations",
  ] as const;
}

export function operationsOverviewQueryKey(accountId: string) {
  return [...adminOperationsRoot(accountId), "overview"] as const;
}

export function adminProjectsQueryKey(
  accountId: string,
  cursor: string | null = null,
  filters: AdminProjectFilters = {},
  limit = 50,
) {
  return [
    ...adminOperationsRoot(accountId),
    "projects",
    cursor,
    projectFiltersSchema.parse(filters),
    adminProjectsPageSizeSchema.parse(limit),
  ] as const;
}

export function adminJobsQueryKey(
  accountId: string,
  cursor: string | null = null,
  filters: AdminJobFilters = {},
  limit: AdminJobPageSize = DEFAULT_ADMIN_JOB_PAGE_SIZE,
) {
  return [
    ...adminOperationsRoot(accountId),
    "jobs",
    cursor,
    jobFiltersSchema.parse(filters),
    adminJobPageSizeSchema.parse(limit),
  ] as const;
}

export function adminAuditQueryKey(
  accountId: string,
  cursor: string | null = null,
  filters: AdminAuditFilters = {},
  limit: AdminOperationsPageSize = DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE,
) {
  return [
    ...adminOperationsRoot(accountId),
    "audit",
    cursor,
    auditFiltersSchema.parse(filters),
    adminOperationsPageSizeSchema.parse(limit),
  ] as const;
}

export function safeRequeueMutationKey(accountId: string) {
  return [
    ...adminOperationsRoot(accountId),
    "jobs",
    "mutation",
    "safe-requeue",
  ] as const;
}

export function adminProjectLifecycleMutationKey(accountId: string) {
  return [
    ...adminOperationsRoot(accountId),
    "projects",
    "mutation",
    "lifecycle",
  ] as const;
}
