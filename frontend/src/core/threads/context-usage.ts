import { z } from "zod";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import type { ProjectPrivateWorkScope } from "@/core/private-work/types";

const progressFields = {
  progress_percent: z.number().finite().min(0).max(100),
  reached: z.boolean(),
};

const tokenTriggerSchema = z
  .object({
    type: z.literal("tokens"),
    configured_value: z.number().int().positive(),
    current_value: z.number().int().nonnegative(),
    threshold_value: z.number().int().positive(),
    remaining_value: z.number().int().nonnegative(),
    ...progressFields,
    threshold_tokens: z.number().int().positive(),
  })
  .strict();

const fractionTriggerSchema = z
  .object({
    type: z.literal("fraction"),
    configured_value: z.number().finite().gt(0).max(1),
    current_value: z.number().finite().nonnegative(),
    threshold_value: z.number().finite().gt(0).max(1),
    remaining_value: z.number().finite().nonnegative(),
    ...progressFields,
    context_window_tokens: z.number().int().positive(),
    threshold_tokens: z.number().int().positive(),
  })
  .strict();

const messageTriggerSchema = z
  .object({
    type: z.literal("messages"),
    configured_value: z.number().int().positive(),
    current_value: z.number().int().nonnegative(),
    threshold_value: z.number().int().positive(),
    remaining_value: z.number().int().nonnegative(),
    ...progressFields,
  })
  .strict();

const contextUsageTriggerSchema = z.discriminatedUnion("type", [
  tokenTriggerSchema,
  fractionTriggerSchema,
  messageTriggerSchema,
]);

const contextUsageComponentSchema = z
  .object({
    estimated_tokens: z.number().int().nonnegative(),
    error_allowance_tokens: z.number().int().nonnegative(),
    safety_bound_tokens: z.number().int().nonnegative(),
  })
  .strict();

const contextUsageComponentsSchema = z
  .object({
    compressible: contextUsageComponentSchema,
    fixed: contextUsageComponentSchema,
    ephemeral: contextUsageComponentSchema,
  })
  .strict();

const threadContextUsageResponseSchema = z
  .object({
    thread_id: z.string().min(1),
    enabled: z.boolean(),
    estimated_tokens: z.number().int().nonnegative(),
    error_allowance_tokens: z.number().int().nonnegative(),
    safety_bound_tokens: z.number().int().nonnegative(),
    provider_input_tokens: z.number().int().nonnegative().nullable(),
    estimator_revision: z.string().min(1).max(128).nullable(),
    error_contract: z.string().min(1).max(512).nullable(),
    components: contextUsageComponentsSchema,
    fixed_over_trigger: z.boolean(),
    message_count: z.number().int().nonnegative(),
    summary_present: z.boolean(),
    context_window_tokens: z.number().int().positive().nullable(),
    triggers: z.array(contextUsageTriggerSchema).max(8),
    primary_trigger: contextUsageTriggerSchema.nullable(),
  })
  .strict();

const threadContextAuthorityResponseSchema = z
  .object({
    thread_id: z.string().min(1),
    cache_marker: z
      .string()
      .min(6)
      .max(71)
      .regex(
        /^(?:active:[A-Za-z0-9][A-Za-z0-9._-]{0,63}|idle:(?:none|[A-Za-z0-9][A-Za-z0-9._-]{0,63}))$/,
      ),
  })
  .strict();

export const CONTEXT_AUTHORITY_REFETCH_INTERVAL_MS = 5_000;

export type ContextUsageTrigger = z.infer<typeof contextUsageTriggerSchema>;
export type ThreadContextUsageResponse = z.infer<
  typeof threadContextUsageResponseSchema
>;
export type ThreadContextAuthorityResponse = z.infer<
  typeof threadContextAuthorityResponseSchema
>;

export function threadContextUsageQueryKey(
  threadId?: string | null,
  modelName?: string | null,
) {
  return modelName
    ? (["thread-context-usage", threadId, modelName] as const)
    : (["thread-context-usage", threadId] as const);
}

export function threadContextAuthorityQueryKey(threadId?: string | null) {
  return [...threadContextUsageQueryKey(threadId), "authority"] as const;
}

export function threadContextUsageReadingQueryKey(
  threadId?: string | null,
  modelName?: string | null,
  cacheMarker?: string | null,
) {
  return [
    ...threadContextUsageQueryKey(threadId, modelName),
    cacheMarker ?? null,
  ] as const;
}

export async function fetchThreadContextAuthority(
  threadId: string,
  options: Pick<ProjectPrivateWorkScope, "apiBaseURL"> & {
    signal?: AbortSignal;
  },
): Promise<ThreadContextAuthorityResponse | null> {
  const response = await fetchWithAuth(
    `${options.apiBaseURL}/threads/${encodeURIComponent(threadId)}/context-usage/authority`,
    { method: "GET", signal: options.signal },
  );

  if (!response.ok) {
    if (response.status === 403 || response.status === 404) {
      return null;
    }
    await throwGatewayApiError(
      response,
      "Failed to load context usage authority.",
    );
  }

  return threadContextAuthorityResponseSchema.parse(await response.json());
}

export async function fetchThreadContextUsage(
  threadId: string,
  options: Pick<ProjectPrivateWorkScope, "apiBaseURL"> & {
    modelName?: string | null;
    signal?: AbortSignal;
  },
): Promise<ThreadContextUsageResponse | null> {
  const query = new URLSearchParams();
  if (options.modelName) {
    query.set("model_name", options.modelName);
  }
  const queryString = query.toString();
  const response = await fetchWithAuth(
    `${options.apiBaseURL}/threads/${encodeURIComponent(threadId)}/context-usage${queryString ? `?${queryString}` : ""}`,
    { method: "GET", signal: options.signal },
  );

  if (!response.ok) {
    if (response.status === 403 || response.status === 404) {
      return null;
    }
    await throwGatewayApiError(response, "Failed to load context usage.");
  }

  return threadContextUsageResponseSchema.parse(await response.json());
}
