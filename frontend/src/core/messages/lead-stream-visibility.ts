import type { Message } from "@langchain/langgraph-sdk";

export type StreamMessageMetadataLookup = (
  message: Message,
  index: number,
) =>
  | {
      streamMetadata?: Record<string, unknown>;
    }
  | undefined;

function hasSubagentTag(
  metadata:
    | {
        streamMetadata?: Record<string, unknown>;
      }
    | undefined,
) {
  const tags = metadata?.streamMetadata?.tags;
  return (
    Array.isArray(tags) &&
    tags.some((tag) => typeof tag === "string" && tag.startsWith("subagent:"))
  );
}

function aiToolCallIds(message: Message) {
  if (message.type !== "ai") {
    return [];
  }
  return (message.tool_calls ?? []).flatMap((toolCall) =>
    typeof toolCall.id === "string" && toolCall.id.length > 0
      ? [toolCall.id]
      : [],
  );
}

/**
 * Keep the lead-Agent conversation projection free of independently streamed
 * subagent messages.
 *
 * Delegated execution already reports its ordered progress through
 * `task_running`, which owns the corresponding SubtaskCard. A provider stream
 * carrying the trusted `subagent:<name>` tag is therefore a duplicate internal
 * channel: rendering it as a root message both scrambles the conversation and
 * can trigger artifact auto-preview for a subagent's temporary `write_file`.
 */
export function filterLeadAgentStreamMessages(
  messages: Message[],
  getMessagesMetadata?: StreamMessageMetadataLookup,
): Message[] {
  if (!getMessagesMetadata) {
    return messages;
  }

  const hiddenMessages = new Set<Message>();
  const hiddenToolCallIds = new Set<string>();
  const visibleToolCallIds = new Set<string>();

  messages.forEach((message, index) => {
    if (hasSubagentTag(getMessagesMetadata(message, index))) {
      hiddenMessages.add(message);
      for (const toolCallId of aiToolCallIds(message)) {
        hiddenToolCallIds.add(toolCallId);
      }
      return;
    }
    for (const toolCallId of aiToolCallIds(message)) {
      visibleToolCallIds.add(toolCallId);
    }
  });

  if (hiddenMessages.size === 0) {
    return messages;
  }

  return messages.filter((message) => {
    if (hiddenMessages.has(message)) {
      return false;
    }
    return !(
      message.type === "tool" &&
      typeof message.tool_call_id === "string" &&
      hiddenToolCallIds.has(message.tool_call_id) &&
      !visibleToolCallIds.has(message.tool_call_id)
    );
  });
}
