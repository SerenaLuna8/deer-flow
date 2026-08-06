import type { Message } from "@langchain/langgraph-sdk";

import { extractTextFromMessage } from "./utils";

export interface BrowserViewMeta {
  screenshot: string;
  url?: string;
  title?: string;
}

export function indexToolCallData(messages: Message[]) {
  const toolCallResults = new Map<string, string>();
  const browserViews = new Map<string, BrowserViewMeta>();

  for (const message of messages) {
    if (message.type !== "tool") continue;
    const toolCallId = message.tool_call_id;
    if (typeof toolCallId !== "string" || toolCallId.length === 0) continue;

    if (!toolCallResults.has(toolCallId)) {
      const result = extractTextFromMessage(message);
      if (result) toolCallResults.set(toolCallId, result);
    }

    if (!browserViews.has(toolCallId)) {
      const browserView = message.additional_kwargs?.browser_view as
        | BrowserViewMeta
        | undefined;
      if (browserView && typeof browserView.screenshot === "string") {
        browserViews.set(toolCallId, browserView);
      }
    }
  }

  return { toolCallResults, browserViews };
}
