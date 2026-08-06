import type { Message } from "@langchain/langgraph-sdk";
import { useCallback, useEffect, useRef, useState } from "react";

export const STREAM_RENDER_COALESCE_MS = 80;

export type CoalesceDecision =
  | { action: "flush-now" }
  | { action: "schedule"; delayMs: number }
  | { action: "wait" };

export function decideCoalesce(
  nowMs: number,
  lastFlushMs: number,
  intervalMs: number,
  hasPendingTimer: boolean,
): CoalesceDecision {
  if (nowMs - lastFlushMs >= intervalMs) return { action: "flush-now" };
  if (hasPendingTimer) return { action: "wait" };
  return { action: "schedule", delayMs: intervalMs - (nowMs - lastFlushMs) };
}

function sameMessages(left: Message[], right: Message[]) {
  return (
    left === right ||
    (left.length === right.length &&
      left.every((message, index) => message === right[index]))
  );
}

export function useCoalescedStreamMessages(
  messages: Message[],
  isStreaming: boolean,
  intervalMs = STREAM_RENDER_COALESCE_MS,
) {
  const [snapshot, setSnapshot] = useState<Message[] | null>(null);
  const latestRef = useRef(messages);
  const lastFlushRef = useRef(Number.NEGATIVE_INFINITY);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  latestRef.current = messages;

  const publish = useCallback(() => {
    setSnapshot((previous) =>
      previous !== null && sameMessages(previous, latestRef.current)
        ? previous
        : latestRef.current,
    );
  }, []);

  const clearPending = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (!isStreaming) {
      clearPending();
      lastFlushRef.current = Number.NEGATIVE_INFINITY;
      setSnapshot((previous) => (previous === null ? previous : null));
      return;
    }

    const now = performance.now();
    const decision = decideCoalesce(
      now,
      lastFlushRef.current,
      intervalMs,
      timerRef.current !== null,
    );
    if (decision.action === "flush-now") {
      clearPending();
      lastFlushRef.current = now;
      publish();
    } else if (decision.action === "schedule") {
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        lastFlushRef.current = performance.now();
        publish();
      }, decision.delayMs);
    }
  }, [clearPending, intervalMs, isStreaming, messages, publish]);

  useEffect(() => clearPending, [clearPending]);

  return isStreaming ? (snapshot ?? messages) : messages;
}
