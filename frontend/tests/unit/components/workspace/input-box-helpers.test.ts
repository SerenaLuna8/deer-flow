import { expect, test } from "@rstest/core";

import {
  completeLatestCheckpointContinuation,
  createLatestCheckpointContinuationState,
  markLatestCheckpointContinuation,
  resetLatestCheckpointContinuation,
  shouldContinueFromLatestCheckpoint,
} from "@/components/workspace/input-box-helpers";

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
