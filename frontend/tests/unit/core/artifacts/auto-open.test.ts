import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

import {
  advanceWriteArtifactAutoOpenState,
  createWriteArtifactAutoOpenState,
  type WriteArtifactSelection,
} from "@/core/artifacts/preview";

const oldSelection: WriteArtifactSelection = {
  key: "ai-old/call-old",
  url: "write-file:///outputs/old.md",
};
const newSelection: WriteArtifactSelection = {
  key: "ai-new/call-new",
  url: "write-file:///outputs/new.md",
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

  test("keeps automatic opening centralized in MessageList", () => {
    const messageGroupSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/messages/message-group.tsx",
      ),
      "utf8",
    );
    const messageListSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/workspace/messages/message-list.tsx",
      ),
      "utf8",
    );

    expect(messageGroupSource).not.toContain("autoOpenArtifactUrl");
    expect(messageGroupSource).not.toContain("autoSelect");
    expect(messageListSource).toContain("advanceWriteArtifactAutoOpenState");
  });
});
