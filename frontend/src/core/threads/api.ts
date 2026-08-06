import { z } from "zod";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  eventSequenceSchema,
  type EventSequence,
} from "@/core/private-work/event-sequence";
import type { ProjectPrivateWorkScope } from "@/core/private-work/types";

import type { RunMessage, ThreadTokenUsageResponse } from "./types";

const projectPrivateWorkURLPattern =
  /\/api\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\/private-work$/iu;

const persistedMessageSchema = z
  .object({
    type: z.string().min(1),
    content: z.union([z.string(), z.array(z.unknown())]),
  })
  // LangChain messages are an extensible third-party union. The ActWeave
  // wrapper below stays strict while this nested payload preserves supported
  // provider/tool-specific fields.
  .passthrough();

const runMessageSchema = z
  .object({
    run_id: z.string().min(1),
    seq: eventSequenceSchema,
    content: persistedMessageSchema,
    metadata: z.record(z.unknown()),
    created_at: z.string().min(1),
  })
  .strict();

const runMessagesPageSchema = z
  .object({
    data: z.array(runMessageSchema),
    has_more: z.boolean(),
  })
  .strict()
  .superRefine((page, context) => {
    if (page.has_more && page.data.length === 0) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "A paginated run message page cannot advance without data",
        path: ["data"],
      });
    }
  });

export type RunMessagesPageResponse = {
  data: RunMessage[];
  has_more: boolean;
};

function requireProjectPrivateWorkURL(apiBaseURL: string): string {
  const value = apiBaseURL.replace(/\/$/u, "");
  if (!projectPrivateWorkURLPattern.test(value)) {
    throw new Error("Run messages require a project private-work URL");
  }
  return value;
}

export function buildRunMessagesUrl(
  apiBaseURL: string,
  threadId: string,
  runId: string,
  beforeSeq?: EventSequence,
) {
  const baseURL = requireProjectPrivateWorkURL(apiBaseURL);
  const path = `${baseURL}/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/messages`;
  if (beforeSeq === undefined) return path;
  const parsedBeforeSeq = eventSequenceSchema.parse(beforeSeq);
  return `${path}?before_seq=${parsedBeforeSeq}`;
}

export async function fetchRunMessagesPage(
  apiBaseURL: string,
  threadId: string,
  runId: string,
  beforeSeq?: EventSequence,
  signal?: AbortSignal,
): Promise<RunMessagesPageResponse> {
  const response = await fetchWithAuth(
    buildRunMessagesUrl(apiBaseURL, threadId, runId, beforeSeq),
    {
      method: "GET",
      signal,
    },
  );
  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to load thread history.");
  }
  return runMessagesPageSchema.parse(
    await response.json(),
  ) as RunMessagesPageResponse;
}

const threadCompactResponseSchema = z
  .object({
    thread_id: z.string().uuid(),
    compacted: z.boolean(),
    reason: z.string().min(1).nullable(),
    removed_message_count: z.number().int().nonnegative(),
    preserved_message_count: z.number().int().nonnegative(),
    summary_updated: z.boolean(),
    checkpoint_id: z.string().min(1).nullable(),
    total_tokens: z.number().int().nonnegative(),
  })
  .strict();

export type ThreadCompactResponse = z.infer<typeof threadCompactResponseSchema>;

export type CompactThreadKeep =
  | { type: "fraction"; value: number }
  | { type: "tokens"; value: number }
  | { type: "messages"; value: number };

const compactThreadKeepSchema = z.discriminatedUnion("type", [
  z
    .object({ type: z.literal("fraction"), value: z.number().gt(0).max(1) })
    .strict(),
  z
    .object({ type: z.literal("tokens"), value: z.number().int().positive() })
    .strict(),
  z
    .object({
      type: z.literal("messages"),
      value: z.number().int().nonnegative(),
    })
    .strict(),
]);

export const DREAM_COMPACTION_MAX_PASSES = 64;
export const DREAM_COMPACTION_KEEP = {
  type: "messages",
  value: 0,
} as const satisfies CompactThreadKeep;

export type DreamThreadCompactionResult = {
  compactedPasses: number;
  latestCheckpointId: string | null;
};

export class DreamThreadCompactionError extends Error {
  constructor(
    readonly reason: "unexpected_result" | "no_progress" | "pass_limit",
  ) {
    super("Dream could not finish archiving the current conversation.");
    this.name = "DreamThreadCompactionError";
  }
}

/**
 * `/Dream` is stronger than ordinary `/compact`: every completed turn must be
 * archived before Dream admission. Each successful request can consume only
 * one model-bounded whole-turn fragment, so keep draining until the server
 * explicitly reports that no complete turn remains.
 */
export async function compactThreadForDream(
  threadId: string,
  options: PrivateWorkRequestOptions & { signal?: AbortSignal },
  onCompacted?: (result: ThreadCompactResponse) => void,
): Promise<DreamThreadCompactionResult> {
  const checkpointIds = new Set<string>();
  let compactedPasses = 0;
  let latestCheckpointId: string | null = null;

  while (compactedPasses < DREAM_COMPACTION_MAX_PASSES) {
    const result = await compactThreadContext(threadId, {
      ...options,
      keep: DREAM_COMPACTION_KEEP,
    });
    if (!result.compacted) {
      if (result.reason !== "not_enough_messages") {
        throw new DreamThreadCompactionError("unexpected_result");
      }
      return { compactedPasses, latestCheckpointId };
    }

    if (
      result.reason !== null ||
      !result.summary_updated ||
      result.removed_message_count === 0 ||
      result.checkpoint_id === null ||
      checkpointIds.has(result.checkpoint_id)
    ) {
      throw new DreamThreadCompactionError("no_progress");
    }

    checkpointIds.add(result.checkpoint_id);
    compactedPasses += 1;
    latestCheckpointId = result.checkpoint_id;
    onCompacted?.(result);
  }

  throw new DreamThreadCompactionError("pass_limit");
}

export type PrivateWorkRequestOptions = Pick<
  ProjectPrivateWorkScope,
  "apiBaseURL"
>;

export type CompactThreadContextOptions = PrivateWorkRequestOptions & {
  signal?: AbortSignal;
  keep?: CompactThreadKeep;
};

function threadAPIBaseURL(options: PrivateWorkRequestOptions): string {
  return options.apiBaseURL;
}

export type ThreadBranchResponse = {
  thread_id: string;
  parent_thread_id: string;
  parent_checkpoint_id: string;
  branched_from_message_id: string;
  workspace_clone_mode: string;
};

export type BranchThreadFromTurnInput = {
  messageId: string;
  messageIds?: string[];
  title?: string;
};

async function readThreadAPIError(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) {
      return body.detail;
    }
  } catch {
    // Fall through to the caller-provided message.
  }
  return fallback;
}

export async function fetchThreadTokenUsage(
  threadId: string,
  options: PrivateWorkRequestOptions,
): Promise<ThreadTokenUsageResponse | null> {
  const response = await fetchWithAuth(
    `${threadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/token-usage`,
    {
      method: "GET",
    },
  );

  if (!response.ok) {
    if (response.status === 403 || response.status === 404) {
      return null;
    }
    throw new Error("Failed to load thread token usage.");
  }

  return (await response.json()) as ThreadTokenUsageResponse;
}

export async function branchThreadFromTurn(
  threadId: string,
  input: BranchThreadFromTurnInput,
  options: PrivateWorkRequestOptions,
): Promise<ThreadBranchResponse> {
  const response = await fetchWithAuth(
    `${threadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/branches`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        message_id: input.messageId,
        message_ids: input.messageIds ?? [input.messageId],
        ...(input.title ? { title: input.title } : {}),
      }),
    },
  );

  if (!response.ok) {
    throw new Error(
      await readThreadAPIError(response, "Failed to branch conversation."),
    );
  }

  return (await response.json()) as ThreadBranchResponse;
}

export async function compactThreadContext(
  threadId: string,
  options: CompactThreadContextOptions,
): Promise<ThreadCompactResponse> {
  const keep =
    options.keep === undefined
      ? undefined
      : compactThreadKeepSchema.parse(options.keep);
  const response = await fetchWithAuth(
    `${threadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/compact`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        force: true,
        ...(keep === undefined ? {} : { keep }),
      }),
      signal: options.signal,
    },
  );

  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to compact context.");
  }

  return threadCompactResponseSchema.parse(await response.json());
}
