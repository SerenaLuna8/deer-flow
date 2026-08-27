import { normalizeTokenUsage } from "../messages/usage";

import type { Subtask } from "./types";

/** Convert additive task lifecycle metadata into a task-state update. */
export function taskEventToSubtaskUpdate(
  event: unknown,
): (Partial<Subtask> & { id: string }) | null {
  if (typeof event !== "object" || event === null) {
    return null;
  }
  const type = Reflect.get(event, "type");
  const taskId = Reflect.get(event, "task_id");
  if (typeof taskId !== "string" || !taskId.trim()) {
    return null;
  }

  const rawModelName = Reflect.get(event, "model_name");
  const modelName =
    typeof rawModelName === "string" && rawModelName.trim()
      ? rawModelName.trim()
      : undefined;
  const rawExecutionId = Reflect.get(event, "execution_id");
  const executionId =
    typeof rawExecutionId === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu.test(
      rawExecutionId.trim(),
    )
      ? rawExecutionId.trim().toLowerCase()
      : undefined;

  if (type === "task_started") {
    return modelName || executionId
      ? {
          id: taskId,
          ...(modelName ? { modelName } : {}),
          ...(executionId ? { executionId } : {}),
        }
      : null;
  }
  if (type !== "task_running") {
    return null;
  }

  const usage = normalizeTokenUsage(Reflect.get(event, "usage"));
  return modelName || usage || executionId
    ? {
        id: taskId,
        ...(modelName ? { modelName } : {}),
        ...(executionId ? { executionId } : {}),
        ...(usage ? { usage } : {}),
      }
    : null;
}
