import type { BaseStream } from "@langchain/langgraph-sdk/react";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";

import type { AgentThreadState } from "../threads";

import { buildWriteFileDraftContent } from "./preview";
export async function loadArtifactContent({
  filepath,
  url,
  signal,
}: {
  filepath: string;
  url: string;
  signal?: AbortSignal;
}) {
  let artifactURL = url;
  if (filepath.endsWith(".skill")) {
    artifactURL = `${url.replace(/\/$/u, "")}/SKILL.md`;
  }
  const response = signal
    ? await fetchWithAuth(artifactURL, { signal })
    : await fetchWithAuth(artifactURL);
  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to load artifact content");
  }
  const text = await response.text();
  return { content: text, url: artifactURL };
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
