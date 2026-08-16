import type { Message, Run } from "@langchain/langgraph-sdk";

import {
  isOutputDeliveryIncompleteError,
  isModelOutputLimitError,
  MODEL_OUTPUT_LIMIT,
  OUTPUT_DELIVERY_INCOMPLETE,
  type ProjectRunFailureCode,
} from "../private-work/api-client";
import {
  compareEventSequences,
  type EventSequence,
} from "../private-work/event-sequence";

import type { RunMessagesPageResponse } from "./api";
import {
  dedupeMessagesByIdentity,
  messageIdentity,
} from "./message-projection";
import type { RunMessage } from "./types";

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
      if (typeof leftSeq === "string" && typeof rightSeq === "string") {
        return (
          compareEventSequences(leftSeq, rightSeq) || left.index - right.index
        );
      }
      if (typeof leftSeq === "string") {
        return -1;
      }
      if (typeof rightSeq === "string") {
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

export function latestRunFailureCode(
  runs: Run[] | undefined,
): ProjectRunFailureCode | null {
  const latestRun = runs?.[0];
  if (!latestRunHasTerminalFailure(runs) || !latestRun) {
    return null;
  }
  const error = Reflect.get(latestRun, "error");
  if (isModelOutputLimitError(error)) {
    return MODEL_OUTPUT_LIMIT;
  }
  return isOutputDeliveryIncompleteError(error)
    ? OUTPUT_DELIVERY_INCOMPLETE
    : null;
}

export function resolveRunFailureCode(
  streamError: unknown,
  runs: Run[] | undefined,
): ProjectRunFailureCode | null {
  if (isModelOutputLimitError(streamError)) {
    return MODEL_OUTPUT_LIMIT;
  }
  return isOutputDeliveryIncompleteError(streamError)
    ? OUTPUT_DELIVERY_INCOMPLETE
    : latestRunFailureCode(runs);
}

export function resolveRunFailureRunId(
  streamError: unknown,
  activeRunId: string | null,
  runs: Run[] | undefined,
): string | null {
  if (isModelOutputLimitError(streamError) && activeRunId) {
    return activeRunId;
  }
  return latestRunFailureCode(runs) === MODEL_OUTPUT_LIMIT
    ? (runs?.[0]?.run_id ?? null)
    : null;
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

export function filterVisibleHistoryRows(
  messageRows: RunMessage[],
): RunMessage[] {
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
    messageRows.filter((message) => !supersededRunIds.has(message.run_id)),
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
  let oldestSeq: EventSequence | null = null;
  for (const message of messages) {
    if (typeof message.seq !== "string") {
      continue;
    }
    oldestSeq =
      oldestSeq === null || compareEventSequences(message.seq, oldestSeq) < 0
        ? message.seq
        : oldestSeq;
  }
  return oldestSeq;
}

export function getNextRunMessagesBeforeSeq(
  result: RunMessagesPageResponse,
): EventSequence | null | undefined {
  if (!runMessagesPageHasMore(result)) {
    return null;
  }
  return getOldestRunMessageSeq(result.data) ?? undefined;
}
