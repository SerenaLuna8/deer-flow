"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  adminAuditQueryKey,
  adminProjectLifecycleMutationKey,
  adminJobsQueryKey,
  adminOperationsRoot,
  adminProjectsQueryKey,
  operationsOverviewQueryKey,
  safeRequeueMutationKey,
} from "./query-keys";
import {
  accountIdSchema,
  adminAuditPageSchema,
  adminJobPageSchema,
  adminProjectSchema,
  adminProjectPageSchema,
  jobFiltersSchema,
  operationsOverviewSchema,
  operationsServerErrorSchema,
  projectFiltersSchema,
  safeRequeueInputSchema,
  safeRequeueResponseSchema,
  type AdminAuditPage,
  type AdminJobFilters,
  type AdminJobPage,
  type AdminProjectFilters,
  type AdminProjectPage,
  type OperationsOverviewData,
  type SafeRequeueInput,
  type SafeRequeueResponse,
} from "./types";

type ServerErrorCode = z.infer<typeof operationsServerErrorSchema>["code"];

const SAFE_MESSAGES: Record<ServerErrorCode, string> = {
  INVALID_STREAM_CURSOR: "Operations cursor is invalid.",
  RELIABILITY_NOT_FOUND: "Operations data was not found.",
  RELIABILITY_CONFLICT: "Operations data changed. Refresh and retry.",
  RELIABILITY_INVALID: "Operations request is invalid.",
  DATABASE_UNAVAILABLE: "Operations data is temporarily unavailable.",
};

export class AdminOperationsApiError extends Error {
  readonly status: number;
  readonly code:
    | ServerErrorCode
    | "AUTH_REQUIRED"
    | "NETWORK_ERROR"
    | "INVALID_RESPONSE";

  constructor(
    status: number,
    code: AdminOperationsApiError["code"],
    message: string,
  ) {
    super(message);
    this.name = "AdminOperationsApiError";
    this.status = status;
    this.code = code;
  }
}

const mutationControllers = new Map<string, Set<AbortController>>();

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function operationsBaseURL(): string {
  return `${getBackendBaseURL()}/api/admin`;
}

async function requestOperations(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (isAbortError(error) || error instanceof AdminOperationsApiError) {
      throw error;
    }
    if (error instanceof AuthRequiredError) {
      throw new AdminOperationsApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new AdminOperationsApiError(
      0,
      "NETWORK_ERROR",
      "Operations data is temporarily unavailable.",
    );
  }
}

async function readOperationsResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
): Promise<T> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new AdminOperationsApiError(
      response.status,
      "INVALID_RESPONSE",
      "Operations response was invalid.",
    );
  }
  if (!response.ok) {
    const parsed = operationsServerErrorSchema.safeParse(body);
    if (!parsed.success) {
      throw new AdminOperationsApiError(
        response.status,
        "INVALID_RESPONSE",
        "Operations request failed.",
      );
    }
    throw new AdminOperationsApiError(
      response.status,
      parsed.data.code,
      SAFE_MESSAGES[parsed.data.code],
    );
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new AdminOperationsApiError(
      response.status,
      "INVALID_RESPONSE",
      "Operations response was invalid.",
    );
  }
  return parsed.data;
}

function withCursor(params: URLSearchParams, cursor: string | null): void {
  if (cursor) params.set("cursor", cursor);
}

export async function fetchOperationsOverview(
  _accountId: string,
  signal?: AbortSignal,
): Promise<OperationsOverviewData> {
  accountIdSchema.parse(_accountId);
  const response = await requestOperations(
    `${operationsBaseURL()}/operations`,
    {
      signal,
    },
  );
  return readOperationsResponse(response, operationsOverviewSchema);
}

export async function fetchAdminProjects(
  accountId: string,
  cursor: string | null = null,
  filters: AdminProjectFilters = {},
  signal?: AbortSignal,
): Promise<AdminProjectPage> {
  accountIdSchema.parse(accountId);
  const parsedFilters = projectFiltersSchema.parse(filters);
  const params = new URLSearchParams({ limit: "50" });
  withCursor(params, cursor);
  if (parsedFilters.query) params.set("query", parsedFilters.query);
  if (parsedFilters.status) params.set("status", parsedFilters.status);
  if (parsedFilters.suspended !== undefined) {
    params.set("suspended", String(parsedFilters.suspended));
  }
  const response = await requestOperations(
    `${operationsBaseURL()}/projects?${params.toString()}`,
    { signal },
  );
  return readOperationsResponse(response, adminProjectPageSchema);
}

export async function changeAdminProjectLifecycle(
  accountId: string,
  projectId: string,
  action: "suspend" | "resume",
  signal?: AbortSignal,
): Promise<AdminProjectPage["items"][number]> {
  accountIdSchema.parse(accountId);
  const parsedProjectId = z.string().uuid().parse(projectId);
  const response = await requestOperations(
    `${operationsBaseURL()}/projects/${parsedProjectId}/${action}`,
    {
      method: "POST",
      signal,
    },
  );
  return readOperationsResponse(response, adminProjectSchema);
}

export async function fetchAdminJobs(
  accountId: string,
  cursor: string | null = null,
  filters: AdminJobFilters = {},
  signal?: AbortSignal,
): Promise<AdminJobPage> {
  accountIdSchema.parse(accountId);
  const parsedFilters = jobFiltersSchema.parse(filters);
  const params = new URLSearchParams({ limit: "50" });
  withCursor(params, cursor);
  if (parsedFilters.project_id) {
    params.set("project_id", parsedFilters.project_id);
  }
  if (parsedFilters.status) params.set("status", parsedFilters.status);
  if (parsedFilters.type) params.set("type", parsedFilters.type);
  const response = await requestOperations(
    `${operationsBaseURL()}/jobs?${params.toString()}`,
    { signal },
  );
  return readOperationsResponse(response, adminJobPageSchema);
}

export async function fetchAdminAudit(
  accountId: string,
  cursor: string | null = null,
  signal?: AbortSignal,
): Promise<AdminAuditPage> {
  accountIdSchema.parse(accountId);
  const params = new URLSearchParams({ limit: "50" });
  withCursor(params, cursor);
  const response = await requestOperations(
    `${operationsBaseURL()}/audit?${params.toString()}`,
    { signal },
  );
  return readOperationsResponse(response, adminAuditPageSchema);
}

export async function requeueSafeJob(
  accountId: string,
  input: SafeRequeueInput,
  signal?: AbortSignal,
): Promise<SafeRequeueResponse> {
  accountIdSchema.parse(accountId);
  const body = safeRequeueInputSchema.parse(input);
  const response = await requestOperations(
    `${operationsBaseURL()}/jobs/requeue`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return readOperationsResponse(response, safeRequeueResponseSchema);
}

async function runAbortableMutation<T>(
  accountId: string,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const parsed = accountIdSchema.parse(accountId);
  const controller = new AbortController();
  const controllers = mutationControllers.get(parsed) ?? new Set();
  controllers.add(controller);
  mutationControllers.set(parsed, controllers);
  try {
    return await operation(controller.signal);
  } finally {
    controllers.delete(controller);
    if (controllers.size === 0) mutationControllers.delete(parsed);
  }
}

export function abortAdminOperationsAccount(accountId: string): void {
  const parsed = accountIdSchema.safeParse(accountId);
  if (!parsed.success) return;
  mutationControllers
    .get(parsed.data)
    ?.forEach((controller) => controller.abort());
  mutationControllers.delete(parsed.data);
}

export function operationsOverviewQueryOptions(accountId: string) {
  const parsed = accountIdSchema.parse(accountId);
  return {
    queryKey: operationsOverviewQueryKey(parsed),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchOperationsOverview(parsed, signal),
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function adminProjectsQueryOptions(
  accountId: string,
  cursor: string | null = null,
  filters: AdminProjectFilters = {},
) {
  const parsed = accountIdSchema.parse(accountId);
  const parsedFilters = projectFiltersSchema.parse(filters);
  return {
    queryKey: adminProjectsQueryKey(parsed, cursor, parsedFilters),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchAdminProjects(parsed, cursor, parsedFilters, signal),
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function adminJobsQueryOptions(
  accountId: string,
  cursor: string | null = null,
  filters: AdminJobFilters = {},
) {
  const parsed = accountIdSchema.parse(accountId);
  const parsedFilters = jobFiltersSchema.parse(filters);
  return {
    queryKey: adminJobsQueryKey(parsed, cursor, parsedFilters),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchAdminJobs(parsed, cursor, parsedFilters, signal),
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function adminAuditQueryOptions(
  accountId: string,
  cursor: string | null = null,
) {
  const parsed = accountIdSchema.parse(accountId);
  return {
    queryKey: adminAuditQueryKey(parsed, cursor),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchAdminAudit(parsed, cursor, signal),
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function safeRequeueMutationOptions(accountId: string) {
  const parsed = accountIdSchema.parse(accountId);
  return {
    mutationKey: safeRequeueMutationKey(parsed),
    mutationFn: (input: SafeRequeueInput) =>
      runAbortableMutation(parsed, (signal) =>
        requeueSafeJob(parsed, input, signal),
      ),
  };
}

export function adminProjectLifecycleMutationOptions(accountId: string) {
  const parsed = accountIdSchema.parse(accountId);
  return {
    mutationKey: adminProjectLifecycleMutationKey(parsed),
    mutationFn: ({
      projectId,
      action,
    }: {
      projectId: string;
      action: "suspend" | "resume";
    }) =>
      runAbortableMutation(parsed, (signal) =>
        changeAdminProjectLifecycle(parsed, projectId, action, signal),
      ),
  };
}

export function useOperationsOverview(accountId: string) {
  return useQuery(operationsOverviewQueryOptions(accountId));
}

export function useAdminProjects(
  accountId: string,
  cursor: string | null = null,
  filters: AdminProjectFilters = {},
) {
  return useQuery(adminProjectsQueryOptions(accountId, cursor, filters));
}

export function useAdminProjectLifecycle(accountId: string) {
  const parsed = accountIdSchema.parse(accountId);
  const queryClient = useQueryClient();
  return useMutation({
    ...adminProjectLifecycleMutationOptions(parsed),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: adminOperationsRoot(parsed),
      });
    },
  });
}

export function useAdminJobs(
  accountId: string,
  cursor: string | null = null,
  filters: AdminJobFilters = {},
) {
  return useQuery(adminJobsQueryOptions(accountId, cursor, filters));
}

export function useAdminAudit(accountId: string, cursor: string | null = null) {
  return useQuery(adminAuditQueryOptions(accountId, cursor));
}

export function useSafeRequeue(accountId: string) {
  const parsed = accountIdSchema.parse(accountId);
  const queryClient = useQueryClient();
  return useMutation({
    ...safeRequeueMutationOptions(parsed),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: adminOperationsRoot(parsed),
      });
    },
  });
}
