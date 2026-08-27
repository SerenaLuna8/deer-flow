import type { Message } from "@langchain/langgraph-sdk";
import { describe, expect, test, rs } from "@rstest/core";

import type { RunMetadataStorage } from "@/core/private-work/types";
import {
  createActiveRunReconnectStorageProxy,
  readActiveRunCatalog,
  resolveThreadHistoryId,
  selectExactActiveRunOwner,
  selectExactTerminalDisplayMessages,
  selectExactTerminalReconciliationError,
  terminalReconciliationResultError,
  type ActiveRunOwnerProjection,
} from "@/core/threads/use-thread-stream";

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "22222222-2222-4222-8222-222222222222",
} as const;
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const RUN_ID = "44444444-4444-4444-8444-444444444444";
const RECONNECT_KEY = `lg:stream:${THREAD_ID}` as const;

function run(index: number, status: "pending" | "running" | "success") {
  const runId = `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
  return {
    run_id: runId,
    thread_id: THREAD_ID,
    assistant_id: null,
    created_at: "2026-08-24T00:00:00Z",
    updated_at: "2026-08-24T00:00:01Z",
    status,
    metadata: {},
    multitask_strategy: "reject",
    error: null,
    model_name: null,
    execution_profile: null,
  };
}

function memoryStorage() {
  const values = new Map<string, string>();
  const storage: RunMetadataStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  return { storage, values };
}

describe("useThreadStream active Run ownership", () => {
  test("surfaces a blocked terminal reconnect as a retryable reconciliation error", () => {
    expect(
      terminalReconciliationResultError({
        kind: "blocked",
        reason: "terminal-reconnect-present",
      })?.message,
    ).toMatch(/reconnect state changed/i);
    expect(
      terminalReconciliationResultError({ kind: "reconciled" }),
    ).toBeNull();
  });

  test("shows a terminal display latch only to its exact Run generation", () => {
    const messages = [
      { id: "terminal-answer", type: "ai", content: "done" } as Message,
    ];
    const authority: ActiveRunOwnerProjection & { runId: string } = {
      ...SCOPE,
      threadId: THREAD_ID,
      runId: RUN_ID,
      generation: 9,
    };
    const latch = { authority, messages };

    expect(
      selectExactTerminalDisplayMessages(latch, authority, SCOPE, THREAD_ID),
    ).toBe(messages);
    expect(
      selectExactTerminalDisplayMessages(
        latch,
        { ...authority, generation: 10 },
        SCOPE,
        THREAD_ID,
      ),
    ).toBeNull();
    expect(
      selectExactTerminalDisplayMessages(
        latch,
        authority,
        { ...SCOPE, projectId: "55555555-5555-4555-8555-555555555555" },
        THREAD_ID,
      ),
    ).toBeNull();
    expect(
      selectExactTerminalDisplayMessages(
        latch,
        authority,
        SCOPE,
        "66666666-6666-4666-8666-666666666666",
      ),
    ).toBeNull();
  });

  test("keeps history bound to the viewed Thread while the SDK stream detaches", () => {
    expect(resolveThreadHistoryId(THREAD_ID, null)).toBe(THREAD_ID);
    expect(resolveThreadHistoryId(null, THREAD_ID)).toBe(THREAD_ID);
    expect(resolveThreadHistoryId(null, null)).toBeNull();
  });

  test("forces the complete scoped Run catalog and returns only strict resolver entries", async () => {
    const firstPage = Array.from({ length: 1000 }, (_, index) =>
      run(index + 1, index === 999 ? "pending" : "success"),
    );
    const finalRun = run(1001, "running");
    const list = rs.fn(
      async (
        _threadId: string,
        options?: { offset?: number; signal?: AbortSignal },
      ) => (options?.offset === 1000 ? [finalRun] : firstPage),
    );
    const signal = new AbortController().signal;

    await expect(
      readActiveRunCatalog(
        { runs: { list } },
        { ...SCOPE, threadId: THREAD_ID },
        signal,
      ),
    ).resolves.toEqual([
      ...firstPage.map(({ run_id, status }) => ({ run_id, status })),
      { run_id: finalRun.run_id, status: finalRun.status },
    ]);
    expect(list).toHaveBeenNthCalledWith(1, THREAD_ID, {
      limit: 1000,
      offset: 0,
      signal,
    });
    expect(list).toHaveBeenNthCalledWith(2, THREAD_ID, {
      limit: 1000,
      offset: 1000,
      signal,
    });
  });

  test("keeps one SDK storage proxy closed until the current resolver generation opens it", () => {
    const first = memoryStorage();
    const second = memoryStorage();
    let selected: RunMetadataStorage | null = null;
    const proxy = createActiveRunReconnectStorageProxy(() => selected);

    proxy.setItem(RECONNECT_KEY, RUN_ID);
    expect(proxy.getItem(RECONNECT_KEY)).toBeNull();

    selected = first.storage;
    proxy.setItem(RECONNECT_KEY, RUN_ID);
    expect(proxy.getItem(RECONNECT_KEY)).toBe(RUN_ID);

    selected = second.storage;
    expect(proxy.getItem(RECONNECT_KEY)).toBeNull();
    proxy.removeItem(RECONNECT_KEY);
    expect(first.values.get(RECONNECT_KEY)).toBe(RUN_ID);
  });

  test("exposes an owner projection only for the exact current scope and Thread", () => {
    const projection: ActiveRunOwnerProjection = {
      ...SCOPE,
      threadId: THREAD_ID,
      runId: RUN_ID,
      generation: 9,
    };

    expect(selectExactActiveRunOwner(projection, SCOPE, THREAD_ID)).toEqual({
      activeRunId: RUN_ID,
      resolverGeneration: 9,
    });
    expect(
      selectExactActiveRunOwner(
        { ...projection, runId: null },
        SCOPE,
        THREAD_ID,
      ),
    ).toEqual({ activeRunId: null, resolverGeneration: 9 });
    for (const [scope, threadId] of [
      [{ ...SCOPE, accountId: "default" }, THREAD_ID],
      [
        {
          ...SCOPE,
          projectId: "55555555-5555-4555-8555-555555555555",
        },
        THREAD_ID,
      ],
      [SCOPE, "66666666-6666-4666-8666-666666666666"],
    ] as const) {
      expect(selectExactActiveRunOwner(projection, scope, threadId)).toEqual({
        activeRunId: null,
        resolverGeneration: null,
      });
    }
  });

  test("shows a reconciliation failure only while the same Run generation owns the view", () => {
    const error = new Error("canonical history unavailable");
    const projection: ActiveRunOwnerProjection = {
      ...SCOPE,
      threadId: THREAD_ID,
      runId: RUN_ID,
      generation: 9,
    };
    const failure = { authority: projection, error };

    expect(selectExactTerminalReconciliationError(failure, projection)).toBe(
      error,
    );
    expect(
      selectExactTerminalReconciliationError(failure, {
        ...projection,
        runId: "55555555-5555-4555-8555-555555555555",
        generation: 10,
      }),
    ).toBeNull();
  });
});
