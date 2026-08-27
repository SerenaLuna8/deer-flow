import { describe, expect, test } from "@rstest/core";

import {
  advanceWriteArtifactAutoOpenState,
  createWriteArtifactAutoOpenState,
  resolveRequestedArtifactViewMode,
  type WriteArtifactSelection,
} from "@/core/artifacts/preview";

const oldSelection: WriteArtifactSelection = {
  key: "ai-old/call-old",
  url: "write-file:///outputs/old.md",
  preferredViewMode: "preview",
};
const newSelection: WriteArtifactSelection = {
  key: "ai-new/call-new",
  url: "write-file:///outputs/new.md",
  preferredViewMode: "preview",
};

describe("write artifact auto-open ownership", () => {
  test("absorbs rehydrated writes while loading and opens only a new write from the active run", () => {
    let state = createWriteArtifactAutoOpenState("thread-1");

    let result = advanceWriteArtifactAutoOpenState({
      state,
      threadId: "thread-1",
      selections: [],
      historyIsLoading: true,
      runIsLoading: true,
    });
    state = result.state;
    expect(result.selection).toBeUndefined();

    result = advanceWriteArtifactAutoOpenState({
      state,
      threadId: "thread-1",
      selections: [oldSelection],
      historyIsLoading: true,
      runIsLoading: true,
    });
    state = result.state;
    expect(result.selection).toBeUndefined();

    result = advanceWriteArtifactAutoOpenState({
      state,
      threadId: "thread-1",
      selections: [oldSelection],
      historyIsLoading: false,
      runIsLoading: true,
    });
    state = result.state;
    expect(result.selection).toBeUndefined();

    result = advanceWriteArtifactAutoOpenState({
      state,
      threadId: "thread-1",
      selections: [oldSelection, newSelection],
      historyIsLoading: false,
      runIsLoading: true,
    });
    state = result.state;
    expect(result.selection).toEqual(newSelection);

    result = advanceWriteArtifactAutoOpenState({
      state,
      threadId: "thread-1",
      selections: [oldSelection, newSelection],
      historyIsLoading: false,
      runIsLoading: true,
    });
    expect(result.selection).toBeUndefined();
  });

  test("builds a fresh baseline instead of opening files when the thread changes", () => {
    const activeState = advanceWriteArtifactAutoOpenState({
      state: createWriteArtifactAutoOpenState("thread-1"),
      threadId: "thread-1",
      selections: [],
      historyIsLoading: false,
      runIsLoading: true,
    }).state;

    const result = advanceWriteArtifactAutoOpenState({
      state: activeState,
      threadId: "thread-2",
      selections: [oldSelection],
      historyIsLoading: false,
      runIsLoading: true,
    });

    expect(result.selection).toBeUndefined();
    expect(result.state.threadId).toBe("thread-2");
    expect(result.state.initialized).toBe(true);
    expect(result.state.seenKeys).toEqual(new Set([oldSelection.key]));
  });

  test("requests preview only for the matching execution-time artifact", () => {
    const request = {
      id: 1,
      artifact: newSelection.url,
      mode: newSelection.preferredViewMode,
    };

    expect(
      resolveRequestedArtifactViewMode({
        request,
        filepath: newSelection.url,
        canPreview: true,
      }),
    ).toBe("preview");
    expect(
      resolveRequestedArtifactViewMode({
        request,
        filepath: newSelection.url,
        canPreview: false,
      }),
    ).toBe("code");
    expect(
      resolveRequestedArtifactViewMode({
        request,
        filepath: oldSelection.url,
        canPreview: true,
      }),
    ).toBeUndefined();
  });
});
