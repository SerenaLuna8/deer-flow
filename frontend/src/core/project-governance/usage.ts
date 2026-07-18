"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";
import {
  projectClientScopeSchema,
  runPrivateWorkAbortable,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "@/core/private-work/types";

import { governanceRoot } from "./query-keys";

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

const quotaCounterShape = {
  used: z.number().int().nonnegative(),
  reserved: z.number().int().nonnegative(),
  limit: z.number().int().nonnegative(),
  warning_threshold_reached: z.boolean(),
};

const quotaDimensionSchema = z.discriminatedUnion("dimension", [
  z
    .object({
      dimension: z.literal("members"),
      bucket: z.literal("lifetime"),
      ...quotaCounterShape,
    })
    .strict(),
  z
    .object({
      dimension: z.literal("storage_bytes"),
      bucket: z.literal("lifetime"),
      ...quotaCounterShape,
    })
    .strict(),
  z
    .object({
      dimension: z.literal("concurrent_runs"),
      bucket: z.literal("lifetime"),
      ...quotaCounterShape,
    })
    .strict(),
  z
    .object({
      dimension: z.literal("mcp_calls_daily"),
      bucket: z.string().date(),
      ...quotaCounterShape,
    })
    .strict(),
]);

export const usageResponseSchema = z
  .object({
    policy: quotaPolicySchema,
    dimensions: z
      .array(quotaDimensionSchema)
      .length(4)
      .superRefine((dimensions, context) => {
        if (new Set(dimensions.map((item) => item.dimension)).size !== 4) {
          context.addIssue({
            code: "custom",
            message: "Every quota dimension is required exactly once",
          });
        }
      }),
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
  return [...governanceRoot(scope), "usage"] as const;
}

function requiredScope(access: PrivateWorkAccess): ProjectClientScope {
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
  scope: ProjectClientScope,
  enabled = true,
) {
  const parsed = projectClientScopeSchema.parse(scope);
  return {
    queryKey: projectUsageQueryKey(parsed),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchProjectUsage(parsed, signal),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function useProjectUsage(scope: ProjectClientScope) {
  return useQuery(projectUsageQueryOptions(scope));
}

export function useUpdateProjectQuotaLimits(access: PrivateWorkAccess) {
  const queryClient = useQueryClient();
  const scope = requiredScope(access);
  return useMutation({
    mutationKey: [...projectUsageQueryKey(scope), "mutation", "limits"],
    mutationFn: (input: UpdateQuotaLimits) =>
      runPrivateWorkAbortable(access, (signal) =>
        updateProjectQuotaLimits(requiredScope(access), input, signal),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: projectUsageQueryKey(scope),
      });
    },
  });
}

export type ProjectUsage = z.infer<typeof usageResponseSchema>;
export type QuotaPolicy = z.infer<typeof quotaPolicySchema>;
export type UpdateQuotaLimits = z.input<typeof updateQuotaLimitsSchema>;
