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

export type ThreadCompactResponse = {
  thread_id: string;
  compacted: boolean;
  reason?: string | null;
  removed_message_count: number;
  preserved_message_count: number;
  summary_updated: boolean;
  checkpoint_id?: string | null;
  total_tokens: number;
};

export type PrivateWorkRequestOptions = Pick<
  ProjectPrivateWorkScope,
  "apiBaseURL"
>;

export type CompactThreadContextOptions = PrivateWorkRequestOptions & {
  signal?: AbortSignal;
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
  const response = await fetchWithAuth(
    `${threadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/compact`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        force: true,
      }),
      signal: options.signal,
    },
  );

  if (!response.ok) {
    throw new Error(
      await readThreadAPIError(response, "Failed to compact context."),
    );
  }

  return (await response.json()) as ThreadCompactResponse;
}
