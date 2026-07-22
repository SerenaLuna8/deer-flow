import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  createProjectThread,
  disposeProjectAPIClient,
  getProjectAPIClient,
} from "@/core/private-work/api-client";

const scope = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const threadId = "33333333-3333-4333-8333-333333333333";
const agentAssetId = "44444444-4444-4444-8444-444444444444";
const createdAt = "2026-07-21T06:00:00Z";
const updatedAt = "2026-07-21T06:01:00Z";

function privateThread(version: number, displayName = "Runnable Thread") {
  return {
    thread_id: threadId,
    agent_asset_id: agentAssetId,
    agent_scope: "project",
    display_name: displayName,
    status: "idle",
    metadata: { topic: "private" },
    version,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function jsonRequestBody(init?: RequestInit): unknown {
  if (typeof init?.body !== "string") {
    throw new TypeError("Expected a JSON string request body");
  }
  return JSON.parse(init.body);
}

afterEach(() => {
  disposeProjectAPIClient(scope);
  rs.unstubAllGlobals();
});

describe("project thread adapter", () => {
  test("maps strict search requests and wrapped responses to SDK threads", async () => {
    const fetcher = rs.fn(async (_input: string, _init?: RequestInit) =>
      jsonResponse({ items: [privateThread(3)] }),
    );
    rs.stubGlobal("fetch", fetcher);
    const client = getProjectAPIClient(scope);

    const threads = await client.threads.search({
      limit: 20,
      offset: 5,
      sortBy: "updated_at",
      sortOrder: "desc",
      select: ["thread_id", "values", "metadata"],
    });

    expect(threads).toHaveLength(1);
    expect(threads[0]).toMatchObject({
      thread_id: threadId,
      status: "idle",
      values: { title: "Runnable Thread", messages: [] },
      metadata: {
        agent_asset_id: agentAssetId,
        agent_scope: "project",
        private_work_version: 3,
      },
      created_at: createdAt,
      updated_at: updatedAt,
      state_updated_at: updatedAt,
    });
    const [, init] = fetcher.mock.calls[0]!;
    expect(jsonRequestBody(init)).toEqual({ limit: 20, offset: 5 });
  });

  test("preserves project thread HTTP status for missing and retryable metadata failures", async () => {
    const fetcher = rs
      .fn(async (_input: string, _init?: RequestInit) =>
        jsonResponse({ detail: "temporarily unavailable" }, 503),
      )
      .mockResolvedValueOnce(jsonResponse({ detail: "not found" }, 404));
    rs.stubGlobal("fetch", fetcher);
    const client = getProjectAPIClient(scope);

    await expect(client.threads.get(threadId)).rejects.toMatchObject({
      message: "not found",
      status: 404,
    });
    await expect(client.threads.get(threadId)).rejects.toMatchObject({
      message: "temporarily unavailable",
      status: 503,
    });
  });

  test("creates only through the explicit agent-bound helper", async () => {
    const fetcher = rs.fn(async (_input: string, _init?: RequestInit) =>
      jsonResponse(privateThread(1), 201),
    );
    rs.stubGlobal("fetch", fetcher);

    const created = await createProjectThread(scope, {
      threadId,
      agentAssetId,
      agentScope: "project",
      displayName: "Runnable Thread",
      metadata: { topic: "private" },
    });

    expect(created.thread_id).toBe(threadId);
    const [, init] = fetcher.mock.calls[0]!;
    expect(jsonRequestBody(init)).toEqual({
      thread_id: threadId,
      agent_asset_id: agentAssetId,
      agent_scope: "project",
      display_name: "Runnable Thread",
      metadata: { topic: "private" },
    });
    await expect(getProjectAPIClient(scope).threads.create()).rejects.toThrow(
      "createProjectThread",
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  test("renames with the cached version and advances it", async () => {
    const fetcher = rs
      .fn(async (_input: string, _init?: RequestInit) =>
        jsonResponse(privateThread(4, "Renamed")),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [privateThread(3)] }))
      .mockResolvedValueOnce(jsonResponse(privateThread(4, "Renamed")));
    rs.stubGlobal("fetch", fetcher);
    const client = getProjectAPIClient(scope);
    await client.threads.search({ limit: 10 });

    await client.threads.updateState(threadId, {
      values: { title: "Renamed" },
    });

    const [url, init] = fetcher.mock.calls[1]!;
    expect(url).toContain(`/threads/${threadId}`);
    expect(init?.method).toBe("PATCH");
    expect(jsonRequestBody(init)).toEqual({
      expected_version: 3,
      display_name: "Renamed",
    });
  });

  test("loads a missing version before delete and sends expected_version", async () => {
    const fetcher = rs
      .fn(async (_input: string, _init?: RequestInit) =>
        jsonResponse({ success: true }),
      )
      .mockResolvedValueOnce(jsonResponse(privateThread(7)))
      .mockResolvedValueOnce(jsonResponse({ success: true }));
    rs.stubGlobal("fetch", fetcher);

    await getProjectAPIClient(scope).threads.delete(threadId);

    expect(fetcher.mock.calls[0]![0]).toContain(`/threads/${threadId}`);
    const [deleteUrl, deleteInit] = fetcher.mock.calls[1]!;
    expect(deleteInit?.method).toBe("DELETE");
    expect(deleteUrl).toContain(`expected_version=7`);
  });

  test("maps project state to the single history entry useStream expects", async () => {
    const fetcher = rs.fn(async (_input: string, _init?: RequestInit) =>
      jsonResponse({
        values: { messages: [{ id: "message-1", content: "hello" }] },
        next: [],
        metadata: { source: "checkpoint" },
        checkpoint: {
          id: "66666666-6666-4666-8666-666666666666",
          ts: "2026-07-15T00:00:00Z",
        },
        checkpoint_id: "66666666-6666-4666-8666-666666666666",
        parent_checkpoint_id: null,
        created_at: "2026-07-15T00:00:00Z",
        tasks: [],
      }),
    );
    rs.stubGlobal("fetch", fetcher);

    const history = await getProjectAPIClient(scope).threads.getHistory(
      threadId,
      { limit: 1 },
    );

    expect(fetcher.mock.calls[0]![0]).toContain(`/threads/${threadId}/state`);
    expect(history).toMatchObject([
      {
        values: { messages: [{ id: "message-1", content: "hello" }] },
        checkpoint: {
          thread_id: threadId,
          checkpoint_id: "66666666-6666-4666-8666-666666666666",
        },
      },
    ]);
  });

  test("maps a missing project state to empty history", async () => {
    const fetcher = rs.fn(async (_input: string, _init?: RequestInit) =>
      jsonResponse({ detail: "Not found" }, 404),
    );
    rs.stubGlobal("fetch", fetcher);

    await expect(
      getProjectAPIClient(scope).threads.getHistory(threadId, { limit: 1 }),
    ).resolves.toEqual([]);
  });
});
