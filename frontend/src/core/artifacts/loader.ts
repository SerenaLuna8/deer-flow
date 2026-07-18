import type { BaseStream } from "@langchain/langgraph-sdk/react";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";

import type { AgentThreadState } from "../threads";

import { buildWriteFileDraftContent } from "./preview";
export async function loadArtifactContent({
  url,
  signal,
}: {
  filepath: string;
  url: string;
  signal?: AbortSignal;
}) {
  // Project file URLs are authority-bearing UUID endpoints. Fetch the exact
  // URL; packaged `.skill` archives are download-only and have no member route.
  const response = signal
    ? await fetchWithAuth(url, { signal })
    : await fetchWithAuth(url);
  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to load artifact content");
  }
  const text = await response.text();
  return { content: text, url };
}

export function loadArtifactContentFromToolCall({
  url: urlString,
  thread,
}: {
  url: string;
  thread: BaseStream<AgentThreadState>;
}) {
  const draftContent = buildWriteFileDraftContent({
    filepath: urlString,
    messages: thread.messages,
  });
  if (draftContent !== undefined) {
    return draftContent;
  }

  const url = new URL(urlString);
  const toolCallId = url.searchParams.get("tool_call_id");
  const messageId = url.searchParams.get("message_id");
  if (messageId && toolCallId) {
    const message = thread.messages.find((message) => message.id === messageId);
    if (message?.type === "ai" && message.tool_calls) {
      const toolCall = message.tool_calls.find(
        (toolCall) => toolCall.id === toolCallId,
      );
      if (toolCall) {
        return toolCall.args.content;
      }
    }
  }
}
