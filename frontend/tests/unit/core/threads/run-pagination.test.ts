import type { Run } from "@langchain/langgraph-sdk";
import { describe, expect, rs, test } from "@rstest/core";

import {
  fetchAllThreadRuns,
  THREAD_RUNS_MAX_OFFSET,
  THREAD_RUNS_MAX_PAGES,
  THREAD_RUNS_PAGE_SIZE,
} from "@/core/threads/hooks";

type GatewayRun = Run & {
  error: string | null;
  model_name: string | null;
};

function makeRun(runId: string): GatewayRun {
  return {
    run_id: runId,
    thread_id: "thread-1",
    assistant_id: "lead_agent",
    created_at: "2026-07-30T00:00:00Z",
    updated_at: "2026-07-30T00:00:00Z",
    status: "success",
    metadata: {},
    multitask_strategy: "reject",
    error: null,
    model_name: null,
  };
}

describe("fetchAllThreadRuns", () => {
  test("loads every newest-first page and forwards the abort signal", async () => {
    const list = rs
      .fn()
      .mockResolvedValueOnce([makeRun("run-4"), makeRun("run-3")])
      .mockResolvedValueOnce([makeRun("run-2"), makeRun("run-1")])
      .mockResolvedValueOnce([]);
    const controller = new AbortController();

    const runs = await fetchAllThreadRuns(
      { runs: { list } },
      "thread-1",
      2,
      controller.signal,
    );

    expect(runs.map((run) => run.run_id)).toEqual([
      "run-4",
      "run-3",
      "run-2",
      "run-1",
    ]);
    expect(list).toHaveBeenNthCalledWith(1, "thread-1", {
      limit: 2,
      offset: 0,
      signal: controller.signal,
    });
    expect(list).toHaveBeenNthCalledWith(2, "thread-1", {
      limit: 2,
      offset: 2,
      signal: controller.signal,
    });
    expect(list).toHaveBeenNthCalledWith(3, "thread-1", {
      limit: 2,
      offset: 4,
      signal: controller.signal,
    });
  });

  test("stops after a short final page", async () => {
    const list = rs
      .fn()
      .mockResolvedValueOnce([makeRun("run-3"), makeRun("run-2")])
      .mockResolvedValueOnce([makeRun("run-1")]);

    const runs = await fetchAllThreadRuns({ runs: { list } }, "thread-1", 2);

    expect(runs.map((run) => run.run_id)).toEqual(["run-3", "run-2", "run-1"]);
    expect(list).toHaveBeenCalledTimes(2);
  });

  test("deduplicates a run repeated by concurrent offset drift", async () => {
    const list = rs
      .fn()
      .mockResolvedValueOnce([makeRun("run-3"), makeRun("run-2")])
      .mockResolvedValueOnce([makeRun("run-2"), makeRun("run-1")])
      .mockResolvedValueOnce([]);

    const runs = await fetchAllThreadRuns({ runs: { list } }, "thread-1", 2);

    expect(runs.map((run) => run.run_id)).toEqual(["run-3", "run-2", "run-1"]);
  });

  test("fails fast when a full page makes no pagination progress", async () => {
    const repeatedPage = [makeRun("run-2"), makeRun("run-1")];
    const list = rs
      .fn()
      .mockResolvedValueOnce(repeatedPage)
      .mockResolvedValueOnce(repeatedPage);

    await expect(
      fetchAllThreadRuns({ runs: { list } }, "thread-1", 2),
    ).rejects.toThrow("non-advancing");
    expect(list).toHaveBeenCalledTimes(2);
  });

  test("strictly rejects unknown Run fields and private authority metadata", async () => {
    const unknownFieldList = rs.fn().mockResolvedValueOnce([
      {
        ...makeRun("run-1"),
        origin_trace_id: "019c0f4e1c1f70cc83b6de2c296fc1df",
      },
    ]);
    const privateMetadataList = rs.fn().mockResolvedValueOnce([
      {
        ...makeRun("run-1"),
        metadata: {
          nested: {
            owner_user_id: "must-not-pass",
          },
        },
      },
    ]);

    await expect(
      fetchAllThreadRuns(
        { runs: { list: unknownFieldList } },
        "thread-1",
        2,
      ),
    ).rejects.toThrow();
    await expect(
      fetchAllThreadRuns(
        { runs: { list: privateMetadataList } },
        "thread-1",
        2,
      ),
    ).rejects.toThrow("private authority");
  });

  test("rejects malformed Run catalog page responses", async () => {
    const objectPageList = rs
      .fn()
      .mockResolvedValueOnce({ data: [makeRun("run-1")] });
    const malformedRunList = rs.fn().mockResolvedValueOnce([
      {
        ...makeRun("run-1"),
        status: "queued",
      },
    ]);

    await expect(
      fetchAllThreadRuns(
        { runs: { list: objectPageList } },
        "thread-1",
        2,
      ),
    ).rejects.toThrow();
    await expect(
      fetchAllThreadRuns(
        { runs: { list: malformedRunList } },
        "thread-1",
        2,
      ),
    ).rejects.toThrow();
  });

  test("fails closed when every full page adds a new Run forever", async () => {
    const list = rs
      .fn()
      .mockImplementation(
        (_threadId: string, options?: { offset?: number }) => [
          makeRun(`run-${options?.offset ?? 0}`),
        ],
      );

    await expect(
      fetchAllThreadRuns({ runs: { list } }, "thread-1", 1),
    ).rejects.toThrow("page safety limit");
    expect(list).toHaveBeenCalledTimes(THREAD_RUNS_MAX_PAGES);
  });

  test("fails closed before requesting a Run page beyond the offset limit", async () => {
    const firstPage = Array.from({ length: THREAD_RUNS_PAGE_SIZE }, (_, index) =>
      makeRun(`initial-${index}`),
    );
    const list = rs
      .fn()
      .mockImplementation(
        (_threadId: string, options?: { offset?: number; limit?: number }) => {
          const offset = options?.offset ?? 0;
          if (offset === 0) {
            return firstPage;
          }
          return [
            makeRun(`new-${offset}`),
            ...firstPage.slice(1, options?.limit),
          ];
        },
      );

    await expect(
      fetchAllThreadRuns(
        { runs: { list } },
        "thread-1",
        THREAD_RUNS_PAGE_SIZE,
      ),
    ).rejects.toThrow("offset safety limit");
    expect(
      list.mock.calls.at(-1)?.[1]?.offset,
    ).toBeLessThanOrEqual(THREAD_RUNS_MAX_OFFSET);
  });

  test("rejects page sizes outside the Gateway contract", async () => {
    const list = rs.fn();

    await expect(
      fetchAllThreadRuns({ runs: { list } }, "thread-1", 0),
    ).rejects.toThrow("between 1 and 1000");
    await expect(
      fetchAllThreadRuns({ runs: { list } }, "thread-1", 1001),
    ).rejects.toThrow("between 1 and 1000");
    expect(list).not.toHaveBeenCalled();
  });

  test("uses a backend-safe default page size", () => {
    expect(THREAD_RUNS_PAGE_SIZE).toBeGreaterThan(10);
    expect(THREAD_RUNS_PAGE_SIZE).toBeLessThanOrEqual(1000);
    expect(THREAD_RUNS_MAX_PAGES).toBeGreaterThan(1);
    expect(THREAD_RUNS_MAX_OFFSET).toBeGreaterThan(THREAD_RUNS_PAGE_SIZE);
  });
});
