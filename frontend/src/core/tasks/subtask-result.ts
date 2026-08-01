import type { Message } from "@langchain/langgraph-sdk";

import { normalizeTokenUsage } from "../messages/usage";

import type { Subtask } from "./types";

export type SubtaskStatus = Subtask["status"];

export interface SubtaskResultUpdate {
  status: SubtaskStatus;
  result?: string;
  error?: string;
  modelName?: string;
  usage?: Subtask["usage"];
  /**
   * Why a guardrail cap ended the run early (``token_capped`` / ``turn_capped``
   * / ``loop_capped``), when the backend stamps ``subagent_stop_reason``. A
   * capped run keeps a normal pill status — ``completed`` when it produced a
   * final answer, ``failed`` when it did not — so this field is the only
   * signal that distinguishes "finished" from "capped" (#3875 Phase 2).
   */
  stopReason?: string;
}

/**
 * Structured-status keys the backend stamps onto
 * ``ToolMessage.additional_kwargs`` for every ``task`` tool result.
 *
 * The values mirror the Python contract in
 * ``backend/packages/harness/deerflow/subagents/status_contract.py``
 * (``SUBAGENT_STATUS_KEY`` / ``SUBAGENT_ERROR_KEY`` /
 * ``SUBAGENT_RESULT_BRIEF_KEY`` / ``SUBAGENT_RESULT_SHA256_KEY``). The
 * result metadata fields are optional and bounded: ``subagent_result_brief``
 * carries a trimmed summary for completed tasks and
 * ``subagent_result_sha256`` carries the full-result digest. The
 * frontend keeps the same wire keys so it can interpret backend task results.
 */
export const SUBAGENT_STATUS_KEY = "subagent_status";
export const SUBAGENT_STOP_REASON_KEY = "subagent_stop_reason";
export const SUBAGENT_ERROR_KEY = "subagent_error";
export const SUBAGENT_RESULT_BRIEF_KEY = "subagent_result_brief";
export const SUBAGENT_RESULT_SHA256_KEY = "subagent_result_sha256";
export const SUBAGENT_MODEL_NAME_KEY = "subagent_model_name";
export const SUBAGENT_TOKEN_USAGE_KEY = "subagent_token_usage";
/**
 * Why a guardrail cap ended a subagent run early (#3875 Phase 2). Mirrors the
 * Python ``SUBAGENT_STOP_REASON_VALUES``. The field is optional/additive —
 * older frontends that only read ``subagent_status`` simply never see it.
 */
const SUBAGENT_STOP_REASON_VALUES = [
  "token_capped",
  "turn_capped",
  "loop_capped",
] as const;
const STRUCTURED_SUBAGENT_KEYS = [
  SUBAGENT_STATUS_KEY,
  SUBAGENT_STOP_REASON_KEY,
  SUBAGENT_ERROR_KEY,
  SUBAGENT_RESULT_BRIEF_KEY,
  SUBAGENT_RESULT_SHA256_KEY,
  SUBAGENT_MODEL_NAME_KEY,
  SUBAGENT_TOKEN_USAGE_KEY,
];

const SUCCESS_PREFIX = "Task Succeeded. Result:";
const FAILURE_PREFIX = "Task failed.";
const TIMEOUT_PREFIX = "Task timed out";
const CANCELLED_PREFIX = "Task cancelled by user.";
const POLLING_TIMEOUT_PREFIX = "Task polling timed out";
const ERROR_WRAPPER_PATTERN = /^Error\b/i;

/**
 * Map from the backend ``subagent_status`` value to the frontend
 * {@link SubtaskStatus} enum. The frontend collapses ``cancelled`` /
 * ``timed_out`` / ``polling_timed_out`` into ``failed`` because the
 * subtask card only renders three pill states. The richer backend
 * vocabulary still survives on ``error`` for tooling that wants the
 * detail.
 *
 * ``max_turns_reached`` is kept as a **deprecated read-only alias**: Phase 1
 * (#3949) wrote it into ``ToolMessage.additional_kwargs``, which is checkpointed
 * in thread history, so old turns still carry it. Phase 2 (#3980) stopped
 * producing it (the cap now rides on ``subagent_stop_reason``), but without this
 * alias those historical cards would strand as a spinning ``in_progress`` pill
 * forever (``readStructuredStatus`` would return null yet
 * ``hasStructuredSubagentMetadata`` stays true from the sibling keys). Mapping
 * it to ``failed`` keeps them terminal, matching how Phase 1 itself rendered it.
 * No code path produces this value anymore; it is read-side tolerance only.
 */
const STRUCTURED_STATUS_TO_SUBTASK: Record<string, SubtaskStatus> = {
  completed: "completed",
  failed: "failed",
  cancelled: "failed",
  timed_out: "failed",
  polling_timed_out: "failed",
  max_turns_reached: "failed",
};

/**
 * Map a `task` tool result to a {@link SubtaskStatus}.
 *
 * The backend writes task lifecycle facts into
 * ``ToolMessage.additional_kwargs``. The textual ``content`` remains
 * model-visible display content only; it is not parsed as a protocol.
 *
 * Returning `in_progress` is the **deliberate** default for content that
 * carries no structured stamp.
 * LangChain only ever emits a `ToolMessage` once the tool itself has
 * returned (success or wrapped exception), so an unknown shape means
 * "the contract changed underneath us" — surfacing it as still-running
 * prompts the operator to investigate, where eagerly marking it
 * terminal-failed would mask the drift.
 */
export function parseSubtaskResult(
  text: string,
  additionalKwargs?: Record<string, unknown> | null,
): SubtaskResultUpdate {
  const structured = readStructuredStatus(additionalKwargs);
  if (!structured) {
    if (!hasStructuredSubagentMetadata(additionalKwargs)) {
      return parseLegacyTaskResult(text.trim());
    }
    return { status: "in_progress" };
  }

  const update: SubtaskResultUpdate = { status: structured.status };
  if (structured.error) {
    update.error = structured.error;
  }
  const structuredResult = readStructuredResultBrief(additionalKwargs);
  if (structured.status === "completed" && structuredResult) {
    update.result = structuredResult;
  }
  const stopReason = readStructuredStopReason(additionalKwargs);
  if (stopReason) {
    update.stopReason = stopReason;
  }
  const modelName = readStructuredModelName(additionalKwargs);
  if (modelName) {
    update.modelName = modelName;
  }
  const usage = readStructuredTokenUsage(additionalKwargs);
  if (usage) {
    update.usage = usage;
  }
  return update;
}

function parseLegacyTaskResult(trimmed: string): SubtaskResultUpdate {
  if (trimmed.startsWith(SUCCESS_PREFIX)) {
    return {
      status: "completed",
      result: trimmed.slice(SUCCESS_PREFIX.length).trim(),
    };
  }

  if (trimmed.startsWith(FAILURE_PREFIX)) {
    return {
      status: "failed",
      error: trimmed.slice(FAILURE_PREFIX.length).trim(),
    };
  }

  if (trimmed.startsWith(TIMEOUT_PREFIX)) {
    return { status: "failed", error: trimmed };
  }

  if (trimmed.startsWith(CANCELLED_PREFIX)) {
    return { status: "failed", error: trimmed };
  }

  if (trimmed.startsWith(POLLING_TIMEOUT_PREFIX)) {
    return { status: "failed", error: trimmed };
  }

  if (ERROR_WRAPPER_PATTERN.test(trimmed)) {
    return { status: "failed", error: trimmed };
  }

  return { status: "in_progress" };
}

export function hasSubtaskToolResult(
  toolCallId: string | undefined,
  messages: Message[],
) {
  if (!toolCallId) {
    return false;
  }
  return messages.some(
    (message) => message.type === "tool" && message.tool_call_id === toolCallId,
  );
}

export function derivePendingSubtaskStatus(
  toolCallId: string | undefined,
  messages: Message[],
  isOwningRunActive: boolean,
): SubtaskStatus {
  if (isOwningRunActive || hasSubtaskToolResult(toolCallId, messages)) {
    return "in_progress";
  }
  return "failed";
}

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

/**
 * Decide whether a subtask group belongs to the currently running parent Run.
 *
 * A subtask group can stop being the last visible group while the Lead Agent
 * continues with other tools or starts another subtask. Run identity, rather
 * than group position, is therefore the stable ownership signal. Looking up
 * the latest Run ID also restores the correct running state after navigating
 * away from an active conversation and reconnecting.
 */
export function isSubtaskRunActive(
  groupMessages: Message[],
  allMessages: Message[],
  isRunLoading: boolean,
): boolean {
  if (!isRunLoading) {
    return false;
  }

  const activeRunId = [...allMessages]
    .reverse()
    .map(messageRunId)
    .find((runId): runId is string => Boolean(runId));
  if (!activeRunId) {
    return false;
  }

  return groupMessages.some((message) => messageRunId(message) === activeRunId);
}

export type SubtaskTerminalEventUpdate = SubtaskResultUpdate & {
  id: string;
};

/**
 * Parse authoritative terminal task lifecycle events emitted on the live
 * custom-event stream. ToolMessage metadata may arrive later and remains the
 * highest-priority result, but the card should transition immediately instead
 * of guessing from message-group position.
 */
export function parseSubtaskTerminalEvent(
  event: unknown,
): SubtaskTerminalEventUpdate | null {
  if (typeof event !== "object" || event === null) {
    return null;
  }

  const type = Reflect.get(event, "type");
  const taskId = Reflect.get(event, "task_id");
  if (typeof taskId !== "string" || taskId.length === 0) {
    return null;
  }

  if (type === "task_completed") {
    const result = Reflect.get(event, "result");
    return {
      id: taskId,
      status: "completed",
      ...(typeof result === "string" && result.length > 0 ? { result } : {}),
      ...readEventRuntimeMetadata(event),
    };
  }

  if (
    type === "task_failed" ||
    type === "task_cancelled" ||
    type === "task_timed_out"
  ) {
    const error = Reflect.get(event, "error");
    return {
      id: taskId,
      status: "failed",
      ...(typeof error === "string" && error.length > 0 ? { error } : {}),
      ...readEventRuntimeMetadata(event),
    };
  }

  return null;
}

interface StructuredStatus {
  status: SubtaskStatus;
  error?: string;
}

function readStructuredStatus(
  additionalKwargs: Record<string, unknown> | null | undefined,
): StructuredStatus | null {
  if (!additionalKwargs) return null;
  const rawStatus = additionalKwargs[SUBAGENT_STATUS_KEY];
  if (typeof rawStatus !== "string") return null;
  const mapped = STRUCTURED_STATUS_TO_SUBTASK[rawStatus];
  if (mapped === undefined) {
    return null;
  }
  const rawError = additionalKwargs[SUBAGENT_ERROR_KEY];
  const result: StructuredStatus = { status: mapped };
  if (typeof rawError === "string" && rawError.trim()) {
    result.error = rawError;
  }
  return result;
}

function hasStructuredSubagentMetadata(
  additionalKwargs: Record<string, unknown> | null | undefined,
): boolean {
  if (!additionalKwargs) return false;
  return STRUCTURED_SUBAGENT_KEYS.some((key) =>
    Object.prototype.hasOwnProperty.call(additionalKwargs, key),
  );
}

function readStructuredResultBrief(
  additionalKwargs: Record<string, unknown> | null | undefined,
): string | undefined {
  const value = additionalKwargs?.[SUBAGENT_RESULT_BRIEF_KEY];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function readStructuredStopReason(
  additionalKwargs: Record<string, unknown> | null | undefined,
): string | undefined {
  const value = additionalKwargs?.[SUBAGENT_STOP_REASON_KEY];
  if (typeof value !== "string") return undefined;
  return SUBAGENT_STOP_REASON_VALUES.includes(
    value as (typeof SUBAGENT_STOP_REASON_VALUES)[number],
  )
    ? value
    : undefined;
}

function readStructuredModelName(
  additionalKwargs: Record<string, unknown> | null | undefined,
): string | undefined {
  const value = additionalKwargs?.[SUBAGENT_MODEL_NAME_KEY];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function readStructuredTokenUsage(
  additionalKwargs: Record<string, unknown> | null | undefined,
): Subtask["usage"] | undefined {
  return normalizeTokenUsage(additionalKwargs?.[SUBAGENT_TOKEN_USAGE_KEY]);
}

function readEventRuntimeMetadata(
  event: object,
): Pick<Subtask, "modelName" | "usage"> {
  const modelName = Reflect.get(event, "model_name");
  const usage = normalizeTokenUsage(Reflect.get(event, "usage"));
  return {
    ...(typeof modelName === "string" && modelName.trim()
      ? { modelName: modelName.trim() }
      : {}),
    ...(usage ? { usage } : {}),
  };
}
