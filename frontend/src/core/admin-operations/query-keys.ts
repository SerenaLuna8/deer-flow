import {
  accountIdSchema,
  jobFiltersSchema,
  projectFiltersSchema,
  type AdminJobFilters,
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
) {
  return [
    ...adminOperationsRoot(accountId),
    "projects",
    cursor,
    projectFiltersSchema.parse(filters),
  ] as const;
}

export function adminJobsQueryKey(
  accountId: string,
  cursor: string | null = null,
  filters: AdminJobFilters = {},
) {
  return [
    ...adminOperationsRoot(accountId),
    "jobs",
    cursor,
    jobFiltersSchema.parse(filters),
  ] as const;
}

export function adminAuditQueryKey(
  accountId: string,
  cursor: string | null = null,
) {
  return [...adminOperationsRoot(accountId), "audit", cursor] as const;
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
