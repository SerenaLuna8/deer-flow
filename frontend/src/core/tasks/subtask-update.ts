import { mergeSteps } from "./steps";
import type { Subtask, SubtaskStatusSource } from "./types";

const STATUS_SOURCE_RANK: Record<SubtaskStatusSource, number> = {
  inferred: 0,
  custom_event: 1,
  tool_result: 2,
};

export function isTerminalSubtaskStatus(status: Subtask["status"] | undefined) {
  return status === "completed" || status === "failed";
}

/**
 * Pure state transition for a single subtask update (#3779).
 *
 * Kept separate from the React hook so it can be unit-tested and, crucially, so
 * the hook can compute `next` from the *latest* `previous` handed to a
 * functional `setTasks` updater — not a stale `tasks` snapshot captured in a
 * closure. Deriving `next` from whatever `previous` the caller passes is what
 * lets an in-flight `fetchSubtaskSteps().then(...)` merge into current state
 * instead of clobbering SSE steps / sibling subtasks that arrived meanwhile.
 *
 * `steps` are treated as deltas: they are merged into `previous.steps`
 * (deduped/ordered by message_index) rather than replacing them, so live SSE
 * steps and fetched-on-expand backfill build one timeline.
 */
export function computeNextSubtask(
  previous: Subtask | undefined,
  task: Partial<Subtask> & { id: string },
): { next: Subtask; becameTerminal: boolean; changed: boolean } {
  const previousStatus = previous?.status;
  const previousSource = previous?.statusSource;
  const incomingSource = task.statusSource;
  const hasLowerAuthority =
    previousSource !== undefined &&
    incomingSource !== undefined &&
    STATUS_SOURCE_RANK[incomingSource] < STATUS_SOURCE_RANK[previousSource];
  const preserveTerminalStatus =
    isTerminalSubtaskStatus(previousStatus) &&
    (task.status === "in_progress" || hasLowerAuthority);

  // MessageList writes the pending task tool-call state before parsing the
  // matching ToolMessage in the same render. Keep authoritative terminal
  // results stable across later inferred renders so the card cannot regress
  // from completed to the synthetic "failed" fallback.
  const next: Subtask = {
    ...previous,
    ...task,
  } as Subtask;

  if (preserveTerminalStatus && previous) {
    next.status = previous.status;
    next.statusSource = previous.statusSource;
    next.result = previous.result;
    next.error = previous.error;
    next.stopReason = previous.stopReason;
  } else if (task.status === "completed") {
    delete next.error;
  } else if (task.status === "failed") {
    delete next.result;
  }

  if (task.steps) {
    next.steps = mergeSteps(previous?.steps ?? [], task.steps);
  }

  // Usage events are cumulative snapshots. A delayed older frame must not
  // make the card appear to have spent fewer tokens than it already reported.
  if (
    task.usage &&
    previous?.usage &&
    task.usage.totalTokens < previous.usage.totalTokens
  ) {
    next.usage = previous.usage;
  }

  const becameTerminal =
    isTerminalSubtaskStatus(next.status) && previousStatus !== next.status;

  return { next, becameTerminal, changed: subtaskChanged(previous, next) };
}

function subtaskChanged(previous: Subtask | undefined, next: Subtask): boolean {
  if (!previous) {
    return true;
  }
  return (
    previous.status !== next.status ||
    previous.statusSource !== next.statusSource ||
    previous.modelName !== next.modelName ||
    previous.result !== next.result ||
    previous.error !== next.error ||
    previous.stopReason !== next.stopReason ||
    previous.subagent_type !== next.subagent_type ||
    previous.description !== next.description ||
    previous.prompt !== next.prompt ||
    previous.latestMessage !== next.latestMessage ||
    previous.steps !== next.steps ||
    !usageEquals(previous.usage, next.usage)
  );
}

function usageEquals(a: Subtask["usage"], b: Subtask["usage"]): boolean {
  if (a === b) {
    return true;
  }
  if (!a || !b) {
    return false;
  }
  return (
    a.inputTokens === b.inputTokens &&
    a.outputTokens === b.outputTokens &&
    a.totalTokens === b.totalTokens
  );
}
