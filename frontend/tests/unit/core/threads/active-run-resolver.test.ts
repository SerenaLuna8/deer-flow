import { describe, expect, test } from "@rstest/core";

import type { RunMetadataStorage } from "@/core/private-work/types";
import { createActiveRunResolver } from "@/core/threads/active-run-resolver";

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  threadId: "22222222-2222-4222-8222-222222222222",
} as const;

const RECONNECT_KEY = `lg:stream:${SCOPE.threadId}` as const;

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  const storage: RunMetadataStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  return { storage, values };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

describe("active Run resolver", () => {
  test("keeps reconnect closed until a forced catalog read confirms the exact hint", async () => {
    const runId = "33333333-3333-4333-8333-333333333333";
    const catalog =
      deferred<readonly [{ run_id: string; status: "pending" }]>();
    const { storage } = memoryStorage({ [RECONNECT_KEY]: runId });
    const owner = createActiveRunResolver();
    const generation = owner.begin({
      scope: SCOPE,
      reconnectStorage: storage,
      readServerCatalog: () => catalog.promise,
    });

    const resolving = generation.resolveFromServerCatalog();

    expect(generation.reconnectStorage.getItem(RECONNECT_KEY)).toBeNull();
    catalog.resolve([{ run_id: runId, status: "pending" }]);
    await expect(resolving).resolves.toEqual({
      kind: "resolved",
      runId,
      generation: 1,
      resumeFromHint: true,
      source: "catalog",
    });
    expect(generation.reconnectStorage.getItem(RECONNECT_KEY)).toBe(runId);
  });

  test("clears a mismatched hint by value and accepts only the catalog Run", async () => {
    const staleRunId = "33333333-3333-4333-8333-333333333333";
    const canonicalRunId = "44444444-4444-4444-8444-444444444444";
    const { storage, values } = memoryStorage({
      [RECONNECT_KEY]: staleRunId,
    });
    const generation = createActiveRunResolver().begin({
      scope: SCOPE,
      reconnectStorage: storage,
      readServerCatalog: async () => [
        { run_id: canonicalRunId, status: "running" },
      ],
    });

    await expect(generation.resolveFromServerCatalog()).resolves.toEqual({
      kind: "resolved",
      runId: canonicalRunId,
      generation: 1,
      resumeFromHint: false,
      source: "catalog",
    });
    expect(values.get(RECONNECT_KEY)).toBeUndefined();
    expect(generation.reconnectStorage.getItem(RECONNECT_KEY)).toBeNull();

    generation.reconnectStorage.setItem(RECONNECT_KEY, staleRunId);
    expect(values.get(RECONNECT_KEY)).toBeUndefined();
    generation.reconnectStorage.setItem(RECONNECT_KEY, canonicalRunId);
    expect(generation.reconnectStorage.getItem(RECONNECT_KEY)).toBe(
      canonicalRunId,
    );
  });

  test("returns none and clears the exact stale hint when the catalog has no active Run", async () => {
    const staleRunId = "33333333-3333-4333-8333-333333333333";
    const { storage, values } = memoryStorage({
      [RECONNECT_KEY]: staleRunId,
    });
    const generation = createActiveRunResolver().begin({
      scope: SCOPE,
      reconnectStorage: storage,
      readServerCatalog: async () => [
        { run_id: staleRunId, status: "success" },
      ],
    });

    await expect(generation.resolveFromServerCatalog()).resolves.toEqual({
      kind: "none",
      generation: 1,
    });
    expect(values.get(RECONNECT_KEY)).toBeUndefined();
    expect(generation.reconnectStorage.getItem(RECONNECT_KEY)).toBeNull();
  });

  test("makes Admission onCreated canonical and drops an older catalog response", async () => {
    const staleRunId = "33333333-3333-4333-8333-333333333333";
    const admittedRunId = "44444444-4444-4444-8444-444444444444";
    const catalog =
      deferred<readonly [{ run_id: string; status: "pending" }]>();
    const { storage, values } = memoryStorage({
      [RECONNECT_KEY]: staleRunId,
    });
    const generation = createActiveRunResolver().begin({
      scope: SCOPE,
      reconnectStorage: storage,
      readServerCatalog: () => catalog.promise,
    });
    const resolving = generation.resolveFromServerCatalog();

    expect(generation.onCreated(admittedRunId)).toEqual({
      kind: "resolved",
      runId: admittedRunId,
      generation: 1,
      resumeFromHint: false,
      source: "admission",
    });
    expect(values.get(RECONNECT_KEY)).toBeUndefined();
    generation.reconnectStorage.setItem(RECONNECT_KEY, admittedRunId);
    expect(generation.reconnectStorage.getItem(RECONNECT_KEY)).toBe(
      admittedRunId,
    );

    catalog.resolve([{ run_id: staleRunId, status: "pending" }]);
    await expect(resolving).resolves.toBeNull();
    expect(generation.reconnectStorage.getItem(RECONNECT_KEY)).toBe(
      admittedRunId,
    );
  });

  test("returns conflict or unavailable without exposing or deleting the hint", async () => {
    const hintedRunId = "33333333-3333-4333-8333-333333333333";
    const otherRunId = "44444444-4444-4444-8444-444444444444";

    for (const scenario of [
      {
        expected: { kind: "conflict", generation: 1 } as const,
        readServerCatalog: async () => [
          { run_id: hintedRunId, status: "pending" },
          { run_id: otherRunId, status: "running" },
        ],
      },
      {
        expected: { kind: "unavailable", generation: 1 } as const,
        readServerCatalog: async () => {
          throw new Error("catalog unavailable");
        },
      },
    ]) {
      const { storage, values } = memoryStorage({
        [RECONNECT_KEY]: hintedRunId,
      });
      const generation = createActiveRunResolver().begin({
        scope: SCOPE,
        reconnectStorage: storage,
        readServerCatalog: scenario.readServerCatalog,
      });

      await expect(generation.resolveFromServerCatalog()).resolves.toEqual(
        scenario.expected,
      );
      expect(values.get(RECONNECT_KEY)).toBe(hintedRunId);
      expect(generation.reconnectStorage.getItem(RECONNECT_KEY)).toBeNull();
    }
  });

  test("fails closed when a raw catalog entry is malformed or unknown", async () => {
    const hintedRunId = "33333333-3333-4333-8333-333333333333";
    const malformedCatalogs: readonly unknown[] = [
      [{ run_id: "not-a-run-uuid", status: "pending" }],
      [{ run_id: hintedRunId, status: "future_status" }],
      [{ run_id: hintedRunId, status: "pending", worker_id: "private" }],
      { data: [] },
    ];

    for (const rawCatalog of malformedCatalogs) {
      const { storage, values } = memoryStorage({
        [RECONNECT_KEY]: hintedRunId,
      });
      const generation = createActiveRunResolver().begin({
        scope: SCOPE,
        reconnectStorage: storage,
        readServerCatalog: async () => rawCatalog as never,
      });

      await expect(generation.resolveFromServerCatalog()).resolves.toEqual({
        kind: "unavailable",
        generation: 1,
      });
      expect(values.get(RECONNECT_KEY)).toBe(hintedRunId);
      expect(generation.reconnectStorage.getItem(RECONNECT_KEY)).toBeNull();
    }
  });

  test("drops late catalog responses and adapter writes across account, project, or Thread generations", async () => {
    const staleRunId = "33333333-3333-4333-8333-333333333333";
    const canonicalRunId = "44444444-4444-4444-8444-444444444444";
    const nextScopes = [
      {
        ...SCOPE,
        accountId: "55555555-5555-4555-8555-555555555555",
      },
      {
        ...SCOPE,
        projectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      },
      {
        ...SCOPE,
        threadId: "66666666-6666-4666-8666-666666666666",
      },
    ] as const;

    for (const nextScope of nextScopes) {
      const oldCatalog =
        deferred<readonly [{ run_id: string; status: "pending" }]>();
      const oldStorage = memoryStorage();
      const nextStorage = memoryStorage();
      const owner = createActiveRunResolver();
      const oldGeneration = owner.begin({
        scope: SCOPE,
        reconnectStorage: oldStorage.storage,
        readServerCatalog: () => oldCatalog.promise,
      });
      const staleResolution = oldGeneration.resolveFromServerCatalog();
      const nextGeneration = owner.begin({
        scope: nextScope,
        reconnectStorage: nextStorage.storage,
        readServerCatalog: async () => [
          { run_id: canonicalRunId, status: "pending" },
        ],
      });

      await expect(nextGeneration.resolveFromServerCatalog()).resolves.toEqual({
        kind: "resolved",
        runId: canonicalRunId,
        generation: 2,
        resumeFromHint: false,
        source: "catalog",
      });
      oldGeneration.reconnectStorage.setItem(RECONNECT_KEY, staleRunId);
      expect(oldStorage.values.size).toBe(0);

      oldCatalog.resolve([{ run_id: staleRunId, status: "pending" }]);
      await expect(staleResolution).resolves.toBeNull();
      expect(nextGeneration.generation).toBe(2);
    }
  });
});
