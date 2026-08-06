import { expect, test } from "@rstest/core";

import {
  canPolishInput,
  completeLatestCheckpointContinuation,
  createLatestCheckpointContinuationState,
  getInputSubmitAction,
  markLatestCheckpointContinuation,
  resetLatestCheckpointContinuation,
  shouldContinueFromLatestCheckpoint,
} from "@/components/workspace/input-box-helpers";

test("routes Dream strictly as a builtin command", () => {
  expect(
    getInputSubmitAction({ text: "/Dream", fileCount: 0, status: "ready" }),
  ).toEqual({ kind: "dream" });
  expect(
    getInputSubmitAction({
      text: "/dream now",
      fileCount: 0,
      status: "ready",
    }),
  ).toEqual({
    kind: "dream-invalid",
    command: "dream",
    reason: "arguments",
  });
  expect(
    getInputSubmitAction({ text: "/dream", fileCount: 1, status: "ready" }),
  ).toEqual({
    kind: "dream-invalid",
    command: "dream",
    reason: "attachments",
  });
  expect(canPolishInput("/dream now")).toBe(false);
});

test("routes Dream history and restore commands without sending chat text", () => {
  expect(
    getInputSubmitAction({
      text: "/dream-log",
      fileCount: 0,
      status: "ready",
    }),
  ).toEqual({ kind: "dream-log", version: null });
  expect(
    getInputSubmitAction({
      text: "/dream-log 12",
      fileCount: 0,
      status: "ready",
    }),
  ).toEqual({ kind: "dream-log", version: 12 });
  expect(
    getInputSubmitAction({
      text: "/dream-restore 12",
      fileCount: 0,
      status: "ready",
    }),
  ).toEqual({ kind: "dream-restore", version: 12 });
  expect(
    getInputSubmitAction({
      text: "/dream-restore",
      fileCount: 0,
      status: "ready",
    }),
  ).toEqual({
    kind: "dream-invalid",
    command: "dream-restore",
    reason: "arguments",
  });
  expect(
    getInputSubmitAction({
      text: "/dream-log 0",
      fileCount: 0,
      status: "ready",
    }),
  ).toEqual({
    kind: "dream-invalid",
    command: "dream-log",
    reason: "arguments",
  });
  expect(canPolishInput("/dream-log 3")).toBe(false);
  expect(canPolishInput("/dream-restore 3")).toBe(false);
});

test("keeps latest-checkpoint continuation until one send succeeds", () => {
  const state = createLatestCheckpointContinuationState();

  markLatestCheckpointContinuation(state, "thread-a");
  expect(shouldContinueFromLatestCheckpoint(state, "thread-a")).toBe(true);

  // A failed send does not complete the one-shot state.
  expect(shouldContinueFromLatestCheckpoint(state, "thread-a")).toBe(true);

  completeLatestCheckpointContinuation(state, "thread-a");
  expect(shouldContinueFromLatestCheckpoint(state, "thread-a")).toBe(false);
});

test("does not carry latest-checkpoint continuation across threads", () => {
  const state = createLatestCheckpointContinuationState();
  markLatestCheckpointContinuation(state, "thread-a");

  expect(shouldContinueFromLatestCheckpoint(state, "thread-b")).toBe(false);
  resetLatestCheckpointContinuation(state);
  expect(shouldContinueFromLatestCheckpoint(state, "thread-a")).toBe(false);
});
