import type { Message } from "@langchain/langgraph-sdk";

import type { MessageGroup } from "./utils";

export interface RunDurationDisplay {
  runId: string;
  durationSeconds: number;
}

export interface RunDurationFormatter {
  lessThanSecond: string;
  hours: (value: number) => string;
  minutes: (value: number) => string;
  seconds: (value: number) => string;
  separator: string;
}

function getMessageRunId(message: Message) {
  const direct = Reflect.get(message, "run_id");
  if (typeof direct === "string" && direct.length > 0) return direct;
  const additional = message.additional_kwargs?.run_id;
  return typeof additional === "string" && additional.length > 0
    ? additional
    : undefined;
}

function normalizeDuration(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : undefined;
}

export function getRunDurationDisplaysByGroupIndex(groups: MessageGroup[]) {
  const displays = groups.map(() => [] as RunDurationDisplay[]);
  let turnDuration: RunDurationDisplay | undefined;

  groups.forEach((group, groupIndex) => {
    if (group.type === "human") {
      turnDuration = undefined;
      return;
    }

    for (const message of group.messages) {
      const runId = getMessageRunId(message);
      if (!runId) continue;
      if (message.type !== "ai") continue;
      const duration = normalizeDuration(
        message.additional_kwargs?.turn_duration,
      );
      if (duration !== undefined) {
        turnDuration = { runId, durationSeconds: duration };
      }
    }

    const nextGroup = groups[groupIndex + 1];
    const isTurnEnd = !nextGroup || nextGroup.type === "human";
    if (isTurnEnd) {
      if (turnDuration) displays[groupIndex]?.push(turnDuration);
      turnDuration = undefined;
    }
  });

  return displays;
}

export function formatRunDuration(
  value: number,
  formatter: RunDurationFormatter,
) {
  const duration = normalizeDuration(value);
  if (duration === undefined) return null;
  if (duration === 0) return formatter.lessThanSecond;

  const hours = Math.floor(duration / 3600);
  const minutes = Math.floor((duration % 3600) / 60);
  const seconds = duration % 60;
  const parts: string[] = [];
  if (hours > 0) parts.push(formatter.hours(hours));
  if (minutes > 0) parts.push(formatter.minutes(minutes));
  if (seconds > 0) parts.push(formatter.seconds(seconds));
  return parts.join(formatter.separator);
}
