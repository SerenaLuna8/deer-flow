import type { Message, Run } from "@langchain/langgraph-sdk";

import { isHiddenFromUIMessage } from "../messages/utils";
import type { ReadyPromptInputFile } from "../uploads/prompt-input-files";

import { textOfMessage } from "./utils";

const SUMMARIZATION_MIDDLEWARE_UPDATE_KEYS = new Set([
  "SummarizationMiddleware.before_model",
  "DeerFlowSummarizationMiddleware.before_model",
]);

function isNonEmptyString(value: string | undefined): value is string {
  return typeof value === "string" && value.length > 0;
}

export function messageRunId(message: Message): string | undefined {
  const directRunId = Reflect.get(message, "run_id");
  if (typeof directRunId === "string" && directRunId.length > 0) {
    return directRunId;
  }
  const additionalRunId = message.additional_kwargs?.run_id;
  return typeof additionalRunId === "string" && additionalRunId.length > 0
    ? additionalRunId
    : undefined;
}

export function filterMessagesBySupersededRunIds(
  messages: Message[],
  supersededRunIds: ReadonlySet<string>,
): Message[] {
  if (supersededRunIds.size === 0) {
    return messages;
  }
  return messages.filter((message) => {
    const runId = messageRunId(message);
    return !runId || !supersededRunIds.has(runId);
  });
}

export function resolveActiveRunIdForMessages(
  messages: Message[],
  isRunLoading: boolean,
  explicitRunId: string | null,
): string | null {
  if (explicitRunId) {
    return explicitRunId;
  }
  if (!isRunLoading) {
    return null;
  }
  return (
    [...messages]
      .reverse()
      .map(messageRunId)
      .find((runId): runId is string => Boolean(runId)) ?? null
  );
}

export function messageIdentity(message: Message): string | undefined {
  if (
    "tool_call_id" in message &&
    typeof message.tool_call_id === "string" &&
    message.tool_call_id.length > 0
  ) {
    const runId = messageRunId(message);
    return runId
      ? `run:${runId}\u0000tool:${message.tool_call_id}`
      : `tool:${message.tool_call_id}`;
  }
  if (typeof message.id === "string" && message.id.length > 0) {
    return `message:${message.id}`;
  }
  return undefined;
}

export type RegenerationTarget = {
  messageId: string;
  supersededMessageIds: string[];
};

function hasToolPayload(value: unknown): boolean {
  return (
    value !== null &&
    value !== undefined &&
    (!Array.isArray(value) || value.length > 0)
  );
}

function hasRetryUnsafeToolActivity(message: Message): boolean {
  if (message.type === "tool") {
    return true;
  }
  if (message.type !== "ai") {
    return false;
  }
  const toolCalls = Reflect.get(message, "tool_calls");
  const invalidToolCalls = Reflect.get(message, "invalid_tool_calls");
  const toolCallChunks = Reflect.get(message, "tool_call_chunks");
  const additionalToolCalls = message.additional_kwargs?.tool_calls;
  const additionalFunctionCall = message.additional_kwargs?.function_call;
  return (
    hasToolPayload(toolCalls) ||
    hasToolPayload(invalidToolCalls) ||
    hasToolPayload(toolCallChunks) ||
    hasToolPayload(additionalToolCalls) ||
    hasToolPayload(additionalFunctionCall)
  );
}

export function getLatestRegenerationTarget(
  messages: readonly Message[],
  targetRunId: string,
): RegenerationTarget | null {
  const candidates = messages.filter(
    (message) => messageRunId(message) === targetRunId,
  );
  if (candidates.some(hasRetryUnsafeToolActivity)) {
    return null;
  }
  const target = [...candidates]
    .reverse()
    .find((message) => message.type === "ai" && message.id);
  if (!target?.id) {
    return null;
  }
  const supersededMessageIds = candidates
    .filter((message) => message.type === "ai" && message.id)
    .map((message) => message.id)
    .filter((id): id is string => typeof id === "string" && id.length > 0);

  return {
    messageId: target.id,
    supersededMessageIds,
  };
}

export function hasAcknowledgedOptimisticHuman(
  optimisticMessages: Message[],
  canonicalMessages: Message[],
): boolean {
  // DynamicContextMiddleware keeps the submitted id on its hidden reminder
  // and moves the visible canonical user message to `<submitted-id>__user`.
  // Match only this server-owned id relationship; equal text can be a distinct
  // user turn and must never acknowledge an optimistic message.
  return (
    retainUnacknowledgedOptimisticHumanMessages(
      optimisticMessages,
      canonicalMessages,
    ).length !== optimisticMessages.length
  );
}

export function retainUnacknowledgedOptimisticHumanMessages(
  optimisticMessages: Message[],
  canonicalMessages: Message[],
): Message[] {
  const canonicalHumanIds = new Set(
    canonicalMessages.flatMap((message) => {
      const id = message.id;
      return message.type === "human" &&
        !isHiddenFromUIMessage(message) &&
        typeof id === "string" &&
        id.length > 0
        ? [id]
        : [];
    }),
  );
  return optimisticMessages.filter((message) => {
    const id = message.id;
    return !(
      message.type === "human" &&
      typeof id === "string" &&
      id.length > 0 &&
      (canonicalHumanIds.has(id) || canonicalHumanIds.has(`${id}__user`))
    );
  });
}

export function retainOptimisticHumanMessagesAfterFailure(
  optimisticMessages: Message[],
  runId?: string,
): Message[] {
  return optimisticMessages
    .filter((message) => message.type === "human")
    .map((message) =>
      runId && !messageRunId(message)
        ? ({ ...message, run_id: runId } as Message)
        : message,
    );
}

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

function restorableReadyFiles(message: Message): ReadyPromptInputFile[] {
  const value = message.additional_kwargs?.files;
  if (!Array.isArray(value)) return [];

  const seenIds = new Set<string>();
  return value.flatMap((candidate) => {
    if (!candidate || typeof candidate !== "object") return [];
    const fileId = Reflect.get(candidate, "file_id");
    const filename = Reflect.get(candidate, "filename");
    const size = Reflect.get(candidate, "size");
    const path = Reflect.get(candidate, "path");
    if (
      typeof fileId !== "string" ||
      !UUID_PATTERN.test(fileId) ||
      seenIds.has(fileId) ||
      typeof filename !== "string" ||
      filename.length === 0 ||
      typeof size !== "number" ||
      !Number.isSafeInteger(size) ||
      size < 0 ||
      (path !== undefined && typeof path !== "string")
    ) {
      return [];
    }
    seenIds.add(fileId);
    return [
      {
        file_id: fileId,
        filename,
        size,
        ...(typeof path === "string" && path.length > 0 ? { path } : {}),
        status: "uploaded" as const,
      },
    ];
  });
}

export type FailedRunComposerInput = {
  runId: string;
  messageId: string;
  text: string;
  files: ReadyPromptInputFile[];
};

export function resolveFailedRunComposerInput(
  messages: readonly Message[],
  runId: string,
): FailedRunComposerInput | null {
  if (!runId) return null;
  const message = [...messages]
    .reverse()
    .find(
      (candidate) =>
        candidate.type === "human" &&
        !isHiddenFromUIMessage(candidate) &&
        messageRunId(candidate) === runId,
    );
  if (!message || typeof message.id !== "string" || message.id.length === 0) {
    return null;
  }
  const text = textOfMessage(message) ?? "";
  const files = restorableReadyFiles(message);
  if (!text.trim() && files.length === 0) return null;
  return { runId, messageId: message.id, text, files };
}

function isRunAdmissionMessage(message: Message): boolean {
  if (message.type !== "human") {
    return false;
  }
  if (Reflect.get(message, "run_message_source") === "run_admission") {
    return true;
  }
  const runId = messageRunId(message);
  return Boolean(runId && message.id === `run-admission-${runId}`);
}

export function attachRunIdToNewMessages(
  messages: Message[],
  runId: string | null,
  baselineMessageIds: ReadonlySet<string>,
): Message[] {
  if (!runId) return messages;
  return messages.map((message) => {
    const identity = messageIdentity(message);
    const existingRunId = Reflect.get(message, "run_id");
    if (
      typeof existingRunId === "string" ||
      !identity ||
      baselineMessageIds.has(identity)
    ) {
      return message;
    }
    return { ...message, run_id: runId } as unknown as Message;
  });
}

/**
 * Snapshot the visible, durably identifiable messages created by a Run before
 * the SDK releases its terminal live projection. The caller keeps this small
 * bridge only until the Run journal returns the same message identities.
 */
export function captureTerminalRunMessages(
  messages: Message[],
  runId: string | null,
  baselineMessageIds: ReadonlySet<string>,
): Message[] {
  if (!runId) return [];

  return dedupeMessagesByIdentity(
    attachRunIdToNewMessages(messages, runId, baselineMessageIds).filter(
      (message) =>
        !isHiddenFromUIMessage(message) &&
        messageIdentity(message) !== undefined &&
        messageRunId(message) === runId,
    ),
  );
}

export function dedupeMessagesByIdentity(messages: Message[]): Message[] {
  const firstIndexByIdentity = new Map<string, number>();
  const lastIndexByIdentity = new Map<string, number>();
  const lastVisibleIndexByIdentity = new Map<string, number>();

  // This is a UI-display dedupe rule, not a general LangChain message-stream
  // contract. Hidden messages that share an identity with a visible message are
  // treated as control messages for this merged view; hidden messages carrying
  // independent tracing/task semantics should use a distinct id or a custom
  // stream/state channel instead of relying on message dedupe preservation.
  const preservedTurnDurations = new Map<string, number>();
  const preservedReasoningDurations = new Map<string, number>();
  const preservedRunIds = new Map<string, string>();
  messages.forEach((message, index) => {
    const identity = messageIdentity(message);
    if (identity) {
      if (!firstIndexByIdentity.has(identity)) {
        firstIndexByIdentity.set(identity, index);
      }
      lastIndexByIdentity.set(identity, index);
      if (!isHiddenFromUIMessage(message)) {
        lastVisibleIndexByIdentity.set(identity, index);
      }
      if (message.additional_kwargs?.turn_duration !== undefined) {
        preservedTurnDurations.set(
          identity,
          message.additional_kwargs.turn_duration as number,
        );
      }
      const reasoningDuration =
        message.additional_kwargs?.reasoning_duration_ms;
      if (
        typeof reasoningDuration === "number" &&
        Number.isSafeInteger(reasoningDuration)
      ) {
        preservedReasoningDurations.set(identity, reasoningDuration);
      }
      const runId = Reflect.get(message, "run_id");
      if (typeof runId === "string" && runId.length > 0) {
        preservedRunIds.set(identity, runId);
      }
    }
  });

  const selectedMessageByIdentity = new Map<string, Message>();
  for (const [identity, lastIndex] of lastIndexByIdentity) {
    // Keep the latest visible payload, but render it in the first identity
    // slot. A stale live duplicate must not move an older HumanMessage behind
    // a later clarification request during asynchronous history hydration.
    const selectedIndex = lastVisibleIndexByIdentity.get(identity) ?? lastIndex;
    const selected = messages[selectedIndex];
    if (selected) {
      selectedMessageByIdentity.set(identity, selected);
    }
  }

  return messages
    .filter((message, index) => {
      const identity = messageIdentity(message);
      if (!identity) {
        return true;
      }
      return firstIndexByIdentity.get(identity) === index;
    })
    .map((message) => {
      const identity = messageIdentity(message);
      if (!identity) return message;
      const selectedMessage =
        selectedMessageByIdentity.get(identity) ?? message;
      const preserveDuration =
        preservedTurnDurations.has(identity) &&
        selectedMessage.additional_kwargs?.turn_duration === undefined;
      const preserveReasoningDuration =
        preservedReasoningDurations.has(identity) &&
        selectedMessage.additional_kwargs?.reasoning_duration_ms === undefined;
      const preserveRunId =
        preservedRunIds.has(identity) &&
        typeof Reflect.get(selectedMessage, "run_id") !== "string";
      if (!preserveDuration && !preserveReasoningDuration && !preserveRunId) {
        return selectedMessage;
      }
      return {
        ...selectedMessage,
        ...(preserveRunId ? { run_id: preservedRunIds.get(identity) } : {}),
        ...(preserveDuration || preserveReasoningDuration
          ? {
              additional_kwargs: {
                ...selectedMessage.additional_kwargs,
                ...(preserveDuration
                  ? {
                      turn_duration: preservedTurnDurations.get(identity),
                    }
                  : {}),
                ...(preserveReasoningDuration
                  ? {
                      reasoning_duration_ms:
                        preservedReasoningDurations.get(identity),
                    }
                  : {}),
              },
            }
          : {}),
      } as Message;
    });
}

function attachRunIdWithinKnownTurns(messages: Message[]): Message[] {
  let currentRunId: string | undefined;

  return messages.map((message) => {
    const explicitRunId = messageRunId(message);
    if (explicitRunId) {
      currentRunId = explicitRunId;
      return message;
    }

    if (message.type === "human" && !isHiddenFromUIMessage(message)) {
      currentRunId = undefined;
      return message;
    }

    if (!currentRunId) {
      return message;
    }
    return { ...message, run_id: currentRunId } as unknown as Message;
  });
}

function projectOriginalUserContent(message: Message): Message {
  if (message.type !== "human") {
    return message;
  }
  const originalContent = message.additional_kwargs?.original_user_content;
  if (typeof originalContent !== "string") {
    return message;
  }
  return {
    ...message,
    content: [{ type: "text", text: originalContent }],
  } as Message;
}

function repairInjectedUserOrder(messages: Message[]): Message[] {
  let repaired = [...messages];
  const reminderIds = repaired.flatMap((message) => {
    const id = message.id;
    return typeof id === "string" &&
      id.length > 0 &&
      message.additional_kwargs?.hide_from_ui === true &&
      message.additional_kwargs?.dynamic_context_reminder === true
      ? [id]
      : [];
  });

  for (const reminderId of reminderIds) {
    const userId = `${reminderId}__user`;
    const memoryId = `${reminderId}__memory`;
    const injectedUser = repaired.find(
      (message) => message.type === "human" && message.id === userId,
    );
    if (!injectedUser) {
      continue;
    }
    const injectedMemory = repaired.filter(
      (message) => message.id === memoryId,
    );
    const withoutInjected = repaired.filter(
      (message) => message.id !== userId && message.id !== memoryId,
    );
    const reminderIndex = withoutInjected.findIndex(
      (message) => message.id === reminderId,
    );
    if (reminderIndex < 0) {
      continue;
    }
    withoutInjected.splice(
      reminderIndex + 1,
      0,
      ...injectedMemory,
      injectedUser,
    );
    repaired = withoutInjected;
  }
  return repaired;
}

function alignSanitizedHistoryUserIds(
  historyMessages: Message[],
  threadMessages: Message[],
): Message[] {
  const reminderIds = new Set(
    threadMessages.flatMap((message) => {
      const id = message.id;
      return typeof id === "string" &&
        message.additional_kwargs?.hide_from_ui === true &&
        message.additional_kwargs?.dynamic_context_reminder === true
        ? [id]
        : [];
    }),
  );
  const injectedUserIds = new Set(
    threadMessages.flatMap((message) => {
      const id = message.id;
      if (
        message.type !== "human" ||
        typeof id !== "string" ||
        !id.endsWith("__user")
      ) {
        return [];
      }
      const reminderId = id.slice(0, -"__user".length);
      return reminderIds.has(reminderId) ? [id] : [];
    }),
  );

  return historyMessages.map((message) => {
    const projected = projectOriginalUserContent(message);
    if (
      projected.type !== "human" ||
      typeof projected.id !== "string" ||
      typeof projected.additional_kwargs?.original_user_content !== "string"
    ) {
      return projected;
    }
    const injectedId = `${projected.id}__user`;
    return injectedUserIds.has(injectedId)
      ? ({ ...projected, id: injectedId } as Message)
      : projected;
  });
}

export function mergeMessages(
  historyMessages: Message[],
  threadMessages: Message[],
  optimisticMessages: Message[],
  runsNewestFirst: Run[] = [],
): Message[] {
  // Only visible live messages should trim overlapping history. Hidden messages
  // are UI control messages in this path, not observability records; any hidden
  // message that must survive as task/tracing data should use custom events or a
  // separate state channel instead of participating in this overlap heuristic.

  // Materialized checkpoint state carries the owning run_id on each admitted
  // HumanMessage, while its following AI/Tool messages can remain unscoped.
  // Propagate that trusted turn boundary before comparing it with the
  // run-scoped history journal. Otherwise the same ToolMessage has two
  // identities (`run:<id>+tool:<id>` in history versus `tool:<id>` live),
  // survives dedupe twice, and a partially hydrated latest clarification can
  // appear before an older checkpoint HumanMessage.
  const scopedThreadMessages = attachRunIdWithinKnownTurns(
    repairInjectedUserOrder(threadMessages),
  );
  const projectedHistoryMessages = alignSanitizedHistoryUserIds(
    historyMessages,
    scopedThreadMessages,
  );
  const visibleOptimisticMessages = retainUnacknowledgedOptimisticHumanMessages(
    optimisticMessages,
    [...projectedHistoryMessages, ...scopedThreadMessages],
  );
  const threadMessageIds = new Set(
    scopedThreadMessages
      .filter((message) => !isHiddenFromUIMessage(message))
      .map(messageIdentity)
      .filter(isNonEmptyString),
  );
  const visibleLiveHumanRunIds = new Set(
    scopedThreadMessages
      .filter(
        (message) =>
          message.type === "human" && !isHiddenFromUIMessage(message),
      )
      .map(messageRunId)
      .filter(isNonEmptyString),
  );
  const admissionMessages: Message[] = [];
  const regularHistoryMessages = projectedHistoryMessages.filter((message) => {
    if (!isRunAdmissionMessage(message)) {
      return true;
    }
    const identity = messageIdentity(message);
    const runId = messageRunId(message);
    if (
      (identity && threadMessageIds.has(identity)) ||
      (runId && visibleLiveHumanRunIds.has(runId))
    ) {
      return false;
    }
    admissionMessages.push(message);
    return false;
  });

  const savedTurnDurations = new Map<string, number>();
  const savedReasoningDurations = new Map<string, number>();
  const savedRunIds = new Map<string, string>();
  for (const msg of projectedHistoryMessages) {
    const identity = messageIdentity(msg);
    if (identity && msg.additional_kwargs?.turn_duration !== undefined) {
      savedTurnDurations.set(
        identity,
        msg.additional_kwargs.turn_duration as number,
      );
    }
    const reasoningDuration = msg.additional_kwargs?.reasoning_duration_ms;
    if (
      identity &&
      typeof reasoningDuration === "number" &&
      Number.isSafeInteger(reasoningDuration)
    ) {
      savedReasoningDurations.set(identity, reasoningDuration);
    }
    const runId = Reflect.get(msg, "run_id");
    if (identity && typeof runId === "string" && runId.length > 0) {
      savedRunIds.set(identity, runId);
    }
  }

  // The overlap is a contiguous suffix of historyMessages (newest history == oldest thread).
  // Scan from the end: shrink cutoff while messages are already in thread, stop as soon as
  // we hit one that isn't — everything before that point is non-overlapping.
  let cutoff = regularHistoryMessages.length;
  for (let i = regularHistoryMessages.length - 1; i >= 0; i--) {
    const msg = regularHistoryMessages[i];
    if (!msg) {
      continue;
    }
    const identity = messageIdentity(msg);
    if (identity && threadMessageIds.has(identity)) {
      cutoff = i;
    } else {
      break;
    }
  }

  const merged = dedupeMessagesByIdentity([
    ...regularHistoryMessages.slice(0, cutoff),
    ...scopedThreadMessages,
    ...visibleOptimisticMessages,
  ]);
  const chronologicalRunOrder = new Map<string, number>();
  [...runsNewestFirst]
    .reverse()
    .forEach((run, index) => chronologicalRunOrder.set(run.run_id, index));
  const orderedAdmissionMessages = admissionMessages
    .map((message, index) => ({ message, index }))
    .sort((left, right) => {
      const leftOrder = chronologicalRunOrder.get(
        messageRunId(left.message) ?? "",
      );
      const rightOrder = chronologicalRunOrder.get(
        messageRunId(right.message) ?? "",
      );
      if (leftOrder === undefined || rightOrder === undefined) {
        return left.index - right.index;
      }
      return leftOrder - rightOrder || left.index - right.index;
    })
    .map(({ message }) => message);

  for (const admissionMessage of orderedAdmissionMessages) {
    const identity = messageIdentity(admissionMessage);
    if (
      identity &&
      merged.some((message) => messageIdentity(message) === identity)
    ) {
      continue;
    }
    const admissionRunOrder = chronologicalRunOrder.get(
      messageRunId(admissionMessage) ?? "",
    );
    const insertionIndex =
      admissionRunOrder === undefined
        ? -1
        : merged.findIndex((message) => {
            const messageOrder = chronologicalRunOrder.get(
              messageRunId(message) ?? "",
            );
            return (
              // A Run-admission HumanMessage is the first message of its Run.
              // This matters after compaction: the checkpoint can retain only
              // the Run's tail while the complete journal still owns the
              // admission row. Insert before the same Run as well as later
              // Runs, otherwise the admission prompt is appended after its
              // own AI/history rows on refresh.
              messageOrder !== undefined && messageOrder >= admissionRunOrder
            );
          });
    if (insertionIndex === -1) {
      merged.push(admissionMessage);
    } else {
      merged.splice(insertionIndex, 0, admissionMessage);
    }
  }

  return merged.map((message) => {
    const identity = messageIdentity(message);
    if (!identity) return message;
    const preserveDuration =
      savedTurnDurations.has(identity) &&
      message.additional_kwargs?.turn_duration === undefined;
    const preserveReasoningDuration =
      savedReasoningDurations.has(identity) &&
      message.additional_kwargs?.reasoning_duration_ms === undefined;
    const preserveRunId =
      savedRunIds.has(identity) &&
      typeof Reflect.get(message, "run_id") !== "string";
    if (!preserveDuration && !preserveReasoningDuration && !preserveRunId) {
      return message;
    }
    return {
      ...message,
      ...(preserveRunId ? { run_id: savedRunIds.get(identity) } : {}),
      ...(preserveDuration || preserveReasoningDuration
        ? {
            additional_kwargs: {
              ...message.additional_kwargs,
              ...(preserveDuration
                ? {
                    turn_duration: savedTurnDurations.get(identity),
                  }
                : {}),
              ...(preserveReasoningDuration
                ? {
                    reasoning_duration_ms:
                      savedReasoningDurations.get(identity),
                  }
                : {}),
            },
          }
        : {}),
    } as Message;
  });
}

/**
 * Overlay the UI-safe thread projection without eagerly evaluating SDK
 * getters.
 *
 * LangGraph's stream handle exposes enumerable getters such as `toolCalls`.
 * During a `RemoveMessage(__remove_all__)` compaction transition its internal
 * message array can briefly be sparse while message-tuple indexes are rebuilt.
 * Object spread would invoke every enumerable getter immediately, and the SDK
 * tool-call getter then reads `.type` from a sparse slot and crashes the page.
 * Copying property descriptors preserves the lazy SDK surface while the three
 * fields consumed by ActWeave use the already-normalized UI projection.
 */
export function overlayThreadProjection<T extends object>(
  thread: T,
  overrides: Record<string, unknown>,
): T {
  const overrideDescriptors = Object.fromEntries(
    Object.entries(overrides).map(([key, value]) => [
      key,
      {
        configurable: true,
        enumerable: true,
        value,
        writable: false,
      },
    ]),
  );
  return Object.create(Object.getPrototypeOf(thread), {
    ...Object.getOwnPropertyDescriptors(thread),
    ...overrideDescriptors,
  }) as T;
}

/**
 * Derive the live turns that context summarization is about to drop and that
 * therefore must be re-archived into history.
 *
 * Summarization emits `RemoveMessage(ALL)` + a hidden summary + the retained
 * tail. Everything in the current live thread before the first retained visible
 * message is being removed; we keep those (minus the summary control messages
 * already tracked) so the UI can still show the full conversation (#3825).
 */
export function computeSummarizationMovedMessages(
  currentMessages: Message[],
  summarizationMessages: Message[],
  summarizedMessageIds: ReadonlySet<string>,
): Message[] {
  const firstRetainedVisibleIdentity = summarizationMessages
    .filter((message) => message.type !== "remove")
    .filter((message) => !isHiddenFromUIMessage(message))
    .map(messageIdentity)
    .find(isNonEmptyString);

  const moved: Message[] = [];
  for (const message of currentMessages) {
    if (
      firstRetainedVisibleIdentity &&
      messageIdentity(message) === firstRetainedVisibleIdentity
    ) {
      break;
    }
    if (!summarizedMessageIds.has(message.id ?? "")) {
      moved.push(message);
    }
  }
  return moved;
}

/**
 * Overlay the messages rescued from context summarization on top of the
 * (possibly stale) visible history so the merged view never drops them.
 *
 * Background (#3825): after summarization the backend removes every live
 * message (`RemoveMessage(ALL)`) and `onUpdateEvent` re-archives the removed
 * messages into history through an async `setState`. The live thread messages
 * are owned by the LangGraph SDK external store while the archived history is
 * React state, so a render can observe the post-summary (shrunk) thread before
 * the archive `setState` commits — leaving the rescued messages in neither
 * merge input. Reading them from a synchronous buffer here keeps the merge
 * correct at every render regardless of how the two state channels interleave.
 *
 * The rescued messages are the oldest live turns, so they follow whatever the
 * already-loaded history holds. Only messages still missing from history are
 * appended: once history absorbs a rescued message, its live copy stays
 * authoritative (the buffered copy is an older snapshot and must never overwrite
 * it), and ordering is preserved.
 */
export function resolvePreservedHistory(
  visibleHistory: Message[],
  pendingArchivedMessages: Message[],
): Message[] {
  if (pendingArchivedMessages.length === 0) {
    return visibleHistory;
  }
  const presentIdentities = new Set(
    visibleHistory.map(messageIdentity).filter(isNonEmptyString),
  );
  const missing = pendingArchivedMessages.filter((message) => {
    const identity = messageIdentity(message);
    // Identity-less messages are intentionally skipped: without a stable
    // identity they cannot be matched against history to drain or dedupe, so
    // overlaying them would risk a permanent duplicate. They are still archived
    // through appendMessages and surface via the normal history path instead.
    return identity !== undefined && !presentIdentities.has(identity);
  });
  if (missing.length === 0) {
    return visibleHistory;
  }
  return [...visibleHistory, ...missing];
}

export type ThreadMessageProjectionInput = {
  threadId: string | null | undefined;
  visibleHistory: Message[];
  pendingArchivedMessages: Message[];
  pendingArchiveThreadId: string | null;
  renderMessages: Message[];
  activeRunId: string | null;
  runBaselineMessageIds: ReadonlySet<string>;
  pendingSupersededRunIds: ReadonlySet<string>;
  visibleOptimisticMessages: Message[];
  historyRuns?: Run[];
};

/**
 * Pure long-session display projection. Keeping every input explicit makes the
 * 100/1k/10k benchmark reproducible and gives React one semantic memo boundary
 * without changing history, replay, or stream ownership.
 */
export function projectThreadMessages({
  threadId,
  visibleHistory,
  pendingArchivedMessages,
  pendingArchiveThreadId,
  renderMessages,
  activeRunId,
  runBaselineMessageIds,
  pendingSupersededRunIds,
  visibleOptimisticMessages,
  historyRuns,
}: ThreadMessageProjectionInput): Message[] {
  const effectiveHistory =
    pendingArchivedMessages.length > 0 && pendingArchiveThreadId === threadId
      ? resolvePreservedHistory(visibleHistory, pendingArchivedMessages)
      : visibleHistory;
  const baselineMessageIds = new Set(runBaselineMessageIds);
  for (const message of effectiveHistory) {
    const identity = messageIdentity(message);
    if (identity) {
      baselineMessageIds.add(identity);
    }
  }
  const runScopedPersistedMessages = attachRunIdToNewMessages(
    renderMessages,
    activeRunId,
    baselineMessageIds,
  );
  const visibleRunScopedPersistedMessages = filterMessagesBySupersededRunIds(
    runScopedPersistedMessages,
    pendingSupersededRunIds,
  );
  return mergeMessages(
    effectiveHistory,
    visibleRunScopedPersistedMessages,
    visibleOptimisticMessages,
    historyRuns,
  );
}

/**
 * Drop the archive-buffer entries that the canonical history state has already
 * absorbed. This keeps the buffer a transient bridge across the async gap
 * rather than a second long-lived source of truth — otherwise a stale copy
 * could resurrect a message that history later filtered out (e.g. a superseded
 * or regenerated run).
 */
export function pruneConfirmedArchivedMessages(
  pendingArchivedMessages: Message[],
  visibleHistory: Message[],
): Message[] {
  if (pendingArchivedMessages.length === 0) {
    return pendingArchivedMessages;
  }
  const confirmedIdentities = new Set(
    visibleHistory.map(messageIdentity).filter(isNonEmptyString),
  );
  return pendingArchivedMessages.filter((message) => {
    const identity = messageIdentity(message);
    return !identity || !confirmedIdentities.has(identity);
  });
}

export function getMessagesAfterBaseline(
  messages: Message[],
  baselineMessageIds: ReadonlySet<string>,
): Message[] {
  return messages.filter((message) => {
    const id = messageIdentity(message);
    return !id || !baselineMessageIds.has(id);
  });
}

export function countHumanMessagesExcludingSuperseded(
  messages: Message[],
  supersededMessageIds: readonly string[],
): number {
  const superseded = new Set(supersededMessageIds);
  return messages.filter(
    (message) =>
      message.type === "human" && (!message.id || !superseded.has(message.id)),
  ).length;
}

export function getVisibleOptimisticMessages(
  optimisticMessages: Message[],
  previousHumanMessageCount: number,
  currentHumanMessageCount: number,
): Message[] {
  if (
    optimisticMessages.some((message) => message.type === "human") &&
    currentHumanMessageCount > previousHumanMessageCount
  ) {
    return [];
  }
  return optimisticMessages;
}

export function getSummarizationMiddlewareMessages(
  data: unknown,
): Message[] | undefined {
  if (typeof data !== "object" || data === null) {
    return undefined;
  }

  for (const [key, update] of Object.entries(data)) {
    if (!SUMMARIZATION_MIDDLEWARE_UPDATE_KEYS.has(key)) {
      continue;
    }
    if (typeof update !== "object" || update === null) {
      continue;
    }

    const messages = Reflect.get(update, "messages");
    if (Array.isArray(messages)) {
      return [...messages] as Message[];
    }
  }

  return undefined;
}
