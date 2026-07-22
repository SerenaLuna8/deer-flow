import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import type { ProjectPrivateWorkScope } from "@/core/private-work/types";

import type { ThreadTokenUsageResponse } from "./types";

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
