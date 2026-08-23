import type { Run } from "@langchain/langgraph-sdk";
import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

import { REASONING_EFFORTS } from "../private-work/execution-profile";
import { usePrivateWorkAccess } from "../private-work/provider";
import type { ProjectPrivateWorkScope } from "../private-work/types";
import { RUN_WORKLOAD_PROFILES } from "../private-work/workload-profile";

import { scopedThreadQueryKey } from "./thread-query-key";

export const THREAD_RUNS_PAGE_SIZE = 1000;
export const THREAD_RUNS_MAX_PAGES = 1000;
export const THREAD_RUNS_MAX_OFFSET = 100_000;

const PRIVATE_RUN_METADATA_KEYS = new Set([
  "project_id",
  "owner_user_id",
  "user_id",
]);
const PRIVATE_RUN_METADATA_KEY_PARTS = [
  "secret",
  "token",
  "password",
  "credential",
  "ciphertext",
  "private_key",
  "key_id",
  "nonce",
  "storage_locator",
] as const;
const THREAD_RUN_METADATA_MAX_NODES = 10_000;

function isPrivateRunMetadataKey(key: string): boolean {
  const normalized = key.toLowerCase();
  return (
    PRIVATE_RUN_METADATA_KEYS.has(normalized) ||
    PRIVATE_RUN_METADATA_KEY_PARTS.some((part) => normalized.includes(part))
  );
}

const threadRunMetadataSchema = z
  .record(z.unknown())
  .superRefine((metadata, context) => {
    const pending: Array<{
      path: Array<string | number>;
      value: unknown;
    }> = [{ path: [], value: metadata }];
    const visited = new Set<object>();
    let visitedNodes = 0;

    while (pending.length > 0) {
      const current = pending.pop();
      if (!current || current.value === null) {
        continue;
      }
      if (typeof current.value !== "object") {
        continue;
      }
      if (visited.has(current.value)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Run metadata must be an acyclic JSON object.",
          path: current.path,
        });
        return;
      }
      visited.add(current.value);
      visitedNodes += 1;
      if (visitedNodes > THREAD_RUN_METADATA_MAX_NODES) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Run metadata exceeds the validation safety limit.",
          path: current.path,
        });
        return;
      }

      if (Array.isArray(current.value)) {
        current.value.forEach((value, index) => {
          pending.push({ path: [...current.path, index], value });
        });
        continue;
      }

      for (const [key, value] of Object.entries(current.value)) {
        const path = [...current.path, key];
        if (isPrivateRunMetadataKey(key)) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Run metadata contains private authority data.",
            path,
          });
          continue;
        }
        pending.push({ path, value });
      }
    }
  });

const runExecutionProfileSchema = z
  .object({
    model_name: z.string().min(1),
    thinking_enabled: z.boolean(),
    reasoning_effort: z.enum(REASONING_EFFORTS).nullable(),
    supports_vision: z.boolean(),
  })
  .strict();

const threadRunSchema = z
  .object({
    run_id: z.string().min(1),
    thread_id: z.string().min(1),
    assistant_id: z.string().min(1).nullable(),
    created_at: z.string().min(1),
    updated_at: z.string().min(1),
    status: z.enum([
      "pending",
      "running",
      "error",
      "success",
      "timeout",
      "interrupted",
    ]),
    metadata: threadRunMetadataSchema,
    multitask_strategy: z.enum(["reject", "interrupt", "rollback", "enqueue"]),
    error: z.string().nullable(),
    model_name: z.string().min(1).nullable(),
    execution_profile: runExecutionProfileSchema.nullable(),
    workload_profile: z.enum(RUN_WORKLOAD_PROFILES).nullable().optional(),
  })
  .strict();

const threadRunPageSchema = z.array(threadRunSchema).max(THREAD_RUNS_PAGE_SIZE);

type ThreadRunsListClient = {
  runs: {
    list: (
      threadId: string,
      options?: {
        limit?: number;
        offset?: number;
        signal?: AbortSignal;
      },
    ) => Promise<unknown>;
  };
};

export async function fetchAllThreadRuns(
  apiClient: ThreadRunsListClient,
  threadId: string,
  pageSize: number = THREAD_RUNS_PAGE_SIZE,
  signal?: AbortSignal,
): Promise<Run[]> {
  if (!Number.isSafeInteger(pageSize) || pageSize < 1 || pageSize > 1000) {
    throw new RangeError("Thread Run page size must be between 1 and 1000.");
  }

  const runs: Run[] = [];
  const seenRunIds = new Set<string>();
  let offset = 0;
  let pageCount = 0;

  while (true) {
    signal?.throwIfAborted();
    if (pageCount >= THREAD_RUNS_MAX_PAGES) {
      throw new Error("Thread Run pagination exceeded the page safety limit.");
    }
    if (offset > THREAD_RUNS_MAX_OFFSET) {
      throw new Error(
        "Thread Run pagination exceeded the offset safety limit.",
      );
    }

    const rawPage = await apiClient.runs.list(threadId, {
      limit: pageSize,
      offset,
      ...(signal ? { signal } : {}),
    });
    signal?.throwIfAborted();
    const page = threadRunPageSchema.parse(rawPage);
    pageCount += 1;
    if (page.length > pageSize) {
      throw new Error(
        "Thread Run pagination returned more rows than requested.",
      );
    }

    const seenCountBeforePage = seenRunIds.size;
    for (const run of page) {
      if (!seenRunIds.has(run.run_id)) {
        seenRunIds.add(run.run_id);
        // Gateway runs intentionally use a nullable assistant_id and expose
        // two public diagnostic fields beyond the SDK's narrower Run type.
        runs.push(run as unknown as Run);
      }
    }

    if (page.length < pageSize) {
      return runs;
    }
    if (seenRunIds.size === seenCountBeforePage) {
      throw new Error("Thread Run pagination returned a non-advancing page.");
    }
    if (pageCount >= THREAD_RUNS_MAX_PAGES) {
      throw new Error("Thread Run pagination exceeded the page safety limit.");
    }
    if (page.length > THREAD_RUNS_MAX_OFFSET - offset) {
      throw new Error(
        "Thread Run pagination exceeded the offset safety limit.",
      );
    }
    offset += page.length;
  }
}

export function useThreadRuns(
  threadId?: string,
  { enabled = true }: { enabled?: boolean } = {},
  explicitPrivateWork?: ProjectPrivateWorkScope,
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery<Run[]>({
    queryKey: scopedThreadQueryKey(privateWork.scope, "thread", threadId),
    queryFn: async ({ signal }) => {
      if (!threadId) {
        return [];
      }
      return fetchAllThreadRuns(
        privateWork.client,
        threadId,
        undefined,
        signal,
      );
    },
    enabled: enabled && Boolean(threadId),
    retry: false,
    refetchOnWindowFocus: false,
  });
}
