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
  ).toEqual({ kind: "dream-invalid", reason: "arguments" });
  expect(
    getInputSubmitAction({ text: "/dream", fileCount: 1, status: "ready" }),
  ).toEqual({ kind: "dream-invalid", reason: "attachments" });
  expect(canPolishInput("/dream now")).toBe(false);
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
