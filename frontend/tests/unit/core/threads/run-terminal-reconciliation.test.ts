import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test, rs } from "@rstest/core";

import type { RunMetadataStorage } from "@/core/private-work/types";
import {
  reconcileTerminalRun,
  retryTerminalReconciliation,
  type RunTerminalCanonicalAuthority,
  type RunTerminalReconciliationAdapters,
} from "@/core/threads/run-terminal-reconciliation";

const TERMINAL_A = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "22222222-2222-4222-8222-222222222222",
  threadId: "33333333-3333-4333-8333-333333333333",
  runId: "44444444-4444-4444-8444-444444444444",
  generation: 7,
} as const satisfies RunTerminalCanonicalAuthority;

const ACTIVE_B = {
  accountId: "55555555-5555-4555-8555-555555555555",
  projectId: "66666666-6666-4666-8666-666666666666",
  threadId: "77777777-7777-4777-8777-777777777777",
  runId: "88888888-8888-4888-8888-888888888888",
  generation: 8,
} as const satisfies RunTerminalCanonicalAuthority;

const RECONNECT_KEY = `lg:stream:${TERMINAL_A.threadId}` as const;

type CanonicalHistory = Readonly<{
  threadId: string;
  terminalRunId: string;
}>;

function memoryStorage(
  initial: Record<string, string>,
  hooks: {
    onGet?: (key: string, readCount: number) => void;
    onRemove?: (key: string) => void;
  } = {},
) {
  const values = new Map(Object.entries(initial));
  let readCount = 0;
  const removeItem = rs.fn((key: string) => {
    values.delete(key);
    hooks.onRemove?.(key);
  });
  const storage: RunMetadataStorage = {
    getItem(key) {
      readCount += 1;
      const value = values.get(key) ?? null;
      hooks.onGet?.(key, readCount);
      return value;
    },
    setItem: (key, value) => values.set(key, value),
    removeItem,
  };
  return { storage, values, removeItem };
}

function harness({
  current = TERMINAL_A as RunTerminalCanonicalAuthority | null,
  reconnectRunId = TERMINAL_A.runId,
}: {
  current?: RunTerminalCanonicalAuthority | null;
  reconnectRunId?: string | null;
} = {}) {
  let selectedCurrent = current;
  const events: string[] = [];
  const history: CanonicalHistory = {
    threadId: TERMINAL_A.threadId,
    terminalRunId: TERMINAL_A.runId,
  };
  const reconnect = memoryStorage(
    reconnectRunId === null ? {} : { [RECONNECT_KEY]: reconnectRunId },
  );
  const adapters: RunTerminalReconciliationAdapters<CanonicalHistory> = {
    readCurrentAuthority: () => selectedCurrent,
    reconnectStorage: reconnect.storage,
    preserveVisibleProjection: () => undefined,
    setControlledThreadId(threadId) {
      events.push(`controlled:${threadId ?? "null"}`);
    },
    switchLocalThreadToNull() {
      events.push("sdk:null");
    },
    async readCanonicalRun(target) {
      events.push(`refetch:${target.threadId}:${target.runId}`);
      return history;
    },
    commitCanonicalRun(target, canonicalHistory) {
      events.push(`merge:${target.threadId}:${canonicalHistory.terminalRunId}`);
    },
  };
  return {
    adapters,
    events,
    history,
    reconnect,
    setCurrent(next: RunTerminalCanonicalAuthority | null) {
      selectedCurrent = next;
    },
  };
}

describe("terminal Run reconciliation", () => {
  test("keeps the visible terminal projection while canonical REST is pending", async () => {
    let resolveHistory: ((history: CanonicalHistory) => void) | undefined;
    const historyPromise = new Promise<CanonicalHistory>((resolve) => {
      resolveHistory = resolve;
    });
    const reconnect = memoryStorage({
      [RECONNECT_KEY]: TERMINAL_A.runId,
    });
    const events: string[] = [];
    let liveMessages = [
      {
        id: "terminal-answer",
        type: "ai",
        content: "Terminal answer",
        run_id: TERMINAL_A.runId,
      } as Message,
    ];
    let preservedMessages: Message[] = [];
    const visibleMessageIds = () =>
      (liveMessages.length > 0 ? liveMessages : preservedMessages).map(
        (message) => message.id,
      );
    const adapters = {
      readCurrentAuthority: () => TERMINAL_A,
      reconnectStorage: reconnect.storage,
      preserveVisibleProjection(target: RunTerminalCanonicalAuthority) {
        events.push("preserve");
        preservedMessages = [...liveMessages];
        expect(target).toEqual(TERMINAL_A);
      },
      setControlledThreadId(threadId: string | null) {
        events.push(`controlled:${threadId ?? "null"}`);
      },
      switchLocalThreadToNull() {
        events.push("sdk:null");
        liveMessages = [];
      },
      readCanonicalRun() {
        events.push("refetch");
        return historyPromise;
      },
      commitCanonicalRun() {
        events.push("merge");
      },
    } satisfies RunTerminalReconciliationAdapters<CanonicalHistory>;

    const reconciliation = reconcileTerminalRun(TERMINAL_A, adapters);
    await Promise.resolve();

    expect(events).toEqual([
      "preserve",
      "controlled:null",
      "sdk:null",
      "refetch",
    ]);
    expect(visibleMessageIds()).toEqual(["terminal-answer"]);

    resolveHistory?.({
      threadId: TERMINAL_A.threadId,
      terminalRunId: TERMINAL_A.runId,
    });
    await expect(reconciliation).resolves.toEqual({ kind: "reconciled" });
  });

  test("retries the exact failed reconciliation after clearing its failure", async () => {
    const events: string[] = [];
    const result = { kind: "reconciled" } as const;

    await expect(
      retryTerminalReconciliation(TERMINAL_A, {
        readCurrentAuthority: () => TERMINAL_A,
        clearFailure(authority) {
          events.push(`clear:${authority.runId}`);
        },
        async reconcile(authority) {
          events.push(`reconcile:${authority.runId}`);
          return result;
        },
        retryCanonicalHistory() {
          events.push("history");
        },
      }),
    ).resolves.toEqual({
      kind: "terminal-reconciliation-retried",
      result,
    });
    expect(events).toEqual([
      `clear:${TERMINAL_A.runId}`,
      `reconcile:${TERMINAL_A.runId}`,
    ]);
  });

  test("does not retry a failed terminal authority after another Run owns the view", async () => {
    const clearFailure = rs.fn();
    const reconcile = rs.fn();
    const retryCanonicalHistory = rs.fn();

    await expect(
      retryTerminalReconciliation(TERMINAL_A, {
        readCurrentAuthority: () => ACTIVE_B,
        clearFailure,
        reconcile,
        retryCanonicalHistory,
      }),
    ).resolves.toEqual({ kind: "canonical-history-retried" });
    expect(clearFailure).not.toHaveBeenCalled();
    expect(reconcile).not.toHaveBeenCalled();
    expect(retryCanonicalHistory).toHaveBeenCalledTimes(1);
  });

  test("detaches locally, awaits exact REST history, merges, then restores in order", async () => {
    const target = harness();

    await expect(
      reconcileTerminalRun(TERMINAL_A, target.adapters),
    ).resolves.toEqual({ kind: "reconciled" });

    expect(target.events).toEqual([
      "controlled:null",
      "sdk:null",
      `refetch:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
      `merge:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
      `controlled:${TERMINAL_A.threadId}`,
    ]);
    expect(target.reconnect.values.has(RECONNECT_KEY)).toBe(false);
    expect(target.reconnect.removeItem).toHaveBeenCalledTimes(1);
  });

  test("keeps the local owner detached and the exact reconnect key cleared when REST fails", async () => {
    const target = harness();
    const failure = new Error("canonical REST unavailable");
    target.adapters.readCanonicalRun = async () => {
      target.events.push("refetch:failed");
      throw failure;
    };

    await expect(
      reconcileTerminalRun(TERMINAL_A, target.adapters),
    ).resolves.toEqual({
      kind: "failed",
      stage: "canonical-history-refetch",
      error: failure,
    });
    expect(target.events).toEqual([
      "controlled:null",
      "sdk:null",
      "refetch:failed",
    ]);
    expect(target.reconnect.values.has(RECONNECT_KEY)).toBe(false);
  });

  test("drops late terminal A when canonical authority is already active B", async () => {
    const target = harness({
      current: ACTIVE_B,
      reconnectRunId: ACTIVE_B.runId,
    });

    await expect(
      reconcileTerminalRun(TERMINAL_A, target.adapters),
    ).resolves.toEqual({ kind: "stale", stage: "initial" });
    expect(target.events).toEqual([]);
    expect(target.reconnect.values.get(RECONNECT_KEY)).toBe(ACTIVE_B.runId);
    expect(target.reconnect.removeItem).not.toHaveBeenCalled();
  });

  test("never deletes a reconnect key that already belongs to Run B", async () => {
    const target = harness({ reconnectRunId: ACTIVE_B.runId });

    await expect(
      reconcileTerminalRun(TERMINAL_A, target.adapters),
    ).resolves.toEqual({ kind: "reconciled" });
    expect(target.reconnect.values.get(RECONNECT_KEY)).toBe(ACTIVE_B.runId);
    expect(target.reconnect.removeItem).not.toHaveBeenCalled();
  });

  test("compares every account/project/thread/run/generation identity field", async () => {
    for (const current of [
      { ...TERMINAL_A, accountId: ACTIVE_B.accountId },
      { ...TERMINAL_A, projectId: ACTIVE_B.projectId },
      { ...TERMINAL_A, threadId: ACTIVE_B.threadId },
      { ...TERMINAL_A, runId: ACTIVE_B.runId },
      { ...TERMINAL_A, generation: ACTIVE_B.generation },
    ]) {
      const target = harness({ current });
      await expect(
        reconcileTerminalRun(TERMINAL_A, target.adapters),
      ).resolves.toEqual({ kind: "stale", stage: "initial" });
      expect(target.events).toEqual([]);
    }
  });

  test("fences a scope switch after every reconciliation callback", async () => {
    const scenarios: ReadonlyArray<{
      name: string;
      install: (
        target: ReturnType<typeof harness>,
      ) => RunTerminalReconciliationAdapters<CanonicalHistory>;
      expectedEvents: readonly string[];
      expectedStage: string;
    }> = [
      {
        name: "reconnect clear",
        install(target) {
          const reconnect = memoryStorage(
            { [RECONNECT_KEY]: TERMINAL_A.runId },
            { onRemove: () => target.setCurrent(ACTIVE_B) },
          );
          return { ...target.adapters, reconnectStorage: reconnect.storage };
        },
        expectedEvents: [],
        expectedStage: "reconnect-cleared",
      },
      {
        name: "visible projection preserve",
        install(target) {
          return {
            ...target.adapters,
            preserveVisibleProjection() {
              target.events.push("preserve");
              target.setCurrent(ACTIVE_B);
            },
          };
        },
        expectedEvents: ["preserve"],
        expectedStage: "visible-projection-preserved",
      },
      {
        name: "controlled detach",
        install(target) {
          return {
            ...target.adapters,
            setControlledThreadId(threadId) {
              target.events.push(`controlled:${threadId ?? "null"}`);
              if (threadId === null) target.setCurrent(ACTIVE_B);
            },
          };
        },
        expectedEvents: ["controlled:null"],
        expectedStage: "controlled-thread-cleared",
      },
      {
        name: "SDK detach",
        install(target) {
          return {
            ...target.adapters,
            switchLocalThreadToNull() {
              target.events.push("sdk:null");
              target.setCurrent(ACTIVE_B);
            },
          };
        },
        expectedEvents: ["controlled:null", "sdk:null"],
        expectedStage: "local-thread-detached",
      },
      {
        name: "REST refetch",
        install(target) {
          return {
            ...target.adapters,
            async readCanonicalRun(authority) {
              target.events.push(
                `refetch:${authority.threadId}:${authority.runId}`,
              );
              target.setCurrent(ACTIVE_B);
              return target.history;
            },
          };
        },
        expectedEvents: [
          "controlled:null",
          "sdk:null",
          `refetch:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
        ],
        expectedStage: "canonical-history-refetched",
      },
      {
        name: "history merge",
        install(target) {
          return {
            ...target.adapters,
            commitCanonicalRun(authority, history) {
              target.events.push(
                `merge:${authority.threadId}:${history.terminalRunId}`,
              );
              target.setCurrent(ACTIVE_B);
            },
          };
        },
        expectedEvents: [
          "controlled:null",
          "sdk:null",
          `refetch:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
          `merge:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
        ],
        expectedStage: "history-merged",
      },
      {
        name: "final reconnect read",
        install(target) {
          let reads = 0;
          const reconnect = memoryStorage(
            {},
            {
              onGet: () => {
                reads += 1;
                if (reads === 2) target.setCurrent(ACTIVE_B);
              },
            },
          );
          return { ...target.adapters, reconnectStorage: reconnect.storage };
        },
        expectedEvents: [
          "controlled:null",
          "sdk:null",
          `refetch:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
          `merge:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
        ],
        expectedStage: "reconnect-confirmed",
      },
      {
        name: "controlled restore",
        install(target) {
          return {
            ...target.adapters,
            setControlledThreadId(threadId) {
              target.events.push(`controlled:${threadId ?? "null"}`);
              if (threadId === TERMINAL_A.threadId) {
                target.setCurrent(ACTIVE_B);
              }
            },
          };
        },
        expectedEvents: [
          "controlled:null",
          "sdk:null",
          `refetch:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
          `merge:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
          `controlled:${TERMINAL_A.threadId}`,
        ],
        expectedStage: "controlled-thread-restored",
      },
    ];

    for (const scenario of scenarios) {
      const target = harness({
        reconnectRunId:
          scenario.name === "final reconnect read" ? null : TERMINAL_A.runId,
      });
      const adapters = scenario.install(target);

      await expect(
        reconcileTerminalRun(TERMINAL_A, adapters),
        scenario.name,
      ).resolves.toEqual({ kind: "stale", stage: scenario.expectedStage });
      expect(target.events, scenario.name).toEqual(scenario.expectedEvents);
    }
  });

  test("does not restore while a late writer reintroduces the terminal reconnect key", async () => {
    const target = harness();
    target.adapters.commitCanonicalRun = () => {
      target.events.push("merge:late-terminal-write");
      target.reconnect.storage.setItem(RECONNECT_KEY, TERMINAL_A.runId);
    };

    await expect(
      reconcileTerminalRun(TERMINAL_A, target.adapters),
    ).resolves.toEqual({
      kind: "blocked",
      reason: "terminal-reconnect-present",
    });
    expect(target.events).toEqual([
      "controlled:null",
      "sdk:null",
      `refetch:${TERMINAL_A.threadId}:${TERMINAL_A.runId}`,
      "merge:late-terminal-write",
    ]);
  });

  test("settles an open local stream without Stop, cancel, join, or a second Run", async () => {
    const target = harness();
    let streamOpen = true;
    const sdk = {
      switchThread: rs.fn((threadId: null) => {
        expect(threadId).toBeNull();
        streamOpen = false;
      }),
      stop: rs.fn(),
      joinStream: rs.fn(),
      createRun: rs.fn(),
    };
    target.adapters.switchLocalThreadToNull = () => sdk.switchThread(null);

    await expect(
      reconcileTerminalRun(TERMINAL_A, target.adapters),
    ).resolves.toEqual({ kind: "reconciled" });

    expect(streamOpen).toBe(false);
    expect(sdk.switchThread).toHaveBeenCalledTimes(1);
    expect(sdk.stop).not.toHaveBeenCalled();
    expect(sdk.joinStream).not.toHaveBeenCalled();
    expect(sdk.createRun).not.toHaveBeenCalled();
    expect(target.reconnect.values.has(RECONNECT_KEY)).toBe(false);
  });
});
