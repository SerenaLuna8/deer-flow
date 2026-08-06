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

const threadContextUsageResponseSchema = z
  .object({
    thread_id: z.string().min(1),
    enabled: z.boolean(),
    estimated_tokens: z.number().int().nonnegative(),
    message_count: z.number().int().nonnegative(),
    summary_present: z.boolean(),
    context_window_tokens: z.number().int().positive().nullable(),
    triggers: z.array(contextUsageTriggerSchema).max(8),
    primary_trigger: contextUsageTriggerSchema.nullable(),
  })
  .strict();

export type ContextUsageTrigger = z.infer<typeof contextUsageTriggerSchema>;
export type ThreadContextUsageResponse = z.infer<
  typeof threadContextUsageResponseSchema
>;

export function threadContextUsageQueryKey(threadId?: string | null) {
  return ["thread-context-usage", threadId] as const;
}

export async function fetchThreadContextUsage(
  threadId: string,
  options: Pick<ProjectPrivateWorkScope, "apiBaseURL"> & {
    signal?: AbortSignal;
  },
): Promise<ThreadContextUsageResponse | null> {
  const response = await fetchWithAuth(
    `${options.apiBaseURL}/threads/${encodeURIComponent(threadId)}/context-usage`,
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
