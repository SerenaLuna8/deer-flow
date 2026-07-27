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
): { next: Subtask; becameTerminal: boolean } {
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

  const becameTerminal =
    isTerminalSubtaskStatus(next.status) && previousStatus !== next.status;

  return { next, becameTerminal };
}
