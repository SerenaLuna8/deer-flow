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

  if (type === "task_started") {
    return modelName ? { id: taskId, modelName } : null;
  }
  if (type !== "task_running") {
    return null;
  }

  const usage = normalizeTokenUsage(Reflect.get(event, "usage"));
  return modelName || usage
    ? {
        id: taskId,
        ...(modelName ? { modelName } : {}),
        ...(usage ? { usage } : {}),
      }
    : null;
}
