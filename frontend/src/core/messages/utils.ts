import type { AIMessage, Message } from "@langchain/langgraph-sdk";

import {
  extractHumanInputRequest,
  inferLegacyHumanInputControlMessageIndexes,
} from "@/core/messages/human-input";

interface GenericMessageGroup<T = string> {
  type: T;
  id: string;
  messages: Message[];
}

interface HumanMessageGroup extends GenericMessageGroup<"human"> {}

interface AssistantProcessingGroup extends GenericMessageGroup<"assistant:processing"> {}

interface AssistantMessageGroup extends GenericMessageGroup<"assistant"> {}

interface AssistantPresentFilesGroup extends GenericMessageGroup<"assistant:present-files"> {}

interface AssistantClarificationGroup extends GenericMessageGroup<"assistant:clarification"> {}

interface AssistantSubagentGroup extends GenericMessageGroup<"assistant:subagent"> {}

export type MessageGroup =
  | HumanMessageGroup
  | AssistantProcessingGroup
  | AssistantMessageGroup
  | AssistantPresentFilesGroup
  | AssistantClarificationGroup
  | AssistantSubagentGroup;

const HIDDEN_CONTROL_MESSAGE_NAMES = new Set([
  "summary",
  "loop_warning",
  "todo_reminder",
  "todo_completion_reminder",
]);

function messageRunId(message: Message): string | undefined {
  const directRunId = Reflect.get(message, "run_id");
  if (typeof directRunId === "string" && directRunId.length > 0) {
    return directRunId;
  }

  const additionalRunId = message.additional_kwargs?.run_id;
  return typeof additionalRunId === "string" && additionalRunId.length > 0
    ? additionalRunId
    : undefined;
}

export function getMessageGroups(messages: Message[]): MessageGroup[] {
  if (messages.length === 0) {
    return [];
  }

  const groups: MessageGroup[] = [];
  const groupByToolCallId = new Map<string, MessageGroup>();
  const pendingToolMessagesByCallId = new Map<string, Message[]>();
  const inferredHiddenHumanInputMessageIndexes =
    inferLegacyHumanInputControlMessageIndexes(messages);
  const usedGroupIds = new Set<string>();
  let unscopedTurn = 0;

  function toolCallAssociationKey(message: Message, toolCallId: string) {
    const runId = messageRunId(message);
    return JSON.stringify(
      runId
        ? ["run", runId, toolCallId]
        : ["unscoped-turn", unscopedTurn, toolCallId],
    );
  }

  function createGroupId(
    message: Message,
    index: number,
    type: MessageGroup["type"],
    preferredId?: string,
  ) {
    const messageId =
      preferredId ??
      (typeof message.id === "string" && message.id.length > 0
        ? message.id
        : `${type}-${index}`);
    let groupId = messageId;
    let suffix = 1;
    while (usedGroupIds.has(groupId)) {
      groupId = `${messageId}-${suffix}`;
      suffix += 1;
    }
    usedGroupIds.add(groupId);
    return groupId;
  }

  function associateToolCalls(message: AIMessage, group: MessageGroup) {
    for (const toolCall of message.tool_calls ?? []) {
      const toolCallId = toolCall.id;
      if (typeof toolCallId !== "string" || toolCallId.length === 0) {
        continue;
      }

      const associationKey = toolCallAssociationKey(message, toolCallId);
      if (groupByToolCallId.has(associationKey)) {
        continue;
      }

      groupByToolCallId.set(associationKey, group);
      const pendingToolMessages =
        pendingToolMessagesByCallId.get(associationKey) ?? [];
      group.messages.push(...pendingToolMessages);
      pendingToolMessagesByCallId.delete(associationKey);
    }
  }

  function associateToolMessage(message: Message) {
    if (message.type !== "tool") {
      return;
    }

    const toolCallId = message.tool_call_id;
    if (typeof toolCallId !== "string" || toolCallId.length === 0) {
      return;
    }

    const associationKey = toolCallAssociationKey(message, toolCallId);
    const group = groupByToolCallId.get(associationKey);
    if (group) {
      group.messages.push(message);
      return;
    }

    const pendingToolMessages =
      pendingToolMessagesByCallId.get(associationKey) ?? [];
    pendingToolMessages.push(message);
    pendingToolMessagesByCallId.set(associationKey, pendingToolMessages);
  }

  for (const [messageIndex, message] of messages.entries()) {
    if (message.type === "human") {
      // A hidden compatibility/control input still marks a new legacy turn.
      // Advancing before the visibility filter keeps unscoped call ids from
      // being reused across that otherwise invisible boundary.
      unscopedTurn += 1;
    }

    if (
      isHiddenFromUIMessage(message) ||
      inferredHiddenHumanInputMessageIndexes.has(messageIndex)
    ) {
      continue;
    }

    if (message.type === "human") {
      groups.push({
        id: createGroupId(message, messageIndex, "human"),
        type: "human",
        messages: [message],
      });
      continue;
    }

    if (message.type === "tool") {
      associateToolMessage(message);
      if (isClarificationToolMessage(message)) {
        // The exact issuing AI group receives the result through
        // associateToolMessage. Keep the standalone card for prominent input.
        const requestId = extractHumanInputRequest(message)?.request_id;
        groups.push({
          id: createGroupId(
            message,
            messageIndex,
            "assistant:clarification",
            requestId ? `clarification-request:${requestId}` : undefined,
          ),
          type: "assistant:clarification",
          messages: [message],
        });
      }
      continue;
    }

    if (message.type === "ai") {
      // A message with answer content and no tool calls becomes its own
      // assistant bubble below, which already renders the message's
      // reasoning_content inside the bubble's <Reasoning> collapsible. Such a
      // message must NOT also feed the processing group, or the ChainOfThought
      // panel above the bubble paints the identical reasoning a second time
      // (#3868). Intermediate reasoning (no content) and tool-calling steps
      // still belong in the processing group.
      const becomesAssistantBubble =
        hasContent(message) && !hasToolCalls(message);
      let toolCallGroup: MessageGroup | null = null;

      if (hasPresentFiles(message)) {
        const group: AssistantPresentFilesGroup = {
          id: createGroupId(message, messageIndex, "assistant:present-files"),
          type: "assistant:present-files",
          messages: [message],
        };
        groups.push(group);
        toolCallGroup = group;
      } else if (hasSubagent(message)) {
        const group: AssistantSubagentGroup = {
          id: createGroupId(message, messageIndex, "assistant:subagent"),
          type: "assistant:subagent",
          messages: [message],
        };
        groups.push(group);
        toolCallGroup = group;
      } else if (
        !becomesAssistantBubble &&
        (hasReasoning(message) || hasToolCalls(message))
      ) {
        const lastGroup = groups[groups.length - 1];
        // Accumulate consecutive intermediate AI messages into one processing group.
        if (lastGroup?.type !== "assistant:processing") {
          const group: AssistantProcessingGroup = {
            id: createGroupId(message, messageIndex, "assistant:processing"),
            type: "assistant:processing",
            messages: [message],
          };
          groups.push(group);
          toolCallGroup = group;
        } else {
          lastGroup.messages.push(message);
          toolCallGroup = lastGroup;
        }
      }

      if (toolCallGroup) {
        associateToolCalls(message, toolCallGroup);
      }

      if (becomesAssistantBubble) {
        groups.push({
          id: createGroupId(message, messageIndex, "assistant"),
          type: "assistant",
          messages: [message],
        });
      }
    }
  }

  // Any tool results still pending reference no visible issuing AI call. They
  // are intentionally omitted: guessing a nearby group can leak raw tool
  // output into a terminal assistant answer and scramble the conversation.
  return groups;
}

/**
 * Returns the transcript-visible projection of a complete ordered message
 * list, including the strict compatibility rule for legacy clarification
 * replies whose persisted `hide_from_ui` flag was lost.
 */
export function filterUIVisibleMessages(messages: Message[]): Message[] {
  const inferredHiddenHumanInputMessageIndexes =
    inferLegacyHumanInputControlMessageIndexes(messages);
  return messages.filter(
    (message, messageIndex) =>
      !isHiddenFromUIMessage(message) &&
      !inferredHiddenHumanInputMessageIndexes.has(messageIndex),
  );
}

export type AssistantTurnDisplay = {
  finalGroupIndex: number;
  hiddenGroupIndexes: number[];
  processStepCount: number;
  processGroupIndexes: number[];
  presentFilesGroupIndexes: number[];
  presentedFiles: string[];
};

export type AssistantTurnDisplayOptions = {
  isCurrentTurnLoading?: boolean;
};

function hasDisplayableReasoning(message: Message): boolean {
  const reasoning = extractReasoningContentFromMessage(message);
  return typeof reasoning === "string" && reasoning.trim().length > 0;
}

function hasDisplayableProcessStep(message: Message): boolean {
  return (
    message.type === "ai" &&
    (hasDisplayableReasoning(message) ||
      (message.tool_calls ?? []).some(
        (toolCall) =>
          toolCall.name !== "present_files" &&
          toolCall.name !== "ask_clarification",
      ))
  );
}

/**
 * Projects safe semantic message groups into a calmer visual turn without
 * mutating their tool-result associations.
 *
 * Only contiguous processing, subagent, and present-files groups immediately
 * preceding a terminal assistant answer are moved. In-flight and
 * present-files-only turns remain in their original position so progress and
 * delivered files never disappear while a Run is still settling. The current
 * final-looking answer is not terminal until its Run has stopped loading.
 */
export function getAssistantTurnDisplays(
  groups: MessageGroup[],
  { isCurrentTurnLoading = false }: AssistantTurnDisplayOptions = {},
): AssistantTurnDisplay[] {
  const displays: AssistantTurnDisplay[] = [];

  for (const [finalGroupIndex, group] of groups.entries()) {
    if (group.type !== "assistant") {
      continue;
    }
    if (isCurrentTurnLoading && finalGroupIndex === groups.length - 1) {
      continue;
    }

    const hiddenGroupIndexes: number[] = [];
    for (
      let groupIndex = finalGroupIndex - 1;
      groupIndex >= 0;
      groupIndex -= 1
    ) {
      const candidate = groups[groupIndex];
      if (
        candidate?.type !== "assistant:processing" &&
        candidate?.type !== "assistant:subagent" &&
        candidate?.type !== "assistant:present-files"
      ) {
        break;
      }
      hiddenGroupIndexes.unshift(groupIndex);
    }

    if (hiddenGroupIndexes.length === 0) {
      continue;
    }

    const presentFilesGroupIndexes = hiddenGroupIndexes.filter(
      (groupIndex) => groups[groupIndex]?.type === "assistant:present-files",
    );
    const processGroupIndexes = hiddenGroupIndexes.filter((groupIndex) => {
      const candidate = groups[groupIndex];
      return (
        candidate?.type === "assistant:processing" ||
        candidate?.type === "assistant:subagent" ||
        (candidate?.type === "assistant:present-files" &&
          candidate.messages.some(hasDisplayableProcessStep))
      );
    });
    if (group.messages.some(hasDisplayableReasoning)) {
      processGroupIndexes.push(finalGroupIndex);
    }
    const presentedFiles = Array.from(
      new Set(
        presentFilesGroupIndexes.flatMap((groupIndex) =>
          (groups[groupIndex]?.messages ?? []).flatMap(
            extractPresentFilesFromMessage,
          ),
        ),
      ),
    );
    const processStepCount = processGroupIndexes
      .flatMap((groupIndex) => groups[groupIndex]?.messages ?? [])
      .reduce((count, message) => {
        if (message.type !== "ai") {
          return count;
        }
        const visibleToolCalls = (message.tool_calls ?? []).filter(
          (toolCall) =>
            toolCall.name !== "ask_clarification" &&
            toolCall.name !== "present_files",
        );
        return (
          count +
          visibleToolCalls.length +
          (hasDisplayableReasoning(message) ? 1 : 0)
        );
      }, 0);

    displays.push({
      finalGroupIndex,
      hiddenGroupIndexes,
      processStepCount,
      processGroupIndexes,
      presentFilesGroupIndexes,
      presentedFiles,
    });
  }

  return displays;
}

export type EditableTurn = {
  humanMessage: Message;
};

function isTerminalAssistantTextMessage(message: Message | undefined): boolean {
  return (
    message?.type === "ai" &&
    Boolean(extractTextFromMessage(message).trim()) &&
    !hasToolCalls(message)
  );
}

export function getLatestEditableTurn(
  groups: MessageGroup[],
  isCurrentTurnLoading: boolean,
): EditableTurn | null {
  if (isCurrentTurnLoading) {
    return null;
  }

  let candidate: EditableTurn | null = null;
  let currentHumanGroup: MessageGroup | null = null;
  let lastAIGroup: MessageGroup | null = null;

  const completeTurn = () => {
    if (!currentHumanGroup) {
      lastAIGroup = null;
      return;
    }

    const humanMessage = currentHumanGroup.messages.find(
      (message) => message.type === "human" && message.id,
    );
    const assistantMessage = [...(lastAIGroup?.messages ?? [])]
      .reverse()
      .find((message) => message.type === "ai" && message.id);

    candidate =
      lastAIGroup?.type === "assistant" &&
      humanMessage &&
      isTerminalAssistantTextMessage(assistantMessage)
        ? { humanMessage }
        : null;
    currentHumanGroup = null;
    lastAIGroup = null;
  };

  for (const group of groups) {
    if (group.type === "human") {
      completeTurn();
      currentHumanGroup = group;
      continue;
    }
    if (group.messages.some((message) => message.type === "ai")) {
      lastAIGroup = group;
    }
  }

  completeTurn();
  return candidate;
}

export function groupMessages<T>(
  messages: Message[],
  mapper: (group: MessageGroup) => T,
): T[] {
  return getMessageGroups(messages)
    .map(mapper)
    .filter((result) => result !== undefined && result !== null) as T[];
}

export function hasActiveAssistantReasoning(groups: MessageGroup[]) {
  let lastHumanIndex = -1;
  for (let index = groups.length - 1; index >= 0; index -= 1) {
    if (groups[index]?.type === "human") {
      lastHumanIndex = index;
      break;
    }
  }

  if (lastHumanIndex === -1) {
    return false;
  }

  return groups
    .slice(lastHumanIndex + 1)
    .some((group) => group.messages.some(hasReasoning));
}

export function getAssistantTurnUsageMessages(groups: MessageGroup[]) {
  const usageMessagesByGroupIndex: Array<Message[] | null> = Array.from(
    { length: groups.length },
    () => null,
  );

  let turnStartIndex: number | null = null;

  for (const [index, group] of groups.entries()) {
    if (group.type === "human") {
      turnStartIndex = null;
      continue;
    }

    turnStartIndex ??= index;

    const nextGroup = groups[index + 1];
    const isTurnEnd = !nextGroup || nextGroup.type === "human";

    if (!isTurnEnd) {
      continue;
    }

    usageMessagesByGroupIndex[index] = groups
      .slice(turnStartIndex, index + 1)
      .flatMap((currentGroup) => currentGroup.messages)
      .filter((message) => message.type === "ai");

    turnStartIndex = null;
  }

  return usageMessagesByGroupIndex;
}

type MessageMetadataLookup = (
  message: Message,
  index: number,
) => { streamMetadata?: Record<string, unknown> } | undefined;

export type StreamingMessageLookup = {
  ids: ReadonlySet<string>;
  messages: ReadonlySet<Message>;
};

export function getStreamingMessageLookup(
  messages: Message[],
  isStreaming: boolean,
  getMessagesMetadata?: MessageMetadataLookup,
): StreamingMessageLookup {
  const streamingMessageIds = new Set<string>();
  const streamingMessages = new Set<Message>();

  if (!isStreaming) {
    return {
      ids: streamingMessageIds,
      messages: streamingMessages,
    };
  }

  messages.forEach((message, index) => {
    if (!getMessagesMetadata?.(message, index)?.streamMetadata) {
      return;
    }

    if (typeof message.id === "string" && message.id.length > 0) {
      streamingMessageIds.add(message.id);
    }
    streamingMessages.add(message);
  });

  return {
    ids: streamingMessageIds,
    messages: streamingMessages,
  };
}

export function isAssistantMessageGroupStreaming(
  groupMessages: Message[],
  streamingMessages: StreamingMessageLookup,
) {
  return groupMessages.some((message) => {
    if (message.type !== "ai") {
      return false;
    }

    return (
      (typeof message.id === "string" &&
        message.id.length > 0 &&
        streamingMessages.ids.has(message.id)) ||
      streamingMessages.messages.has(message)
    );
  });
}

export function getAssistantTurnCopyData(
  messages: Message[],
  { isStreaming = false }: { isStreaming?: boolean } = {},
) {
  if (isStreaming) {
    return null;
  }

  return (
    [...messages]
      .reverse()
      .filter((message) => message.type === "ai")
      .map((message) => {
        const content = extractContentFromMessage(message);
        return content ?? extractReasoningContentFromMessage(message) ?? "";
      })
      .find((content) => content.length > 0) ?? null
  );
}

export function getMessageCopyData(message: Message) {
  const content = extractContentFromMessage(message);
  if (message.type === "human") {
    return stripUploadedFilesTag(content);
  }
  if (content.length > 0) {
    return content;
  }
  return extractReasoningContentFromMessage(message) ?? "";
}

export function extractTextFromMessage(message: Message) {
  if (typeof message.content === "string") {
    return (
      splitInlineReasoningFromAIMessage(message)?.content ??
      message.content.trim()
    );
  }
  if (Array.isArray(message.content)) {
    return message.content
      .map((content) =>
        typeof content === "string"
          ? content
          : content.type === "text"
            ? content.text
            : "",
      )
      .join("\n")
      .trim();
  }
  return "";
}

const THINK_OPEN_TAG = "<think>";
const THINK_TAG_RE = /<think>\s*([\s\S]*?)\s*<\/think>/g;
const inlineReasoningCache = new WeakMap<
  Message,
  { source: string; result: ReturnType<typeof splitInlineReasoning> }
>();

function splitInlineReasoning(content: string) {
  const reasoningParts: string[] = [];

  // First pass: strip every fully closed `<think>...</think>` pair and
  // collect its body as reasoning.
  let cleaned = content.replace(THINK_TAG_RE, (_, reasoning: string) => {
    const normalized = reasoning.trim();
    if (normalized) {
      reasoningParts.push(normalized);
    }
    return "";
  });

  // Streaming-safe pass: a `<think>` opener whose `</think>` has not arrived
  // yet means the rest of the chunk is reasoning in flight. Route it into the
  // reasoning slot instead of letting it render as message content (the
  // raw-HTML markdown pipeline would otherwise paint the inner text on
  // screen until the closing tag lands).
  //
  // Skip when the opener sits right after a backtick — that is the model
  // talking about `<think>` literally inside markdown inline code, not
  // actually streaming reasoning.
  const openTagIndex = cleaned.indexOf(THINK_OPEN_TAG);
  if (openTagIndex !== -1 && cleaned[openTagIndex - 1] !== "`") {
    const tail = cleaned.slice(openTagIndex + THINK_OPEN_TAG.length).trim();
    if (tail) {
      reasoningParts.push(tail);
    }
    cleaned = cleaned.slice(0, openTagIndex);
  }

  return {
    content: cleaned.trim(),
    reasoning: reasoningParts.length > 0 ? reasoningParts.join("\n\n") : null,
  };
}

function splitInlineReasoningFromAIMessage(message: Message) {
  if (message.type !== "ai" || typeof message.content !== "string") {
    return null;
  }
  const cached = inlineReasoningCache.get(message);
  if (cached?.source === message.content) {
    return cached.result;
  }
  const result = splitInlineReasoning(message.content);
  inlineReasoningCache.set(message, { source: message.content, result });
  return result;
}

export function extractContentFromMessage(message: Message) {
  if (typeof message.content === "string") {
    return (
      splitInlineReasoningFromAIMessage(message)?.content ??
      message.content.trim()
    );
  }
  if (Array.isArray(message.content)) {
    return message.content
      .map((content) => {
        if (typeof content === "string") {
          return content;
        }
        switch (content.type) {
          case "text":
            return content.text;
          case "image_url":
            const imageURL = extractURLFromImageURLContent(content.image_url);
            return `![image](${imageURL})`;
          default:
            return "";
        }
      })
      .join("\n")
      .trim();
  }
  return "";
}

export function extractReasoningContentFromMessage(message: Message) {
  if (message.type !== "ai") {
    return null;
  }
  if (
    message.additional_kwargs &&
    "reasoning_content" in message.additional_kwargs
  ) {
    return message.additional_kwargs.reasoning_content as string | null;
  }
  if (Array.isArray(message.content)) {
    const part = message.content[0];
    if (part && typeof part === "object" && "thinking" in part) {
      return part.thinking as string;
    }
  }
  if (typeof message.content === "string") {
    return splitInlineReasoningFromAIMessage(message)?.reasoning ?? null;
  }
  return null;
}

const MAX_REASONING_DURATION_MS = 24 * 60 * 60 * 1000;

/**
 * Read the Worker-observed lead-Agent reasoning interval.
 *
 * This value is deliberately independent from ``turn_duration``: it covers
 * only the model's reasoning-output window for this message, never tool,
 * subagent, queue, or whole-Run time.
 */
export function getReasoningDurationSeconds(
  message: Message,
): number | undefined {
  const value = message.additional_kwargs?.reasoning_duration_ms;
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0 ||
    value > MAX_REASONING_DURATION_MS
  ) {
    return undefined;
  }
  return Math.floor(value / 1000);
}

export function removeReasoningContentFromMessage(message: Message) {
  if (message.type !== "ai" || !message.additional_kwargs) {
    return;
  }
  delete message.additional_kwargs.reasoning_content;
}

export function extractURLFromImageURLContent(
  content:
    | string
    | {
        url: string;
      },
) {
  if (typeof content === "string") {
    return content;
  }
  return content.url;
}

export function hasContent(message: Message) {
  if (typeof message.content === "string") {
    return (
      (
        splitInlineReasoningFromAIMessage(message)?.content ??
        message.content.trim()
      ).length > 0
    );
  }
  if (Array.isArray(message.content)) {
    return message.content.length > 0;
  }
  return false;
}

export function hasReasoning(message: Message) {
  if (message.type !== "ai") {
    return false;
  }
  if (typeof message.additional_kwargs?.reasoning_content === "string") {
    return true;
  }
  if (Array.isArray(message.content)) {
    const part = message.content[0];
    // Compatible with the Anthropic gateway
    return (part as unknown as { type: "thinking" })?.type === "thinking";
  }
  if (typeof message.content === "string") {
    return splitInlineReasoning(message.content).reasoning !== null;
  }
  return false;
}

export function hasToolCalls(message: Message) {
  return (
    message.type === "ai" && message.tool_calls && message.tool_calls.length > 0
  );
}

export function hasPresentFiles(message: Message) {
  return (
    message.type === "ai" &&
    message.tool_calls?.some((toolCall) => toolCall.name === "present_files")
  );
}

export function isClarificationToolMessage(message: Message) {
  return message.type === "tool" && message.name === "ask_clarification";
}

export function extractPresentFilesFromMessage(message: Message) {
  if (message.type !== "ai" || !hasPresentFiles(message)) {
    return [];
  }
  const files: string[] = [];
  for (const toolCall of message.tool_calls ?? []) {
    if (
      toolCall.name === "present_files" &&
      Array.isArray(toolCall.args.filepaths)
    ) {
      files.push(...(toolCall.args.filepaths as string[]));
    }
  }
  return files;
}

export function hasSubagent(message: AIMessage) {
  for (const toolCall of message.tool_calls ?? []) {
    if (toolCall.name === "task") {
      return true;
    }
  }
  return false;
}

export function findToolCallResult(toolCallId: string, messages: Message[]) {
  for (const message of messages) {
    if (message.type === "tool" && message.tool_call_id === toolCallId) {
      const content = extractTextFromMessage(message);
      if (content) {
        return content;
      }
    }
  }
  return undefined;
}

export function isHiddenFromUIMessage(message: Message) {
  if (message.additional_kwargs?.hide_from_ui === true) return true;
  if (
    typeof message.name === "string" &&
    HIDDEN_CONTROL_MESSAGE_NAMES.has(message.name)
  ) {
    return true;
  }
  if (message.type !== "human") return false;
  const content = extractTextFromMessage(message);
  return (
    content.includes("<slash_skill_activation>") &&
    stripUploadedFilesTag(content).length === 0
  );
}

/**
 * Represents a file stored in message additional_kwargs.files.
 * Used for optimistic UI (uploading state) and structured file metadata.
 */
export interface FileInMessage {
  file_id?: string; // opaque private file-version id; required once uploaded
  filename: string;
  size: number; // bytes
  path?: string; // virtual path, may not be set during upload
  status?: "uploading" | "uploaded";
}

/**
 * Strip backend-injected human context tags from message content.
 * Kept under its historical name because callers use it for uploaded-file
 * display cleanup.
 */
export function stripUploadedFilesTag(content: string): string {
  return content
    .replace(
      /<(uploaded_files|current_uploads|slash_skill_activation)>[\s\S]*?<\/\1>/g,
      "",
    )
    .trim();
}

/**
 * Tag names that backend middlewares wrap around internal payloads before
 * letting them ride along inside LangGraph message ``content``.
 *
 * These markers are *not* user copy — they come from:
 *
 * - ``UploadsMiddleware`` → ``<uploaded_files>``
 * - ``SkillActivationMiddleware`` → ``<slash_skill_activation>``
 * - ``DynamicContextMiddleware`` → ``<system-reminder>`` (carrying
 *   ``<memory>`` / ``<current_date>`` inside)
 * - ``TodoListMiddleware`` / ``LoopDetectionMiddleware`` style reminders
 *   live in ``hide_from_ui`` HumanMessages, but their inner payload uses
 *   the same tag vocabulary.
 *
 * The primary export filter is {@link isHiddenFromUIMessage}. This list is
 * the defence-in-depth strip for any message that — by middleware bug,
 * provider quirk, or merge-conflict regression — slips through without
 * its ``hide_from_ui`` flag set.
 */
export const INTERNAL_MARKER_TAGS = [
  "uploaded_files",
  "current_uploads",
  "slash_skill_activation",
  "system-reminder",
  "memory",
  "current_date",
] as const;

const INTERNAL_MARKER_RE = new RegExp(
  `<(${INTERNAL_MARKER_TAGS.join("|")})>[\\s\\S]*?</\\1>`,
  "g",
);

/**
 * Strip every known backend-injected marker from message content.
 *
 * Intended for the chat export path where a marker leaking through is a
 * privacy regression. UI render paths should keep using
 * {@link stripUploadedFilesTag} — they receive ``hide_from_ui`` messages
 * via a separate filter and the narrower function avoids stripping content
 * a user might legitimately type into a meta-discussion (e.g. asking the
 * model about its own ``<memory>`` system).
 */
export function stripInternalMarkers(content: string): string {
  return content.replace(INTERNAL_MARKER_RE, "").trim();
}

export function parseUploadedFiles(content: string): FileInMessage[] {
  const uploadedFilesRegex =
    /<(?:uploaded_files|current_uploads)>([\s\S]*?)<\/(?:uploaded_files|current_uploads)>/;
  // eslint-disable-next-line @typescript-eslint/prefer-regexp-exec
  const match = content.match(uploadedFilesRegex);

  if (!match) {
    return [];
  }

  const uploadedFilesContent = match[1];

  // Check if it's "No files have been uploaded yet."
  if (uploadedFilesContent?.includes("No files have been uploaded yet.")) {
    return [];
  }

  // Check if the backend reported no new files were uploaded in this message
  if (uploadedFilesContent?.includes("(empty)")) {
    return [];
  }

  // Parse file list
  // Format: - filename (size)\n  Path: /path/to/file
  const fileRegex = /- ([^\n(]+)\s*\(([^)]+)\)\s*\n\s*Path:\s*([^\n]+)/g;
  const files: FileInMessage[] = [];
  let fileMatch;

  while ((fileMatch = fileRegex.exec(uploadedFilesContent ?? "")) !== null) {
    files.push({
      filename: fileMatch[1].trim(),
      size: parseHumanFileSize(fileMatch[2].trim()),
      path: fileMatch[3].trim(),
    });
  }

  return files;
}

function parseHumanFileSize(value: string) {
  const match = /^([\d.]+)\s*(B|KB|MB|GB|TB)?$/i.exec(value.trim());
  if (!match) return 0;
  const amount = Number.parseFloat(match[1] ?? "");
  if (!Number.isFinite(amount)) return 0;
  const unit = (match[2] ?? "B").toUpperCase();
  const multiplier =
    {
      B: 1,
      KB: 1024,
      MB: 1024 ** 2,
      GB: 1024 ** 3,
      TB: 1024 ** 4,
    }[unit] ?? 1;
  return Math.round(amount * multiplier);
}
