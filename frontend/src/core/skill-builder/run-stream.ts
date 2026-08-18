import type {
  SkillBuilderRunStatus,
  SkillBuilderRunStreamProjection,
  SkillBuilderRunToolStepProjection,
} from "./types";

export type SkillBuilderRunStreamFrame = {
  id?: string;
  event: string;
  data: unknown;
};

export type ConsumeSkillBuilderRunStreamOptions = {
  runId: string;
  initialStatus: "pending" | "running";
  signal: AbortSignal;
  open: () => AsyncIterable<SkillBuilderRunStreamFrame>;
  onProjection: (projection: SkillBuilderRunStreamProjection) => void;
  retryDelayMs?: number;
  maxReconnectAttempts?: number;
};

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function safeString(value: unknown, maxLength: number): string | null {
  if (typeof value !== "string") return null;
  const selected = value.trim();
  return selected.length > 0 && selected.length <= maxLength ? selected : null;
}

function messageKind(message: UnknownRecord): "ai" | "tool" | "human" | null {
  const raw = safeString(message.type, 64)?.toLocaleLowerCase();
  if (raw === "ai" || raw === "aimessage" || raw === "aimessagechunk") {
    return "ai";
  }
  if (raw === "tool" || raw === "toolmessage" || raw === "toolmessagechunk") {
    return "tool";
  }
  if (raw === "human" || raw === "humanmessage") return "human";
  return null;
}

function messageRunId(message: UnknownRecord): string | null {
  const direct = safeString(message.run_id, 128);
  if (direct) return direct;
  return safeString(record(message.additional_kwargs)?.run_id, 128);
}

function toolCallName(call: UnknownRecord): string | null {
  return (
    safeString(call.name, 255) ?? safeString(record(call.function)?.name, 255)
  );
}

function toolCallId(call: UnknownRecord): string | null {
  return safeString(call.id, 512);
}

function toolCalls(
  message: UnknownRecord,
  key: "tool_calls" | "tool_call_chunks",
): UnknownRecord[] {
  const value = message[key];
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const selected = record(item);
    return selected ? [selected] : [];
  });
}

function upsertStep(
  steps: SkillBuilderRunToolStepProjection[],
  next: SkillBuilderRunToolStepProjection,
) {
  const index = steps.findIndex((step) => step.id === next.id);
  if (index < 0) {
    steps.push(next);
    return;
  }
  const current = steps[index];
  if (!current) return;
  steps[index] = {
    id: current.id,
    toolName: current.toolName,
    status: next.status,
  };
}

function projectMessage(
  steps: SkillBuilderRunToolStepProjection[],
  message: UnknownRecord,
) {
  const kind = messageKind(message);
  if (kind === "ai") {
    for (const call of toolCalls(message, "tool_call_chunks")) {
      const id = toolCallId(call);
      const toolName = toolCallName(call);
      if (!id || !toolName) continue;
      upsertStep(steps, { id, toolName, status: "pending" });
    }
    for (const call of toolCalls(message, "tool_calls")) {
      const id = toolCallId(call);
      const toolName = toolCallName(call);
      if (!id || !toolName) continue;
      upsertStep(steps, { id, toolName, status: "running" });
    }
    return;
  }
  if (kind !== "tool") return;
  const id = safeString(message.tool_call_id, 512);
  if (!id) return;
  const toolName = safeString(message.name, 255);
  const status =
    message.status === "error" || message.status === "failed"
      ? "failed"
      : "completed";
  const existing = steps.find((step) => step.id === id);
  if (!existing && !toolName) return;
  upsertStep(steps, {
    id,
    toolName: existing?.toolName ?? toolName!,
    status,
  });
}

function currentRunMessages(
  data: unknown,
  runId: string,
): UnknownRecord[] | null {
  const messages = record(data)?.messages;
  if (!Array.isArray(messages)) return null;
  const selected = messages.flatMap((message) => {
    const item = record(message);
    return item ? [item] : [];
  });
  const anchor = selected.findIndex(
    (message) => messageRunId(message) === runId,
  );
  return anchor < 0 ? null : selected.slice(anchor);
}

function nestedUpdateMessages(value: unknown, depth = 0): UnknownRecord[] {
  if (depth > 4) return [];
  if (Array.isArray(value)) {
    return value.flatMap((item) => nestedUpdateMessages(item, depth + 1));
  }
  const selected = record(value);
  if (!selected) return [];
  const messages = selected.messages;
  if (Array.isArray(messages)) {
    return messages.flatMap((message) => {
      const item = record(message);
      return item ? [item] : [];
    });
  }
  return Object.values(selected).flatMap((item) =>
    nestedUpdateMessages(item, depth + 1),
  );
}

function terminalStatus(data: unknown): SkillBuilderRunStatus {
  const status = record(data)?.status;
  if (status === "completed" || status === "success") return "success";
  if (status === "timeout") return "timeout";
  if (status === "interrupted" || status === "cancelled") {
    return "interrupted";
  }
  return "error";
}

function settleToolSteps(
  steps: SkillBuilderRunToolStepProjection[],
  succeeded: boolean,
) {
  return steps.map((step) =>
    step.status === "pending" || step.status === "running"
      ? { ...step, status: succeeded ? "completed" : "failed" }
      : step,
  ) satisfies SkillBuilderRunToolStepProjection[];
}

export function createSkillBuilderRunStreamProjection(
  runId: string,
  status: "pending" | "running",
): SkillBuilderRunStreamProjection {
  return {
    runId,
    status,
    messages: [],
    toolSteps: [],
    clarification: null,
  };
}

/**
 * Reduce an authenticated private-Run frame to the Builder's public UI state.
 *
 * Only stable tool call IDs, bounded tool names, and lifecycle statuses cross
 * this boundary. Tool arguments, tool results, provider errors, message text,
 * and delegated subgraph payloads are deliberately never retained.
 */
export function reduceSkillBuilderRunStreamFrame(
  projection: SkillBuilderRunStreamProjection,
  frame: SkillBuilderRunStreamFrame,
): SkillBuilderRunStreamProjection {
  if (frame.event.includes("|")) return projection;
  if (frame.event === "end" || frame.event === "error") {
    const status =
      frame.event === "error" ? "error" : terminalStatus(frame.data);
    return {
      ...projection,
      status,
      toolSteps: settleToolSteps(projection.toolSteps, status === "success"),
    };
  }

  if (frame.event === "values") {
    const messages = currentRunMessages(frame.data, projection.runId);
    if (!messages) return projection;
    const toolSteps: SkillBuilderRunToolStepProjection[] = [];
    for (const message of messages) projectMessage(toolSteps, message);
    return { ...projection, status: "running", toolSteps };
  }

  let messages: UnknownRecord[] = [];
  if (frame.event === "messages") {
    const message = Array.isArray(frame.data) ? record(frame.data[0]) : null;
    if (message) messages = [message];
  } else if (frame.event === "updates") {
    messages = nestedUpdateMessages(frame.data);
  } else {
    return projection;
  }

  if (messages.length === 0) return projection;
  const toolSteps = projection.toolSteps.map((step) => ({ ...step }));
  for (const message of messages) projectMessage(toolSteps, message);
  return { ...projection, status: "running", toolSteps };
}

function waitForRetry(signal: AbortSignal, delayMs: number): Promise<void> {
  if (signal.aborted || delayMs <= 0) return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = globalThis.setTimeout(finish, delayMs);
    function finish() {
      globalThis.clearTimeout(timeout);
      signal.removeEventListener("abort", finish);
      resolve();
    }
    signal.addEventListener("abort", finish, { once: true });
  });
}

/** Consume one durable Run, reopening from cursor zero after transport loss. */
export async function consumeSkillBuilderRunStream({
  runId,
  initialStatus,
  signal,
  open,
  onProjection,
  retryDelayMs = 500,
  maxReconnectAttempts = 4,
}: ConsumeSkillBuilderRunStreamOptions): Promise<void> {
  const reconnectLimit =
    Number.isInteger(maxReconnectAttempts) && maxReconnectAttempts >= 0
      ? Math.min(maxReconnectAttempts, 10)
      : 4;
  let reconnectAttempts = 0;
  let projection = createSkillBuilderRunStreamProjection(runId, initialStatus);
  onProjection(projection);

  while (!signal.aborted) {
    let receivedFrame = false;
    try {
      for await (const frame of open()) {
        if (signal.aborted) return;
        receivedFrame = true;
        projection = reduceSkillBuilderRunStreamFrame(projection, frame);
        onProjection(projection);
        if (
          projection.status !== "pending" &&
          projection.status !== "running"
        ) {
          return;
        }
      }
      // The shared private client rejects a started non-terminal stream. An
      // empty stream means the Run no longer has replayable work; session
      // polling remains the terminal business-state authority.
      if (!receivedFrame) return;
    } catch {
      if (signal.aborted) return;
    }
    if (reconnectAttempts >= reconnectLimit) return;
    reconnectAttempts += 1;
    await waitForRetry(signal, retryDelayMs);
  }
}
