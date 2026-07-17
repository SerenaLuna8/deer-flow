"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  projectClientScopeSchema,
  runPrivateWorkAbortable,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "@/core/private-work/types";

const quotaLimitsSchema = z
  .object({
    member_limit: z.number().int().nonnegative().nullable(),
    storage_bytes_limit: z.number().int().nonnegative().nullable(),
    concurrent_run_limit: z.number().int().nonnegative().nullable(),
    mcp_calls_daily_limit: z.number().int().nonnegative().nullable(),
  })
  .strict();

const effectiveQuotaLimitsSchema = z
  .object({
    member_limit: z.number().int().nonnegative(),
    storage_bytes_limit: z.number().int().nonnegative(),
    concurrent_run_limit: z.number().int().nonnegative(),
    mcp_calls_daily_limit: z.number().int().nonnegative(),
  })
  .strict();

export const quotaPolicySchema = z
  .object({
    version: z.number().int().nonnegative(),
    configured: quotaLimitsSchema,
    effective: effectiveQuotaLimitsSchema,
  })
  .strict();

const quotaDimensionSchema = z
  .object({
    dimension: z.enum([
      "members",
      "storage_bytes",
      "concurrent_runs",
      "mcp_calls_daily",
    ]),
    bucket: z.string().min(1).max(32),
    used: z.number().int().nonnegative(),
    reserved: z.number().int().nonnegative(),
    limit: z.number().int().nonnegative(),
    warning_threshold_reached: z.boolean(),
  })
  .strict();

export const usageResponseSchema = z
  .object({
    policy: quotaPolicySchema,
    dimensions: z.array(quotaDimensionSchema).max(4),
  })
  .strict();

export const updateQuotaLimitsSchema = z
  .object({
    expected_version: z.number().int().nonnegative(),
    limits: quotaLimitsSchema,
  })
  .strict();

const serverErrorSchema = z
  .object({
    code: z.enum([
      "INVALID_STREAM_CURSOR",
      "RELIABILITY_NOT_FOUND",
      "RELIABILITY_CONFLICT",
      "RELIABILITY_INVALID",
      "RELIABILITY_CUTOVER",
      "DATABASE_UNAVAILABLE",
    ]),
    message: z.string().min(1),
    request_id: z.string().min(1),
  })
  .strict();

type ServerErrorCode = z.infer<typeof serverErrorSchema>["code"];

const SAFE_MESSAGES: Record<ServerErrorCode, string> = {
  INVALID_STREAM_CURSOR: "Project governance cursor is invalid.",
  RELIABILITY_NOT_FOUND: "Project governance data was not found.",
  RELIABILITY_CONFLICT: "Project governance data changed. Refresh and retry.",
  RELIABILITY_INVALID: "Project governance request is invalid.",
  RELIABILITY_CUTOVER: "Project governance is not ready.",
  DATABASE_UNAVAILABLE: "Project governance is temporarily unavailable.",
};

export class ProjectGovernanceApiError extends Error {
  readonly status: number;
  readonly code:
    | ServerErrorCode
    | "AUTH_REQUIRED"
    | "NETWORK_ERROR"
    | "INVALID_RESPONSE";

  constructor(
    status: number,
    code: ProjectGovernanceApiError["code"],
    message: string,
  ) {
    super(message);
    this.name = "ProjectGovernanceApiError";
    this.status = status;
    this.code = code;
  }
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

export function projectGovernanceBaseURL(scope: ProjectClientScope): string {
  const parsed = projectClientScopeSchema.parse(scope);
  return `${getBackendBaseURL()}/api/projects/${encodeURIComponent(parsed.projectId)}`;
}

export async function requestProjectGovernance(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (isAbortError(error) || error instanceof ProjectGovernanceApiError) {
      throw error;
    }
    if (error instanceof AuthRequiredError) {
      throw new ProjectGovernanceApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new ProjectGovernanceApiError(
      0,
      "NETWORK_ERROR",
      "Project governance is temporarily unavailable.",
    );
  }
}

export async function readProjectGovernanceResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
): Promise<T> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new ProjectGovernanceApiError(
      response.status,
      "INVALID_RESPONSE",
      "Project governance response was invalid.",
    );
  }
  if (!response.ok) {
    const parsed = serverErrorSchema.safeParse(body);
    if (!parsed.success) {
      throw new ProjectGovernanceApiError(
        response.status,
        "INVALID_RESPONSE",
        "Project governance request failed.",
      );
    }
    throw new ProjectGovernanceApiError(
      response.status,
      parsed.data.code,
      SAFE_MESSAGES[parsed.data.code],
    );
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new ProjectGovernanceApiError(
      response.status,
      "INVALID_RESPONSE",
      "Project governance response was invalid.",
    );
  }
  return parsed.data;
}

export function projectUsageQueryKey(scope: ProjectClientScope) {
  const parsed = projectClientScopeSchema.parse(scope);
  return [
    "account",
    parsed.accountId,
    "project",
    parsed.projectId,
    "governance",
    "usage",
  ] as const;
}

function requiredScope(access: PrivateWorkAccess): ProjectClientScope {
  if (!access.scope) throw new Error("Project governance scope is unavailable");
  return access.scope;
}

export async function fetchProjectUsage(
  scope: ProjectClientScope,
  signal?: AbortSignal,
): Promise<ProjectUsage> {
  const response = await requestProjectGovernance(
    `${projectGovernanceBaseURL(scope)}/usage`,
    { signal },
  );
  return readProjectGovernanceResponse(response, usageResponseSchema);
}

export async function updateProjectQuotaLimits(
  scope: ProjectClientScope,
  input: UpdateQuotaLimits,
  signal?: AbortSignal,
): Promise<QuotaPolicy> {
  const body = updateQuotaLimitsSchema.parse(input);
  const response = await requestProjectGovernance(
    `${projectGovernanceBaseURL(scope)}/usage/limits`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    },
  );
  return readProjectGovernanceResponse(response, quotaPolicySchema);
}

export function projectUsageQueryOptions(
  access: PrivateWorkAccess,
  enabled = true,
) {
  const scope = access.scope;
  return {
    queryKey: scope
      ? projectUsageQueryKey(scope)
      : (["governance", "usage", "inactive"] as const),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchProjectUsage(requiredScope(access), signal),
    enabled: enabled && scope !== null,
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function useProjectUsage(enabled = true) {
  const access = usePrivateWorkAccess();
  return useQuery(projectUsageQueryOptions(access, enabled));
}

export function useUpdateProjectQuotaLimits() {
  const queryClient = useQueryClient();
  const access = usePrivateWorkAccess();
  const scope = access.scope;
  return useMutation({
    mutationKey: scope
      ? [...projectUsageQueryKey(scope), "mutation", "limits"]
      : ["governance", "usage", "inactive", "mutation"],
    mutationFn: (input: UpdateQuotaLimits) =>
      runPrivateWorkAbortable(access, (signal) =>
        updateProjectQuotaLimits(requiredScope(access), input, signal),
      ),
    onSuccess: async () => {
      if (scope) {
        await queryClient.invalidateQueries({
          queryKey: projectUsageQueryKey(scope),
        });
      }
    },
  });
}

export type ProjectUsage = z.infer<typeof usageResponseSchema>;
export type QuotaPolicy = z.infer<typeof quotaPolicySchema>;
export type UpdateQuotaLimits = z.input<typeof updateQuotaLimitsSchema>;
