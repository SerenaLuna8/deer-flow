import type { Run } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import {
  getInitialHistoryRunIds,
  INITIAL_HISTORY_RUN_WINDOW_SIZE,
  isInitialHistoryWindowLoaded,
} from "@/core/threads/run-history";

function runs(count: number): Run[] {
  return Array.from({ length: count }, (_, index) => ({
    run_id: `run-${index + 1}`,
  })) as Run[];
}

describe("initial thread history window", () => {
  test("stages only a bounded window of the newest Runs", () => {
    const threadRuns = runs(INITIAL_HISTORY_RUN_WINDOW_SIZE + 2);

    expect(getInitialHistoryRunIds(threadRuns)).toEqual(
      threadRuns
        .slice(0, INITIAL_HISTORY_RUN_WINDOW_SIZE)
        .map((run) => run.run_id),
    );
  });

  test("publishes only after every Run in the initial window is loaded", () => {
    const threadRuns = runs(INITIAL_HISTORY_RUN_WINDOW_SIZE + 1);
    const initialRunIds = getInitialHistoryRunIds(threadRuns);
    const partiallyLoaded = new Set(initialRunIds.slice(0, -1));

    expect(isInitialHistoryWindowLoaded(threadRuns, partiallyLoaded)).toBe(
      false,
    );
    expect(
      isInitialHistoryWindowLoaded(threadRuns, new Set(initialRunIds)),
    ).toBe(true);
  });

  test("does not wait for older Runs outside the initial window", () => {
    const threadRuns = runs(INITIAL_HISTORY_RUN_WINDOW_SIZE + 1);
    const initialRunIds = getInitialHistoryRunIds(threadRuns);

    expect(
      isInitialHistoryWindowLoaded(threadRuns, new Set(initialRunIds)),
    ).toBe(true);
    expect(initialRunIds).not.toContain(
      threadRuns[INITIAL_HISTORY_RUN_WINDOW_SIZE]?.run_id,
    );
  });
});
