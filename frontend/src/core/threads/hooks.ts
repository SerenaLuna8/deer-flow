import type { AIMessage, Message, Run } from "@langchain/langgraph-sdk";
import type { ThreadsClient } from "@langchain/langgraph-sdk/client";
import { useStream } from "@langchain/langgraph-sdk/react";
import {
  type QueryClient,
  type InfiniteData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import type { PromptInputMessage } from "@/components/ai-elements/prompt-input";

import { fetch } from "../api/fetcher";
import { useI18n } from "../i18n/hooks";
import { isHiddenFromUIMessage } from "../messages/utils";
import type { FileInMessage } from "../messages/utils";
import { isProjectRunTerminalFailure } from "../private-work/api-client";
import { usePrivateWorkAccess } from "../private-work/provider";
import { privateWorkQueryKey } from "../private-work/query-keys";
import {
  runPrivateWorkAbortable,
  type ProjectClientScope,
  type ProjectPrivateWorkScope,
} from "../private-work/types";
import type { LocalSettings } from "../settings";
import { isSidecarThread, SIDECAR_METADATA_KEY } from "../sidecar/thread";
import { useUpdateSubtask } from "../tasks/context";
import { messageToStep } from "../tasks/steps";
import { parseSubtaskTerminalEvent } from "../tasks/subtask-result";
import type { UploadedFileInfo } from "../uploads";
import {
  promptInputFilePartToFile,
  uploadFailureMessage,
  uploadFiles,
} from "../uploads";

import {
  branchThreadFromTurn,
  fetchRunMessagesPage,
  fetchThreadTokenUsage,
  type RunMessagesPageResponse,
} from "./api";
import {
  buildThreadsSearchQueryOptions,
  DEFAULT_THREAD_SEARCH_PARAMS,
  filterThreadSearchResults,
  type ThreadSearchParams,
} from "./thread-search-query";
import { threadTokenUsageQueryKey } from "./token-usage";
import type {
  AgentThread,
  AgentThreadState,
  RunMessage,
  ThreadTokenUsageResponse,
} from "./types";

export type ToolEndEvent = {
  name: string;
  data: unknown;
};

export type ThreadStreamOptions = {
  threadId?: string | null | undefined;
  displayThreadId?: string | null | undefined;
  context: LocalSettings["context"];
  isMock?: boolean;
  privateWork?: ProjectPrivateWorkScope;
  onSend?: (threadId: string) => void;
  onStart?: (threadId: string, runId: string) => void;
  onFinish?: (state: AgentThreadState) => void;
  onToolEnd?: (event: ToolEndEvent) => void;
};

function scopedThreadQueryKey(
  scope: ProjectClientScope,
  ...segments: readonly unknown[]
) {
  return privateWorkQueryKey(scope, ...segments);
}

type SendMessageOptions = {
  additionalKwargs?: Record<string, unknown>;
  additionalInputMessages?: Message[];
  /**
   * Invoked exactly once when the send passes the in-flight guard and is
   * genuinely dispatched. It never fires on the early-return path, so callers
   * can safely perform one-time cleanup (e.g. clearing quoted references)
   * without losing state when a concurrent send is dropped.
   */
  onSent?: () => void;
};

type ThreadDeleteClient = {
  threads: {
    delete: (threadId: string) => Promise<unknown>;
    search: (query: Record<string, unknown>) => Promise<AgentThread[]>;
  };
};

type ThreadSidecarSearchClient = {
  threads: {
    search: (query: Record<string, unknown>) => Promise<AgentThread[]>;
  };
};

type RegeneratePrepareResponse = {
  input: Partial<AgentThreadState>;
  checkpoint: {
    checkpoint_ns: string;
    checkpoint_id: string;
    checkpoint_map: Record<string, unknown> | null;
  };
  metadata: Record<string, unknown>;
  target_run_id: string;
};

export function buildThreadSubmitMessages({
  text,
  messageId,
  additionalKwargs,
  additionalInputMessages = [],
  filesForSubmit = [],
}: {
  text: string;
  messageId: string;
  additionalKwargs?: Record<string, unknown>;
  additionalInputMessages?: Message[];
  filesForSubmit?: FileInMessage[];
}): Message[] {
  return [
    ...additionalInputMessages,
    {
      type: "human",
      id: messageId,
      content: [
        {
          type: "text",
          text,
        },
      ],
      additional_kwargs: {
        ...additionalKwargs,
        ...(filesForSubmit.length > 0 ? { files: filesForSubmit } : {}),
      },
    } as Message,
  ];
}

const EMPTY_THREAD_VALUES: AgentThreadState = {
  title: "",
  messages: [],
  artifacts: [],
  todos: [],
};

function isNonEmptyString(value: string | undefined): value is string {
  return typeof value === "string" && value.length > 0;
}

const SUMMARIZATION_MIDDLEWARE_UPDATE_KEYS = new Set([
  "SummarizationMiddleware.before_model",
  "DeerFlowSummarizationMiddleware.before_model",
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

function messageIdentity(message: Message): string | undefined {
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

function dedupeMessagesByIdentity(messages: Message[]): Message[] {
  const lastIndexByIdentity = new Map<string, number>();
  const lastVisibleIndexByIdentity = new Map<string, number>();

  // This is a UI-display dedupe rule, not a general LangChain message-stream
  // contract. Hidden messages that share an identity with a visible message are
  // treated as control messages for this merged view; hidden messages carrying
  // independent tracing/task semantics should use a distinct id or a custom
  // stream/state channel instead of relying on message dedupe preservation.
  const preservedTurnDurations = new Map<string, number>();
  const preservedRunIds = new Map<string, string>();
  messages.forEach((message, index) => {
    const identity = messageIdentity(message);
    if (identity) {
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
      const runId = Reflect.get(message, "run_id");
      if (typeof runId === "string" && runId.length > 0) {
        preservedRunIds.set(identity, runId);
      }
    }
  });

  return messages
    .filter((message, index) => {
      const identity = messageIdentity(message);
      if (!identity) {
        return true;
      }
      const visibleIndex = lastVisibleIndexByIdentity.get(identity);
      if (visibleIndex !== undefined) {
        return visibleIndex === index;
      }
      return lastIndexByIdentity.get(identity) === index;
    })
    .map((message) => {
      const identity = messageIdentity(message);
      if (!identity) return message;
      const preserveDuration =
        preservedTurnDurations.has(identity) &&
        message.additional_kwargs?.turn_duration === undefined;
      const preserveRunId =
        preservedRunIds.has(identity) &&
        typeof Reflect.get(message, "run_id") !== "string";
      if (!preserveDuration && !preserveRunId) return message;
      return {
        ...message,
        ...(preserveRunId ? { run_id: preservedRunIds.get(identity) } : {}),
        ...(preserveDuration
          ? {
              additional_kwargs: {
                ...message.additional_kwargs,
                turn_duration: preservedTurnDurations.get(identity),
              },
            }
          : {}),
      } as Message;
    });
}

function dedupeRunMessagesByIdentity(messages: RunMessage[]): RunMessage[] {
  const lastIndexByIdentity = new Map<string, number>();
  messages.forEach((message, index) => {
    const identity = messageIdentity(message.content);
    if (identity) {
      lastIndexByIdentity.set(`${message.run_id}:${identity}`, index);
    }
  });

  return messages.filter((message, index) => {
    const identity = messageIdentity(message.content);
    if (!identity) {
      return true;
    }
    return lastIndexByIdentity.get(`${message.run_id}:${identity}`) === index;
  });
}

export function mergeRunMessageRows(
  previous: RunMessage[],
  incoming: RunMessage[],
  runsNewestFirst: Run[],
): RunMessage[] {
  const merged = dedupeRunMessagesByIdentity([...previous, ...incoming]);
  const runOrder = new Map<string, number>();
  [...runsNewestFirst]
    .reverse()
    .forEach((run, index) => runOrder.set(run.run_id, index));

  let nextUnknownRunOrder = runOrder.size;
  for (const message of merged) {
    if (!runOrder.has(message.run_id)) {
      runOrder.set(message.run_id, nextUnknownRunOrder);
      nextUnknownRunOrder += 1;
    }
  }

  return merged
    .map((message, index) => ({ message, index }))
    .sort((left, right) => {
      const runDifference =
        (runOrder.get(left.message.run_id) ?? Number.MAX_SAFE_INTEGER) -
        (runOrder.get(right.message.run_id) ?? Number.MAX_SAFE_INTEGER);
      if (runDifference !== 0) {
        return runDifference;
      }

      const leftSeq = left.message.seq;
      const rightSeq = right.message.seq;
      if (typeof leftSeq === "number" && typeof rightSeq === "number") {
        return leftSeq - rightSeq || left.index - right.index;
      }
      if (typeof leftSeq === "number") {
        return -1;
      }
      if (typeof rightSeq === "number") {
        return 1;
      }
      return left.index - right.index;
    })
    .map(({ message }) => message);
}

export function getSupersededRunIds(
  runs: Run[] | undefined,
  pendingSupersededRunIds?: ReadonlySet<string>,
) {
  const ids = new Set(pendingSupersededRunIds ?? []);
  for (const run of runs ?? []) {
    if (run.status !== "success") {
      continue;
    }
    const metadata = run.metadata;
    if (metadata && typeof metadata === "object") {
      const fromRunId = Reflect.get(metadata, "regenerate_from_run_id");
      if (typeof fromRunId === "string" && fromRunId) {
        ids.add(fromRunId);
      }
    }
  }
  return ids;
}

export function latestRunHasTerminalFailure(runs: Run[] | undefined) {
  const status = runs?.[0]?.status as string | undefined;
  return status === "error" || status === "failed" || status === "timeout";
}

export function rememberActiveRun(
  runs: Run[] | undefined,
  {
    threadId,
    runId,
    createdAt,
  }: {
    threadId: string;
    runId: string;
    createdAt: string;
  },
): Run[] {
  const existing = runs?.find((run) => run.run_id === runId);
  const activeRun: Run =
    existing ??
    ({
      run_id: runId,
      thread_id: threadId,
      assistant_id: "lead_agent",
      created_at: createdAt,
      updated_at: createdAt,
      status: "running",
      metadata: {},
      multitask_strategy: null,
    } satisfies Run);
  return [activeRun, ...(runs ?? []).filter((run) => run.run_id !== runId)];
}

export function removeSetItems<T>(
  values: ReadonlySet<T>,
  itemsToRemove: Iterable<T>,
) {
  const next = new Set(values);
  for (const item of itemsToRemove) {
    next.delete(item);
  }
  return next;
}

function isInternalHistoryCaller(caller: unknown): boolean {
  return (
    typeof caller === "string" &&
    (caller.startsWith("subagent:") || caller.startsWith("middleware:"))
  );
}

function getAIToolCallIds(message: Message): string[] {
  if (message.type !== "ai") {
    return [];
  }

  const ids = new Set<string>();
  const collectIds = (toolCalls: unknown) => {
    if (!Array.isArray(toolCalls)) {
      return;
    }
    for (const toolCall of toolCalls) {
      if (!toolCall || typeof toolCall !== "object") {
        continue;
      }
      const id = Reflect.get(toolCall, "id");
      if (typeof id === "string" && id.length > 0) {
        ids.add(id);
      }
    }
  };

  collectIds(Reflect.get(message, "tool_calls"));
  collectIds(message.additional_kwargs?.tool_calls);
  return [...ids];
}

function runToolCallKey(runId: string, toolCallId: string): string {
  return `${runId}\u0000${toolCallId}`;
}

function filterVisibleHistoryRows(messageRows: RunMessage[]): RunMessage[] {
  const hiddenToolCalls = new Set<string>();
  const visibleToolCalls = new Set<string>();

  for (const row of messageRows) {
    if (row.content.type !== "ai") {
      continue;
    }
    const internal = isInternalHistoryCaller(row.metadata.caller);
    for (const toolCallId of getAIToolCallIds(row.content)) {
      const key = runToolCallKey(row.run_id, toolCallId);
      if (internal) {
        hiddenToolCalls.add(key);
        visibleToolCalls.delete(key);
      } else if (!hiddenToolCalls.has(key)) {
        visibleToolCalls.add(key);
      }
    }
  }

  return messageRows.filter((row) => {
    if (row.content.type === "human") {
      return (
        row.metadata.source === "run_admission" ||
        !isInternalHistoryCaller(row.metadata.caller)
      );
    }
    if (row.content.type === "ai") {
      return !isInternalHistoryCaller(row.metadata.caller);
    }
    if (row.content.type === "tool") {
      const toolCallId = Reflect.get(row.content, "tool_call_id");
      if (typeof toolCallId !== "string" || toolCallId.length === 0) {
        return false;
      }
      return visibleToolCalls.has(runToolCallKey(row.run_id, toolCallId));
    }
    return true;
  });
}

export function buildVisibleHistoryMessages(
  messageRows: RunMessage[],
  supersededRunIds: ReadonlySet<string>,
  appendedMessages: Message[],
) {
  const visibleRows = filterVisibleHistoryRows(
    messageRows.filter(
      (message) => !supersededRunIds.has(message.run_id),
    ),
  );
  return dedupeMessagesByIdentity([
    // Carry the owning run_id onto the content message so historical subtask
    // cards can fetch their persisted step history on expand (#3779). run_id
    // lives on the RunMessage wrapper and would otherwise be dropped here.
    ...visibleRows.map((message) => ({
      ...message.content,
      run_id: message.run_id,
      ...(message.metadata.source === "run_admission"
        ? { run_message_source: "run_admission" }
        : {}),
    })),
    ...appendedMessages,
  ]);
}

export function findLatestUnloadedRunIndex(
  runs: Run[],
  loadedRunIds: ReadonlySet<string>,
): number {
  for (let i = 0; i < runs.length; i++) {
    const run = runs[i];
    if (run && !loadedRunIds.has(run.run_id)) {
      return i;
    }
  }
  return -1;
}

export const MAX_CONSECUTIVE_EMPTY_RUN_LOADS = 5;

export function shouldAutoContinueOnEmptyRun(
  fetchedMessageCount: number,
  consecutiveEmptyLoads: number,
  maxConsecutiveEmptyLoads: number = MAX_CONSECUTIVE_EMPTY_RUN_LOADS,
): boolean {
  return (
    fetchedMessageCount === 0 &&
    consecutiveEmptyLoads < maxConsecutiveEmptyLoads
  );
}

export function runMessagesPageHasMore(result: RunMessagesPageResponse) {
  return result.has_more;
}

export function getOldestRunMessageSeq(messages: RunMessage[]) {
  let oldestSeq: number | null = null;
  for (const message of messages) {
    if (typeof message.seq !== "number") {
      continue;
    }
    oldestSeq =
      oldestSeq === null ? message.seq : Math.min(oldestSeq, message.seq);
  }
  return oldestSeq;
}

export function getNextRunMessagesBeforeSeq(
  result: RunMessagesPageResponse,
): number | null | undefined {
  if (!runMessagesPageHasMore(result)) {
    return null;
  }
  return getOldestRunMessageSeq(result.data) ?? undefined;
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

  const threadMessageIds = new Set(
    threadMessages
      .filter((message) => !isHiddenFromUIMessage(message))
      .map(messageIdentity)
      .filter(isNonEmptyString),
  );
  const visibleLiveHumanRunIds = new Set(
    threadMessages
      .filter(
        (message) =>
          message.type === "human" && !isHiddenFromUIMessage(message),
      )
      .map(messageRunId)
      .filter(isNonEmptyString),
  );
  const admissionMessages: Message[] = [];
  const regularHistoryMessages = historyMessages.filter((message) => {
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
  const savedRunIds = new Map<string, string>();
  for (const msg of historyMessages) {
    const identity = messageIdentity(msg);
    if (identity && msg.additional_kwargs?.turn_duration !== undefined) {
      savedTurnDurations.set(
        identity,
        msg.additional_kwargs.turn_duration as number,
      );
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
    ...threadMessages,
    ...optimisticMessages,
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
              messageOrder !== undefined && messageOrder > admissionRunOrder
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
    const preserveRunId =
      savedRunIds.has(identity) &&
      typeof Reflect.get(message, "run_id") !== "string";
    if (!preserveDuration && !preserveRunId) return message;
    return {
      ...message,
      ...(preserveRunId ? { run_id: savedRunIds.get(identity) } : {}),
      ...(preserveDuration
        ? {
            additional_kwargs: {
              ...message.additional_kwargs,
              turn_duration: savedTurnDurations.get(identity),
            },
          }
        : {}),
    } as Message;
  });
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

function getMessagesAfterBaseline(
  messages: Message[],
  baselineMessageIds: ReadonlySet<string>,
): Message[] {
  return messages.filter((message) => {
    const id = messageIdentity(message);
    return !id || !baselineMessageIds.has(id);
  });
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

export function upsertThreadInSearchCache(
  queryClient: QueryClient,
  thread: AgentThread,
  scope: ProjectClientScope,
) {
  queryClient.setQueriesData(
    {
      queryKey: scopedThreadQueryKey(scope, "threads", "search"),
      exact: false,
    },
    (oldData: Array<AgentThread> | undefined) => {
      if (!oldData) {
        return [thread];
      }

      const existingIndex = oldData.findIndex(
        (t) => t.thread_id === thread.thread_id,
      );
      if (existingIndex === -1) {
        return [thread, ...oldData];
      }

      return oldData.map((t, index) => {
        if (index !== existingIndex) {
          return t;
        }
        return {
          ...thread,
          ...t,
          metadata: {
            ...(thread.metadata ?? {}),
            ...(t.metadata ?? {}),
          },
          values: {
            ...thread.values,
            ...t.values,
          },
        };
      });
    },
  );
}

export function upsertThreadInInfiniteCache(
  queryClient: QueryClient,
  thread: AgentThread,
  scope: ProjectClientScope,
) {
  queryClient.setQueriesData(
    {
      queryKey: scopedThreadQueryKey(
        scope,
        ...INFINITE_THREADS_QUERY_KEY_PREFIX,
      ),
      exact: false,
    },
    (oldData: InfiniteData<AgentThread[]> | undefined) => {
      if (!oldData) {
        return oldData;
      }

      const merged = oldData.pages.map((page) =>
        page.map((t) =>
          t.thread_id === thread.thread_id
            ? {
                ...thread,
                ...t,
                metadata: {
                  ...(thread.metadata ?? {}),
                  ...(t.metadata ?? {}),
                },
                values: {
                  ...thread.values,
                  ...t.values,
                },
              }
            : t,
        ),
      );

      const exists = merged.some((page) =>
        page.some((t) => t.thread_id === thread.thread_id),
      );
      if (exists) {
        return { ...oldData, pages: merged };
      }

      const firstPage = merged[0] ?? [];
      const restPages = merged.slice(1);
      return {
        ...oldData,
        pages: [[thread, ...firstPage], ...restPages],
      };
    },
  );
}

export function invalidateStoppedThreadCaches(
  queryClient: QueryClient,
  threadId: string | null | undefined,
  isMock = false,
  scope: ProjectClientScope,
) {
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(scope, "threads", "search"),
  });
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(scope, ...INFINITE_THREADS_QUERY_KEY_PREFIX),
  });

  if (!threadId || isMock) {
    return;
  }

  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(scope, "thread", threadId),
  });
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(
      scope,
      "thread",
      "metadata",
      threadId,
      isMock,
    ),
  });
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(
      scope,
      ...threadTokenUsageQueryKey(threadId),
    ),
  });
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(scope, "uploads", "list", threadId),
  });
}

export const STOP_THREAD_FINALIZATION_REFETCH_DELAY_MS = 1500;

function scheduleStoppedThreadFinalizationRefetch(
  queryClient: QueryClient,
  threadId: string | null | undefined,
  isMock = false,
  scope: ProjectClientScope,
) {
  if (isMock) {
    return;
  }
  globalThis.setTimeout(() => {
    invalidateStoppedThreadCaches(queryClient, threadId, isMock, scope);
  }, STOP_THREAD_FINALIZATION_REFETCH_DELAY_MS);
}

export async function stopThreadAndInvalidateCaches(
  queryClient: QueryClient,
  stop: () => Promise<void> | void,
  threadId: string | null | undefined,
  isMock = false,
  scope: ProjectClientScope,
) {
  try {
    await stop();
  } finally {
    invalidateStoppedThreadCaches(queryClient, threadId, isMock, scope);
    scheduleStoppedThreadFinalizationRefetch(
      queryClient,
      threadId,
      isMock,
      scope,
    );
  }
}

function getStreamErrorMessage(error: unknown): string {
  if (typeof error === "string" && error.trim()) {
    return error;
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  if (typeof error === "object" && error !== null) {
    const message = Reflect.get(error, "message");
    if (typeof message === "string" && message.trim()) {
      return message;
    }
    const nestedError = Reflect.get(error, "error");
    if (nestedError instanceof Error && nestedError.message.trim()) {
      return nestedError.message;
    }
    if (typeof nestedError === "string" && nestedError.trim()) {
      return nestedError;
    }
  }
  return "Request failed.";
}

async function readResponseErrorMessage(
  response: Response,
  fallback = "Request failed.",
) {
  try {
    const data = await response.json();
    if (typeof data?.detail === "string" && data.detail.trim()) {
      return data.detail;
    }
  } catch {
    // Use the fallback below when the response body is not JSON.
  }
  return response.statusText || fallback;
}

function getHttpStatus(error: unknown): number | undefined {
  if (typeof error !== "object" || error === null) {
    return undefined;
  }

  const status = Reflect.get(error, "status");
  if (typeof status === "number") {
    return status;
  }

  const response = Reflect.get(error, "response");
  if (typeof response === "object" && response !== null) {
    const responseStatus = Reflect.get(response, "status");
    if (typeof responseStatus === "number") {
      return responseStatus;
    }
  }

  return undefined;
}

function isThreadMissingError(error: unknown): boolean {
  const status = getHttpStatus(error);
  // Treat 403 like 404 here to avoid disclosing whether an inaccessible thread
  // exists; callers redirect stale/inaccessible URLs back to a blank chat.
  return status === 403 || status === 404;
}

export function useThreadStream({
  threadId,
  displayThreadId,
  context,
  isMock,
  privateWork: explicitPrivateWork,
  onSend,
  onStart,
  onFinish,
  onToolEnd,
}: ThreadStreamOptions) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const { t } = useI18n();
  const currentViewThreadId = displayThreadId ?? threadId ?? null;
  const currentViewThreadIdRef = useRef(currentViewThreadId);
  currentViewThreadIdRef.current = currentViewThreadId;
  // Optimistic messages shown before the server stream responds.
  const [optimisticMessages, setOptimisticMessages] = useState<Message[]>([]);
  const [optimisticThreadId, setOptimisticThreadId] = useState<string | null>(
    null,
  );
  const [liveMessagesThreadId, setLiveMessagesThreadId] = useState<
    string | null
  >(null);
  const [pendingSupersededRunIds, setPendingSupersededRunIds] = useState<
    ReadonlySet<string>
  >(() => new Set());
  const [pendingSupersededMessageIds, setPendingSupersededMessageIds] =
    useState<ReadonlySet<string>>(() => new Set());
  const [isUploading, setIsUploading] = useState(false);
  // Track the thread ID that is currently streaming to handle thread changes during streaming
  const [onStreamThreadId, setOnStreamThreadId] = useState(() => threadId);
  // Ref to track current thread ID across async callbacks without causing re-renders,
  // and to allow access to the current thread id in onUpdateEvent
  const threadIdRef = useRef<string | null>(threadId ?? null);
  const messagesRef = useRef<Message[]>([]);
  const currentRunIdRef = useRef<string | null>(null);
  const currentRunThreadIdRef = useRef<string | null>(null);
  const currentRunBaselineMessageIdsRef = useRef<Set<string>>(new Set());
  const runBaselinePreparedRef = useRef(false);
  const startedRef = useRef(false);
  const pendingUsageBaselineMessageIdsRef = useRef<Set<string>>(new Set());
  const listeners = useRef({
    onSend,
    onStart,
    onFinish,
    onToolEnd,
  });

  const {
    messages: history,
    runs: historyRuns,
    hasMore: hasMoreHistory,
    loadMore: loadMoreHistory,
    loading: isHistoryLoading,
    error: historyError,
    retry: retryHistory,
    appendMessages,
  } = useThreadHistory(onStreamThreadId ?? "", {
    enabled: !isMock,
    pendingSupersededRunIds,
    privateWork,
  });

  // Keep listeners ref updated with latest callbacks
  useEffect(() => {
    listeners.current = { onSend, onStart, onFinish, onToolEnd };
  }, [onSend, onStart, onFinish, onToolEnd]);

  useEffect(() => {
    const normalizedThreadId = threadId ?? null;
    if (!normalizedThreadId) {
      // Reset when the UI moves back to a brand new unsaved thread.
      startedRef.current = false;
      setOnStreamThreadId(normalizedThreadId);
    } else {
      setOnStreamThreadId(normalizedThreadId);
    }
    threadIdRef.current = normalizedThreadId;
  }, [threadId]);

  const handleStreamStart = useCallback((_threadId: string, _runId: string) => {
    threadIdRef.current = _threadId;
    if (!runBaselinePreparedRef.current) {
      currentRunBaselineMessageIdsRef.current = new Set(
        messagesRef.current
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
    }
    currentRunIdRef.current = _runId;
    currentRunThreadIdRef.current = _threadId;
    runBaselinePreparedRef.current = false;
    setOptimisticThreadId((currentOptimisticThreadId) => {
      const currentView = currentViewThreadIdRef.current;
      if (
        currentOptimisticThreadId &&
        (currentOptimisticThreadId === currentView ||
          currentOptimisticThreadId === _threadId)
      ) {
        return _threadId;
      }
      return currentOptimisticThreadId;
    });
    setLiveMessagesThreadId((currentLiveMessagesThreadId) => {
      const currentView = currentViewThreadIdRef.current;
      if (
        currentLiveMessagesThreadId &&
        (currentLiveMessagesThreadId === currentView ||
          currentLiveMessagesThreadId === _threadId)
      ) {
        return _threadId;
      }
      return currentLiveMessagesThreadId;
    });
    if (!startedRef.current) {
      listeners.current.onStart?.(_threadId, _runId);
      startedRef.current = true;
    }
    setOnStreamThreadId(_threadId);
  }, []);

  const queryClient = useQueryClient();
  const updateSubtask = useUpdateSubtask();

  const thread = useStream<AgentThreadState>({
    client: privateWork.client,
    assistantId: "lead_agent",
    threadId: onStreamThreadId,
    reconnectOnMount: privateWork.reconnectOnMount,
    fetchStateHistory: { limit: 1 },
    onCreated(meta) {
      handleStreamStart(meta.thread_id, meta.run_id);
      const now = new Date().toISOString();
      const runQueryKey = scopedThreadQueryKey(
        privateWork.scope,
        "thread",
        meta.thread_id,
      );
      queryClient.setQueryData<Run[]>(runQueryKey, (runs) =>
        rememberActiveRun(runs, {
          threadId: meta.thread_id,
          runId: meta.run_id,
          createdAt: now,
        }),
      );
      void queryClient.invalidateQueries({
        queryKey: runQueryKey,
        exact: true,
      });
      upsertThreadInSearchCache(
        queryClient,
        {
          thread_id: meta.thread_id,
          created_at: now,
          updated_at: now,
          state_updated_at: now,
          metadata: context.agent_name
            ? { agent_name: context.agent_name }
            : {},
          status: "busy",
          values: {
            title: t.pages.newChat,
            messages: [],
            artifacts: [],
          },
          interrupts: {},
        },
        privateWork.scope,
      );
      upsertThreadInInfiniteCache(
        queryClient,
        {
          thread_id: meta.thread_id,
          created_at: now,
          updated_at: now,
          state_updated_at: now,
          metadata: context.agent_name
            ? { agent_name: context.agent_name }
            : {},
          status: "busy",
          values: {
            title: t.pages.newChat,
            messages: [],
            artifacts: [],
          },
          interrupts: {},
        },
        privateWork.scope,
      );
      if (context.agent_name && !isMock) {
        void privateWork.client.threads
          .update(meta.thread_id, {
            metadata: { agent_name: context.agent_name },
          })
          .catch(() => ({}));
      }
    },
    onLangChainEvent(event) {
      if (event.event === "on_tool_end") {
        listeners.current.onToolEnd?.({
          name: event.name,
          data: event.data,
        });
      }
    },
    onUpdateEvent(data) {
      const _messages = getSummarizationMiddlewareMessages(data);
      if (_messages && _messages.length >= 2) {
        for (const m of _messages) {
          // Backward-compat shim: pre-PR2 threads may still carry a synthetic
          // HumanMessage(name="summary") from the old summarization path. New
          // threads keep the summary in ThreadState.summary_text instead.
          if (m.name === "summary" && m.type === "human") {
            summarizedRef.current?.add(m.id ?? "");
          }
        }
        const _movedMessages = attachRunIdToNewMessages(
          computeSummarizationMovedMessages(
            messagesRef.current,
            _messages,
            summarizedRef.current ?? new Set<string>(),
          ),
          currentRunIdRef.current,
          currentRunBaselineMessageIdsRef.current,
        );
        // Buffer the rescued messages synchronously so the merge can keep
        // displaying them immediately, even though appendMessages below only
        // updates the archived-history state asynchronously (#3825).
        pendingArchivedMessagesRef.current = dedupeMessagesByIdentity([
          ...pendingArchivedMessagesRef.current,
          ..._movedMessages,
        ]);
        pendingArchiveThreadIdRef.current = threadIdRef.current;
        appendMessages(_movedMessages);
        messagesRef.current = [];
      }

      const updates: Array<Partial<AgentThreadState> | null> = Object.values(
        data || {},
      );
      for (const update of updates) {
        if (update && "title" in update && update.title) {
          void queryClient.setQueriesData(
            {
              queryKey: scopedThreadQueryKey(
                privateWork.scope,
                "threads",
                "search",
              ),
              exact: false,
            },
            (oldData: Array<AgentThread> | undefined) => {
              return oldData?.map((t) => {
                if (t.thread_id === threadIdRef.current) {
                  return {
                    ...t,
                    values: {
                      ...t.values,
                      title: update.title,
                    },
                  };
                }
                return t;
              });
            },
          );
          const nextTitle: string = update.title;
          void queryClient.setQueriesData(
            {
              queryKey: scopedThreadQueryKey(
                privateWork.scope,
                ...INFINITE_THREADS_QUERY_KEY_PREFIX,
              ),
              exact: false,
            },
            (oldData: InfiniteData<AgentThread[]> | undefined) =>
              mapInfiniteThreadsCache(
                oldData,
                (t): AgentThread =>
                  t.thread_id === threadIdRef.current
                    ? {
                        ...t,
                        values: {
                          ...t.values,
                          title: nextTitle,
                        },
                      }
                    : t,
              ),
          );
        }
      }
    },
    onCustomEvent(event: unknown) {
      const terminalUpdate = parseSubtaskTerminalEvent(event);
      if (terminalUpdate) {
        updateSubtask({
          ...terminalUpdate,
          statusSource: "custom_event",
          ...(terminalUpdate.status === "failed" && !terminalUpdate.error
            ? { error: t.subtasks.failed }
            : {}),
        });
        return;
      }

      if (
        typeof event === "object" &&
        event !== null &&
        "type" in event &&
        event.type === "task_running"
      ) {
        const e = event as {
          type: "task_running";
          task_id: string;
          message: AIMessage;
          message_index?: number;
        };
        // Accumulate the full step history instead of overwriting (#3779): keep
        // latestMessage for the collapsed-header tool-call hint, and append the
        // normalized step (assistant turn or tool output) to the timeline.
        updateSubtask({
          id: e.task_id,
          status: "in_progress",
          statusSource: "custom_event",
          latestMessage: e.message,
          steps: [messageToStep(e.message, e.message_index ?? 0)],
        });
        return;
      }

      if (
        typeof event === "object" &&
        event !== null &&
        "type" in event &&
        event.type === "llm_retry" &&
        "message" in event &&
        typeof event.message === "string" &&
        event.message.trim()
      ) {
        const e = event as { type: "llm_retry"; message: string };
        toast(e.message);
      }
    },
    onError(error) {
      setOptimisticMessages([]);
      setOptimisticThreadId(null);
      setLiveMessagesThreadId(null);
      setPendingSupersededRunIds(new Set());
      setPendingSupersededMessageIds(new Set());
      toast.error(
        isProjectRunTerminalFailure(error)
          ? t.conversation.runFailedDescription
          : getStreamErrorMessage(error),
      );
      pendingUsageBaselineMessageIdsRef.current = new Set(
        messagesRef.current
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      invalidateStoppedThreadCaches(
        queryClient,
        threadIdRef.current,
        isMock,
        privateWork.scope,
      );
    },
    onFinish(state) {
      listeners.current.onFinish?.(state.values);
      pendingUsageBaselineMessageIdsRef.current = new Set(
        messagesRef.current
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      invalidateStoppedThreadCaches(
        queryClient,
        threadIdRef.current,
        isMock,
        privateWork.scope,
      );
    },
  });

  const stopThread = useCallback(async () => {
    const stoppedThreadId =
      threadIdRef.current ?? displayThreadId ?? threadId ?? null;
    await stopThreadAndInvalidateCaches(
      queryClient,
      () => thread.stop(),
      stoppedThreadId,
      isMock,
      privateWork.scope,
    );
  }, [
    displayThreadId,
    isMock,
    privateWork.scope,
    queryClient,
    thread,
    threadId,
  ]);

  const hasVisibleStreamState =
    Boolean(threadId) || liveMessagesThreadId === currentViewThreadId;
  const persistedMessages = useMemo(
    () =>
      hasVisibleStreamState
        ? thread.messages.filter(
            (message) =>
              !message.id || !pendingSupersededMessageIds.has(message.id),
          )
        : [],
    [hasVisibleStreamState, pendingSupersededMessageIds, thread.messages],
  );
  const visibleHistory = useMemo(
    () => (threadId ? history : []),
    [history, threadId],
  );
  const humanMessageCount = persistedMessages.filter(
    (m) => m.type === "human",
  ).length;
  const latestMessageCountsRef = useRef({ humanMessageCount });
  const sendInFlightRef = useRef(false);
  // Synchronous bridge for messages rescued from context summarization. The
  // archived-history `setState` (via appendMessages) lands on a different
  // schedule than the live thread external store, so the merge reads this buffer
  // to avoid dropping rescued messages in the render window before history
  // catches up (#3825).
  const pendingArchivedMessagesRef = useRef<Message[]>([]);
  // The thread the rescue buffer belongs to, captured when onUpdateEvent fills
  // it. The merge only overlays the buffer when this matches the viewed
  // `threadId`, so a previous thread's rescued messages can never flash into
  // another thread or the new-chat screen (#3825).
  const pendingArchiveThreadIdRef = useRef<string | null>(null);
  const summarizedRef = useRef<Set<string>>(null);
  // Track human message count before sending to prevent clearing optimistic
  // messages before the server's human message arrives (e.g. when AI messages
  // from "messages-tuple" events arrive before the input human message from
  // "values" events).
  const prevHumanMsgCountRef = useRef(humanMessageCount);

  latestMessageCountsRef.current = { humanMessageCount };
  summarizedRef.current ??= new Set<string>();

  // Reset thread-local pending UI state when switching between threads so
  // optimistic messages and in-flight guards do not leak across chat views.
  useEffect(() => {
    startedRef.current = false;
    sendInFlightRef.current = false;
    messagesRef.current = [];
    currentRunIdRef.current = null;
    currentRunThreadIdRef.current = null;
    currentRunBaselineMessageIdsRef.current = new Set();
    runBaselinePreparedRef.current = false;
    pendingArchivedMessagesRef.current = [];
    pendingArchiveThreadIdRef.current = null;
    summarizedRef.current = new Set<string>();
    pendingUsageBaselineMessageIdsRef.current = new Set();
    setPendingSupersededRunIds(new Set());
    setPendingSupersededMessageIds(new Set());
    prevHumanMsgCountRef.current =
      latestMessageCountsRef.current.humanMessageCount;
  }, [threadId]);

  // Release archive-buffer entries once the canonical history state has absorbed
  // them, so the synchronous bridge stays transient and never resurrects a
  // message that history later filters out (e.g. a superseded run) (#3825).
  useEffect(() => {
    pendingArchivedMessagesRef.current = pruneConfirmedArchivedMessages(
      pendingArchivedMessagesRef.current,
      visibleHistory,
    );
  }, [visibleHistory]);

  useEffect(() => {
    if (optimisticThreadId && optimisticThreadId !== currentViewThreadId) {
      setOptimisticMessages([]);
      setOptimisticThreadId(null);
    }
    if (liveMessagesThreadId && liveMessagesThreadId !== currentViewThreadId) {
      setLiveMessagesThreadId(null);
    }
  }, [currentViewThreadId, liveMessagesThreadId, optimisticThreadId]);

  // When streaming starts without a baseline (e.g. reconnection, run started
  // from another client, or page reload mid-stream), snapshot the current
  // messages so only *new* messages are treated as "pending" for token usage.
  useEffect(() => {
    if (
      thread.isLoading &&
      pendingUsageBaselineMessageIdsRef.current.size === 0
    ) {
      pendingUsageBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
    }
  }, [persistedMessages, thread.isLoading]);

  // Clear optimistic when server messages arrive.
  // For messages with a human optimistic message, wait until the server's
  // human message has arrived to avoid clearing before the input message
  // appears in the stream (the input message may arrive via "values" events
  // after individual "messages-tuple" events for AI messages).
  const optimisticMessageCount = optimisticMessages.length;
  const hasHumanOptimistic = optimisticMessages.some((m) => m.type === "human");
  useEffect(() => {
    if (optimisticMessageCount === 0) return;

    const newHumanMsgArrived = humanMessageCount > prevHumanMsgCountRef.current;

    if (!hasHumanOptimistic || newHumanMsgArrived) {
      setOptimisticMessages([]);
      setOptimisticThreadId(null);
    }
  }, [hasHumanOptimistic, humanMessageCount, optimisticMessageCount]);

  const sendMessage = useCallback(
    async (
      threadId: string,
      message: PromptInputMessage,
      extraContext?: Record<string, unknown>,
      options?: SendMessageOptions,
    ) => {
      if (sendInFlightRef.current) {
        return;
      }
      sendInFlightRef.current = true;

      // The send has genuinely proceeded past the in-flight guard, so callers
      // can now run one-time cleanup that must not fire on the dropped path.
      options?.onSent?.();

      const text = message.text.trim();
      const humanMessageId = `human-${crypto.randomUUID()}`;

      // Capture the current human message count before showing optimistic
      // messages so we can wait for the server's copy of the user input.
      prevHumanMsgCountRef.current = humanMessageCount;
      pendingUsageBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      currentRunBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      runBaselinePreparedRef.current = true;

      // Build optimistic files list with uploading status
      const optimisticFiles: FileInMessage[] = (message.files ?? []).map(
        (f) => ({
          filename: f.filename ?? "",
          size: 0,
          status: "uploading" as const,
        }),
      );

      const hideFromUI = options?.additionalKwargs?.hide_from_ui === true;
      const optimisticAdditionalKwargs = {
        ...options?.additionalKwargs,
        ...(optimisticFiles.length > 0 ? { files: optimisticFiles } : {}),
      };

      const newOptimistic: Message[] = [];
      if (!hideFromUI) {
        newOptimistic.push({
          type: "human",
          id: humanMessageId,
          content: text ? [{ type: "text", text }] : "",
          additional_kwargs: optimisticAdditionalKwargs,
        });
      }

      if (optimisticFiles.length > 0 && !hideFromUI) {
        // Mock AI message while files are being uploaded
        newOptimistic.push({
          type: "ai",
          id: `opt-ai-${Date.now()}`,
          content: t.uploads.uploadingFiles,
          additional_kwargs: { element: "task" },
        });
      }
      setOptimisticThreadId(threadId);
      setLiveMessagesThreadId(threadId);
      setOptimisticMessages(newOptimistic);

      listeners.current.onSend?.(threadId);

      let uploadedFileInfo: UploadedFileInfo[] = [];

      try {
        // Upload files first if any
        if (message.files && message.files.length > 0) {
          setIsUploading(true);
          try {
            const filePromises = message.files.map((fileUIPart) =>
              promptInputFilePartToFile(fileUIPart),
            );

            const conversionResults = await Promise.all(filePromises);
            const files = conversionResults.filter(
              (file): file is File => file !== null,
            );
            const failedConversions = conversionResults.length - files.length;

            if (failedConversions > 0) {
              throw new Error(
                `Failed to prepare ${failedConversions} attachment(s) for upload. Please retry.`,
              );
            }

            if (!threadId) {
              throw new Error("Thread is not ready for file upload.");
            }

            if (files.length > 0) {
              const uploadResponse = await runPrivateWorkAbortable(
                privateWork,
                (signal) => uploadFiles(threadId, files, privateWork, signal),
              );
              uploadedFileInfo = uploadResponse.files;

              // Update optimistic human message with uploaded status + paths
              const uploadedFiles: FileInMessage[] = uploadedFileInfo.map(
                (info) => ({
                  filename: info.filename,
                  size: info.size,
                  path: info.virtual_path,
                  status: "uploaded" as const,
                }),
              );
              setOptimisticMessages((messages) => {
                if (messages.length > 1 && messages[0]) {
                  const humanMessage: Message = messages[0];
                  return [
                    {
                      ...humanMessage,
                      additional_kwargs: { files: uploadedFiles },
                    },
                    ...messages.slice(1),
                  ];
                }
                return messages;
              });
            }
          } catch (error) {
            const errorMessage = uploadFailureMessage(error, {
              tooLarge: t.uploads.serverTooLarge,
              storageQuotaExceeded: t.uploads.storageQuotaExceeded,
              preflightRejected: t.uploads.preflightRejected,
              fallback: t.uploads.uploadFailed,
            });
            toast.error(errorMessage);
            setOptimisticMessages([]);
            setOptimisticThreadId(null);
            setLiveMessagesThreadId(null);
            throw error;
          } finally {
            setIsUploading(false);
          }
        }

        // Build files metadata for submission (included in additional_kwargs)
        const filesForSubmit: FileInMessage[] = uploadedFileInfo.map(
          (info) => ({
            filename: info.filename,
            size: info.size,
            path: info.virtual_path,
            status: "uploaded" as const,
          }),
        );

        await thread.submit(
          {
            messages: buildThreadSubmitMessages({
              text,
              messageId: humanMessageId,
              additionalKwargs: options?.additionalKwargs,
              additionalInputMessages: options?.additionalInputMessages,
              filesForSubmit,
            }),
          },
          {
            threadId: threadId,
            streamSubgraphs: true,
            streamResumable: true,
            config: {
              recursion_limit: 1000,
            },
            context: {
              ...extraContext,
              ...context,
              thinking_enabled: context.mode !== "flash",
              is_plan_mode: context.mode === "pro" || context.mode === "ultra",
              subagent_enabled: context.mode === "ultra",
              reasoning_effort:
                context.reasoning_effort ??
                (context.mode === "ultra"
                  ? "high"
                  : context.mode === "pro"
                    ? "medium"
                    : context.mode === "thinking"
                      ? "low"
                      : undefined),
              thread_id: threadId,
            },
          },
        );
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            "threads",
            "search",
          ),
        });
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...INFINITE_THREADS_QUERY_KEY_PREFIX,
          ),
        });
      } catch (error) {
        setOptimisticMessages([]);
        setOptimisticThreadId(null);
        setLiveMessagesThreadId(null);
        setIsUploading(false);
        throw error;
      } finally {
        sendInFlightRef.current = false;
      }
    },
    [
      thread,
      t.uploads.preflightRejected,
      t.uploads.serverTooLarge,
      t.uploads.storageQuotaExceeded,
      t.uploads.uploadFailed,
      t.uploads.uploadingFiles,
      context,
      queryClient,
      humanMessageCount,
      persistedMessages,
      privateWork,
    ],
  );

  const regenerateMessage = useCallback(
    async (
      threadId: string,
      messageId: string,
      supersededMessageIds: string[] = [messageId],
    ) => {
      if (sendInFlightRef.current || !threadId || !messageId) {
        return;
      }
      sendInFlightRef.current = true;
      prevHumanMsgCountRef.current = humanMessageCount;
      pendingUsageBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      currentRunBaselineMessageIdsRef.current = new Set(
        persistedMessages
          .map(messageIdentity)
          .filter((id): id is string => Boolean(id)),
      );
      runBaselinePreparedRef.current = true;
      setLiveMessagesThreadId(threadId);
      listeners.current.onSend?.(threadId);
      let preparedSupersededRunId: string | null = null;
      let preparedSupersededMessageIds: string[] = [];

      try {
        const response = await fetch(
          `${privateWork.apiBaseURL}/threads/${encodeURIComponent(
            threadId,
          )}/runs/regenerate/prepare`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            credentials: "include",
            body: JSON.stringify({ message_id: messageId }),
          },
        );
        if (!response.ok) {
          throw new Error(await readResponseErrorMessage(response));
        }
        const prepared = (await response.json()) as RegeneratePrepareResponse;
        preparedSupersededRunId = prepared.target_run_id;
        preparedSupersededMessageIds = supersededMessageIds;
        setPendingSupersededRunIds((current) => {
          const next = new Set(current);
          next.add(prepared.target_run_id);
          return next;
        });
        setPendingSupersededMessageIds((current) => {
          const next = new Set(current);
          for (const id of supersededMessageIds) {
            next.add(id);
          }
          return next;
        });

        await thread.submit(prepared.input, {
          threadId,
          checkpoint: prepared.checkpoint,
          metadata: prepared.metadata,
          streamSubgraphs: true,
          streamResumable: true,
          config: {
            recursion_limit: 1000,
          },
          context: {
            ...context,
            thinking_enabled: context.mode !== "flash",
            is_plan_mode: context.mode === "pro" || context.mode === "ultra",
            subagent_enabled: context.mode === "ultra",
            reasoning_effort:
              context.reasoning_effort ??
              (context.mode === "ultra"
                ? "high"
                : context.mode === "pro"
                  ? "medium"
                  : context.mode === "thinking"
                    ? "low"
                    : undefined),
            thread_id: threadId,
          },
        });
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(privateWork.scope, "thread", threadId),
        });
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            "threads",
            "search",
          ),
        });
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...INFINITE_THREADS_QUERY_KEY_PREFIX,
          ),
        });
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...threadTokenUsageQueryKey(threadId),
          ),
        });
      } catch (error) {
        setLiveMessagesThreadId(null);
        if (preparedSupersededRunId) {
          const supersededRunId = preparedSupersededRunId;
          setPendingSupersededRunIds((current) =>
            removeSetItems(current, [supersededRunId]),
          );
          setPendingSupersededMessageIds((current) =>
            removeSetItems(current, preparedSupersededMessageIds),
          );
        }
        toast.error(getStreamErrorMessage(error));
      } finally {
        sendInFlightRef.current = false;
      }
    },
    [
      context,
      humanMessageCount,
      persistedMessages,
      privateWork,
      queryClient,
      thread,
    ],
  );

  // Cache the latest thread messages in a ref to compare against incoming history messages for deduplication,
  // and to allow access to the full message list in onUpdateEvent without causing re-renders.
  if (persistedMessages.length >= messagesRef.current.length) {
    messagesRef.current = persistedMessages;
  }
  const visibleOptimisticMessages = getVisibleOptimisticMessages(
    optimisticThreadId === currentViewThreadId ? optimisticMessages : [],
    prevHumanMsgCountRef.current,
    humanMessageCount,
  );

  // Overlay the summarization rescue buffer only onto the history of the thread
  // it was captured from. visibleHistory is gated on `threadId`, so comparing the
  // same prop keeps the buffer from flashing into another thread or the new-chat
  // screen, and reading it here (instead of clearing a ref during render) is
  // concurrent-mode safe (#3825).
  const rescueBuffer = pendingArchivedMessagesRef.current;
  const effectiveHistory =
    rescueBuffer.length > 0 && pendingArchiveThreadIdRef.current === threadId
      ? resolvePreservedHistory(visibleHistory, rescueBuffer)
      : visibleHistory;
  const activeRunId = resolveActiveRunIdForMessages(
    persistedMessages,
    thread.isLoading,
    currentRunThreadIdRef.current === threadId
      ? currentRunIdRef.current
      : null,
  );
  const runBaselineMessageIds = new Set(
    currentRunBaselineMessageIdsRef.current,
  );
  for (const message of effectiveHistory) {
    const identity = messageIdentity(message);
    if (identity) {
      runBaselineMessageIds.add(identity);
    }
  }
  const runScopedPersistedMessages = attachRunIdToNewMessages(
    persistedMessages,
    activeRunId,
    runBaselineMessageIds,
  );
  const mergedMessages = mergeMessages(
    effectiveHistory,
    runScopedPersistedMessages,
    visibleOptimisticMessages,
    historyRuns,
  );
  const pendingUsageMessages = thread.isLoading
    ? getMessagesAfterBaseline(
        persistedMessages,
        pendingUsageBaselineMessageIdsRef.current,
      )
    : [];

  // Merge history, live stream, and optimistic messages for display
  // History messages may overlap with thread.messages; thread.messages take precedence
  const mergedThread = {
    ...thread,
    stop: stopThread,
    values: hasVisibleStreamState ? thread.values : EMPTY_THREAD_VALUES,
    messages: mergedMessages,
  } as typeof thread;

  return {
    thread: mergedThread,
    boundThreadId: onStreamThreadId ?? null,
    pendingUsageMessages,
    sendMessage,
    regenerateMessage,
    isUploading,
    isHistoryLoading,
    hasMoreHistory,
    loadMoreHistory,
    historyError,
    retryHistory,
    hasTerminalRunFailure: latestRunHasTerminalFailure(historyRuns),
  } as const;
}

type ThreadHistoryOptions = {
  enabled?: boolean;
  pendingSupersededRunIds?: ReadonlySet<string>;
  privateWork?: ProjectPrivateWorkScope;
};

export function useThreadHistory(
  threadId: string,
  {
    enabled = true,
    pendingSupersededRunIds,
    privateWork: explicitPrivateWork,
  }: ThreadHistoryOptions = {},
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const runs = useThreadRuns(threadId, { enabled }, privateWork);
  const threadIdRef = useRef(threadId);
  const runsRef = useRef(runs.data ?? []);
  const indexRef = useRef(-1);
  const loadingRef = useRef(false);
  const pendingLoadRef = useRef(false);
  const loadingRunIdRef = useRef<string | null>(null);
  const loadedRunIdsRef = useRef<Set<string>>(new Set());
  const runBeforeSeqRef = useRef<Map<string, number>>(new Map());
  const loadGenerationRef = useRef(0);
  const [loading, setLoading] = useState(false);
  const [messageLoadError, setMessageLoadError] = useState<Error | null>(null);
  const [messageRows, setMessageRows] = useState<RunMessage[]>([]);
  const [appendedMessages, setAppendedMessages] = useState<Message[]>([]);

  const supersededRunIds = useMemo(() => {
    return getSupersededRunIds(runs.data, pendingSupersededRunIds);
  }, [pendingSupersededRunIds, runs.data]);

  const messages = useMemo(() => {
    return buildVisibleHistoryMessages(
      messageRows,
      supersededRunIds,
      appendedMessages,
    );
  }, [appendedMessages, messageRows, supersededRunIds]);

  const loadMessages = useCallback(async () => {
    if (!enabled) {
      return;
    }
    const loadGeneration = loadGenerationRef.current;
    if (loadingRef.current) {
      const pendingRunIndex = findLatestUnloadedRunIndex(
        runsRef.current,
        loadedRunIdsRef.current,
      );
      const pendingRun = runsRef.current[pendingRunIndex];
      if (pendingRun && pendingRun.run_id !== loadingRunIdRef.current) {
        pendingLoadRef.current = true;
      }
      return;
    }
    if (runsRef.current.length === 0) {
      return;
    }

    loadingRef.current = true;
    setMessageLoadError(null);
    setLoading(true);

    try {
      let consecutiveEmptyLoads = 0;
      do {
        pendingLoadRef.current = false;

        const nextRunIndex = findLatestUnloadedRunIndex(
          runsRef.current,
          loadedRunIdsRef.current,
        );
        indexRef.current = nextRunIndex;

        const run = runsRef.current[nextRunIndex];
        if (!run) {
          indexRef.current = -1;
          return;
        }

        const requestThreadId = threadIdRef.current;
        loadingRunIdRef.current = run.run_id;
        const beforeSeq = runBeforeSeqRef.current.get(run.run_id);
        const result = await runPrivateWorkAbortable(privateWork, (signal) =>
          fetchRunMessagesPage(
            privateWork.apiBaseURL,
            requestThreadId,
            run.run_id,
            beforeSeq,
            signal,
          ),
        );
        if (
          loadGenerationRef.current !== loadGeneration ||
          threadIdRef.current !== requestThreadId
        ) {
          return;
        }
        const _messages = result.data;
        setMessageRows((prev) =>
          mergeRunMessageRows(prev, _messages, runsRef.current),
        );
        const nextBeforeSeq = getNextRunMessagesBeforeSeq(result);
        if (typeof nextBeforeSeq === "number") {
          runBeforeSeqRef.current.set(run.run_id, nextBeforeSeq);
          pendingLoadRef.current = true;
        } else if (nextBeforeSeq === undefined) {
          throw new Error(
            `Run ${run.run_id} returned a non-advancing message page.`,
          );
        } else {
          runBeforeSeqRef.current.delete(run.run_id);
          loadedRunIdsRef.current.add(run.run_id);
          if (
            shouldAutoContinueOnEmptyRun(
              filterVisibleHistoryRows(_messages).length,
              consecutiveEmptyLoads,
            )
          ) {
            consecutiveEmptyLoads += 1;
            pendingLoadRef.current = true;
          } else {
            consecutiveEmptyLoads = 0;
          }
        }
        indexRef.current = findLatestUnloadedRunIndex(
          runsRef.current,
          loadedRunIdsRef.current,
        );
      } while (pendingLoadRef.current);
    } catch (err) {
      pendingLoadRef.current = false;
      if (loadGenerationRef.current === loadGeneration) {
        setMessageLoadError(
          err instanceof Error
            ? err
            : new Error("Failed to load thread history."),
        );
      }
    } finally {
      if (loadGenerationRef.current === loadGeneration) {
        loadingRef.current = false;
        loadingRunIdRef.current = null;
        setLoading(false);
      }
    }
  }, [enabled, privateWork]);
  useEffect(() => {
    const threadChanged = threadIdRef.current !== threadId;
    threadIdRef.current = threadId;

    if (!enabled || threadChanged) {
      loadGenerationRef.current += 1;
      runsRef.current = [];
      indexRef.current = -1;
      pendingLoadRef.current = false;
      loadingRunIdRef.current = null;
      loadedRunIdsRef.current = new Set();
      runBeforeSeqRef.current = new Map();
      loadingRef.current = false;
      setLoading(false);
      setMessageLoadError(null);
      setMessageRows([]);
      setAppendedMessages([]);
    }

    if (!enabled) {
      return;
    }

    if (runs.data && runs.data.length > 0) {
      runsRef.current = runs.data ?? [];
      indexRef.current = findLatestUnloadedRunIndex(
        runs.data,
        loadedRunIdsRef.current,
      );
    }
    void loadMessages();
  }, [enabled, threadId, runs.data, loadMessages]);

  const appendMessages = useCallback((_messages: Message[]) => {
    setAppendedMessages((prev) => {
      return dedupeMessagesByIdentity([...prev, ..._messages]);
    });
  }, []);
  const hasThreadId = Boolean(threadId);
  const hasUnloadedRuns = Boolean(
    runs.data?.some((run) => !loadedRunIdsRef.current.has(run.run_id)),
  );
  const isRunsLoading =
    enabled &&
    hasThreadId &&
    (runs.isLoading || (runs.isFetching && !runs.data));
  const isRunsUnresolved =
    enabled && hasThreadId && !runs.data && !runs.isError;
  const historyError = messageLoadError ?? runs.error;
  const hasMore =
    enabled &&
    hasThreadId &&
    historyError === null &&
    (indexRef.current >= 0 || hasUnloadedRuns);
  const runsFailed = runs.isError;
  const refetchRuns = runs.refetch;
  const retryHistory = useCallback(() => {
    if (runsFailed) {
      void refetchRuns();
      return;
    }
    void loadMessages();
  }, [loadMessages, refetchRuns, runsFailed]);
  return {
    runs: runs.data,
    messages,
    loading: loading || isRunsLoading || isRunsUnresolved,
    appendMessages,
    hasMore,
    loadMore: loadMessages,
    error: historyError,
    retry: retryHistory,
  };
}

export function useThreads(
  params: ThreadSearchParams = DEFAULT_THREAD_SEARCH_PARAMS,
  explicitPrivateWork?: ProjectPrivateWorkScope,
  queryOptions: { enabled?: boolean } = {},
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const options = buildThreadsSearchQueryOptions(privateWork.client, params);
  return useQuery<AgentThread[]>({
    ...options,
    queryKey: scopedThreadQueryKey(privateWork.scope, ...options.queryKey),
    ...{ enabled: queryOptions.enabled },
  });
}

export const INFINITE_THREADS_PAGE_SIZE = 50;

export const INFINITE_THREADS_QUERY_KEY_PREFIX = [
  "threads",
  "searchInfinite",
] as const;

const INFINITE_THREADS_NEXT_PAGE_PARAM = Symbol(
  "deerflow.infiniteThreads.nextPageParam",
);

type InfiniteThreadsParams = Omit<
  Parameters<ThreadsClient["search"]>[0],
  "limit" | "offset"
>;

type InfiniteThreadsSearchClient = {
  threads: {
    search: ThreadsClient["search"];
  };
};

type InfiniteThreadsPageWithNextParam = AgentThread[] & {
  [INFINITE_THREADS_NEXT_PAGE_PARAM]?: number;
};

function annotateInfiniteThreadsPage(
  page: AgentThread[],
  nextPageParam: number | undefined,
): AgentThread[] {
  if (nextPageParam !== undefined) {
    Reflect.set(page, INFINITE_THREADS_NEXT_PAGE_PARAM, nextPageParam);
  }
  return page;
}

export async function fetchInfiniteThreadsPage(
  apiClient: InfiniteThreadsSearchClient,
  params: InfiniteThreadsParams,
  pageParam: number,
  pageSize: number = INFINITE_THREADS_PAGE_SIZE,
  signal?: AbortSignal,
): Promise<AgentThread[]> {
  const threads: AgentThread[] = [];
  let offset = pageParam;
  let nextPageParam: number | undefined;

  while (threads.length < pageSize) {
    const currentLimit = pageSize - threads.length;
    const response = (await apiClient.threads.search<AgentThreadState>({
      ...params,
      ...(signal ? { signal } : {}),
      limit: currentLimit,
      offset,
    })) as AgentThread[];

    threads.push(...filterThreadSearchResults(response, params));
    offset += response.length;

    if (response.length < currentLimit) {
      nextPageParam = undefined;
      break;
    }

    nextPageParam = offset;
  }

  return annotateInfiniteThreadsPage(threads, nextPageParam);
}

export function getInfiniteThreadsNextPageParam(
  lastPage: AgentThread[],
  allPages: AgentThread[][],
  pageSize: number = INFINITE_THREADS_PAGE_SIZE,
): number | undefined {
  const annotatedNextPageParam = Reflect.get(
    lastPage as InfiniteThreadsPageWithNextParam,
    INFINITE_THREADS_NEXT_PAGE_PARAM,
  );
  if (typeof annotatedNextPageParam === "number") {
    return annotatedNextPageParam;
  }

  if (lastPage.length < pageSize) {
    return undefined;
  }
  return allPages.reduce((sum, page) => sum + page.length, 0);
}

export function mapInfiniteThreadsCache(
  oldData: InfiniteData<AgentThread[]> | undefined,
  mapper: (thread: AgentThread) => AgentThread,
): InfiniteData<AgentThread[]> | undefined {
  if (!oldData) {
    return oldData;
  }
  return {
    ...oldData,
    pages: oldData.pages.map((page) => page.map(mapper)),
  };
}

export function filterInfiniteThreadsCache(
  oldData: InfiniteData<AgentThread[]> | undefined,
  predicate: (thread: AgentThread) => boolean,
): InfiniteData<AgentThread[]> | undefined {
  if (!oldData) {
    return oldData;
  }
  return {
    ...oldData,
    pages: oldData.pages.map((page) => page.filter(predicate)),
  };
}

export function useInfiniteThreads(
  params: InfiniteThreadsParams = {
    sortBy: "updated_at",
    sortOrder: "desc",
    select: ["thread_id", "updated_at", "values", "metadata"],
  },
  explicitPrivateWork?: ProjectPrivateWorkScope,
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useInfiniteQuery<
    AgentThread[],
    Error,
    InfiniteData<AgentThread[]>,
    readonly unknown[],
    number
  >({
    queryKey: scopedThreadQueryKey(
      privateWork.scope,
      ...INFINITE_THREADS_QUERY_KEY_PREFIX,
      params,
    ),
    initialPageParam: 0,
    queryFn: async ({ pageParam, signal }) =>
      fetchInfiniteThreadsPage(
        privateWork.client,
        params,
        pageParam,
        INFINITE_THREADS_PAGE_SIZE,
        signal,
      ),
    getNextPageParam: (lastPage, allPages) =>
      getInfiniteThreadsNextPageParam(lastPage, allPages),
    refetchOnWindowFocus: false,
  });
}

export function useThreadRuns(
  threadId?: string,
  { enabled = true }: { enabled?: boolean } = {},
  explicitPrivateWork?: ProjectPrivateWorkScope,
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery<Run[]>({
    queryKey: scopedThreadQueryKey(privateWork.scope, "thread", threadId),
    queryFn: async () => {
      if (!threadId) {
        return [];
      }
      const response = await privateWork.client.runs.list(threadId);
      return response;
    },
    enabled: enabled && Boolean(threadId),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useThreadMetadata(
  threadId?: string | null,
  {
    enabled = true,
    isMock = false,
    privateWork: explicitPrivateWork,
  }: {
    enabled?: boolean;
    isMock?: boolean;
    privateWork?: ProjectPrivateWorkScope;
  } = {},
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery<AgentThread | null>({
    queryKey: scopedThreadQueryKey(
      privateWork.scope,
      "thread",
      "metadata",
      threadId,
      isMock,
    ),
    queryFn: async () => {
      if (!threadId) {
        return null;
      }
      try {
        const response = await privateWork.client.threads.get(threadId);
        return response as AgentThread;
      } catch (error) {
        if (isThreadMissingError(error)) {
          return null;
        }
        throw error;
      }
    },
    enabled: enabled && Boolean(threadId),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useThreadTokenUsage(
  threadId?: string | null,
  {
    enabled = true,
    privateWork: explicitPrivateWork,
  }: { enabled?: boolean; privateWork?: ProjectPrivateWorkScope } = {},
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery<ThreadTokenUsageResponse | null>({
    queryKey: scopedThreadQueryKey(
      privateWork.scope,
      ...threadTokenUsageQueryKey(threadId),
    ),
    queryFn: async () => {
      if (!threadId) {
        return null;
      }
      return fetchThreadTokenUsage(threadId, privateWork);
    },
    enabled: enabled && Boolean(threadId),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useBranchThread(explicitPrivateWork?: ProjectPrivateWorkScope) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      threadId,
      messageId,
      messageIds,
      title,
    }: {
      threadId: string;
      messageId: string;
      messageIds?: string[];
      title?: string;
    }) =>
      branchThreadFromTurn(
        threadId,
        { messageId, messageIds, title },
        privateWork,
      ),
    onSuccess(response, { threadId }) {
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(
          privateWork.scope,
          "thread",
          "metadata",
          response.thread_id,
        ),
      });
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(
          privateWork.scope,
          "thread",
          "metadata",
          threadId,
        ),
      });
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(privateWork.scope, "threads", "search"),
      });
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(
          privateWork.scope,
          ...INFINITE_THREADS_QUERY_KEY_PREFIX,
        ),
      });
    },
  });
}

export function useRunDetail(
  threadId: string,
  runId: string,
  explicitPrivateWork?: ProjectPrivateWorkScope,
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery<Run>({
    queryKey: scopedThreadQueryKey(
      privateWork.scope,
      "thread",
      threadId,
      "run",
      runId,
    ),
    queryFn: async () => {
      const response = await privateWork.client.runs.get(threadId, runId);
      return response;
    },
    refetchOnWindowFocus: false,
  });
}

export async function deleteThreadEverywhere(
  apiClient: ThreadDeleteClient,
  threadId: string,
  _privateWork: ProjectPrivateWorkScope,
) {
  await apiClient.threads.delete(threadId);
}

export async function findSidecarThreadIdsForParent(
  apiClient: ThreadSidecarSearchClient,
  parentThreadId: string,
) {
  const threadIds: string[] = [];
  const limit = 100;
  let offset = 0;

  while (true) {
    const response = await apiClient.threads.search({
      metadata: {
        [SIDECAR_METADATA_KEY]: true,
        parent_thread_id: parentThreadId,
      },
      limit,
      offset,
      sortBy: "updated_at",
      sortOrder: "desc",
      select: ["thread_id", "metadata"],
    });

    for (const thread of response) {
      if (
        isSidecarThread(thread) &&
        thread.metadata?.parent_thread_id === parentThreadId
      ) {
        threadIds.push(thread.thread_id);
      }
    }

    if (response.length < limit) {
      break;
    }
    offset += response.length;
  }

  return threadIds;
}

async function deleteSidecarThreadsForParent(
  apiClient: ThreadDeleteClient,
  parentThreadId: string,
  privateWork: ProjectPrivateWorkScope,
) {
  let sidecarThreadIds: string[];
  try {
    sidecarThreadIds = await findSidecarThreadIdsForParent(
      apiClient,
      parentThreadId,
    );
  } catch (err) {
    console.warn(
      `Failed to look up sidecar threads for parent ${parentThreadId}; skipping cascade cleanup. Orphaned sidecar threads may remain.`,
      err,
    );
    return [];
  }

  const results = await Promise.allSettled(
    sidecarThreadIds.map((threadId) =>
      deleteThreadEverywhere(apiClient, threadId, privateWork),
    ),
  );

  const failedDeletions = results
    .map((result, index) =>
      result.status === "rejected"
        ? { threadId: sidecarThreadIds[index], reason: result.reason }
        : null,
    )
    .filter((entry): entry is { threadId: string; reason: unknown } =>
      Boolean(entry),
    );

  if (failedDeletions.length > 0) {
    console.warn(
      `Failed to delete ${failedDeletions.length} sidecar thread(s) for parent ${parentThreadId}; orphaned sidecar threads may remain.`,
      failedDeletions,
    );
  }

  return sidecarThreadIds.filter((_, index) => {
    return results[index]?.status === "fulfilled";
  });
}

export function useDeleteThread(explicitPrivateWork?: ProjectPrivateWorkScope) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();
  const apiClient = privateWork.client as ThreadDeleteClient;
  return useMutation({
    mutationFn: async ({
      threadId,
      onRemoteDeleted,
    }: {
      threadId: string;
      onRemoteDeleted?: () => void;
    }) => {
      const deletedSidecarThreadIds = await deleteSidecarThreadsForParent(
        apiClient,
        threadId,
        privateWork,
      );
      await deleteThreadEverywhere(apiClient, threadId, privateWork);
      onRemoteDeleted?.();
      return deletedSidecarThreadIds;
    },
    onSuccess(deletedSidecarThreadIds, { threadId }) {
      const deletedThreadIds = new Set([threadId, ...deletedSidecarThreadIds]);
      queryClient.setQueriesData(
        {
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            "threads",
            "search",
          ),
          exact: false,
        },
        (oldData: Array<AgentThread> | undefined) => {
          if (oldData == null) {
            return oldData;
          }
          return oldData.filter((t) => !deletedThreadIds.has(t.thread_id));
        },
      );
      queryClient.setQueriesData(
        {
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...INFINITE_THREADS_QUERY_KEY_PREFIX,
          ),
          exact: false,
        },
        (oldData: InfiniteData<AgentThread[]> | undefined) =>
          filterInfiniteThreadsCache(
            oldData,
            (t) => !deletedThreadIds.has(t.thread_id),
          ),
      );
    },

    onSettled() {
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(privateWork.scope, "threads", "search"),
      });
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(
          privateWork.scope,
          ...INFINITE_THREADS_QUERY_KEY_PREFIX,
        ),
      });
    },
  });
}

export function useRenameThread(explicitPrivateWork?: ProjectPrivateWorkScope) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();
  const apiClient = privateWork.client;
  return useMutation({
    mutationFn: async ({
      threadId,
      title,
    }: {
      threadId: string;
      title: string;
    }) => {
      await apiClient.threads.updateState(threadId, {
        values: { title },
      });
    },
    onSuccess(_, { threadId, title }) {
      queryClient.setQueriesData(
        {
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            "threads",
            "search",
          ),
          exact: false,
        },
        (oldData: Array<AgentThread>) => {
          return oldData.map((t) => {
            if (t.thread_id === threadId) {
              return {
                ...t,
                values: {
                  ...t.values,
                  title,
                },
              };
            }
            return t;
          });
        },
      );
      queryClient.setQueriesData(
        {
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...INFINITE_THREADS_QUERY_KEY_PREFIX,
          ),
          exact: false,
        },
        (oldData: InfiniteData<AgentThread[]> | undefined) =>
          mapInfiniteThreadsCache(oldData, (t) =>
            t.thread_id === threadId
              ? {
                  ...t,
                  values: {
                    ...t.values,
                    title,
                  },
                }
              : t,
          ),
      );
    },
  });
}
