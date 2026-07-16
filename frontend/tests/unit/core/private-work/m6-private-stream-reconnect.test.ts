import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  acceptProjectStreamFrame,
  clearProjectReconnectStorage,
  disposeProjectAPIClient,
  emptyProjectStreamCursorState,
  getProjectAPIClient,
  projectStreamCursorStorageKey,
  shouldReconnectProjectStream,
} from "@/core/private-work/api-client";

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};

function makeSessionStorage() {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    getItem: rs.fn((key: string) => values.get(key) ?? null),
    key: rs.fn((index: number) => [...values.keys()][index] ?? null),
    removeItem: rs.fn((key: string) => values.delete(key)),
    setItem: rs.fn((key: string, value: string) => values.set(key, value)),
  };
}

afterEach(() => {
  clearProjectReconnectStorage(SCOPE);
  disposeProjectAPIClient(SCOPE);
  rs.unstubAllGlobals();
});

describe("M6 private stream reconnect", () => {
  test("dedupes a replayed terminal frame", () => {
    const first = acceptProjectStreamFrame(
      emptyProjectStreamCursorState(),
      { id: "7", event: "end", data: { status: "completed" } },
      "run-1",
    );
    const replay = acceptProjectStreamFrame(
      first.state,
      { id: "7", event: "end", data: { status: "completed" } },
      "run-1",
    );

    expect(first.accepted).toBe(true);
    expect(first.state).toEqual({ lastEventId: 7, terminalRunId: "run-1" });
    expect(replay.accepted).toBe(false);
    expect(replay.state).toBe(first.state);
    expect(shouldReconnectProjectStream(first.state, "run-1")).toBe(false);
    expect(shouldReconnectProjectStream(first.state, "run-2")).toBe(true);
  });

  test("accepts only canonical positive server event IDs", () => {
    for (const id of ["-1", "+1", "01", "1.0", " 1", "abc", "0"]) {
      const result = acceptProjectStreamFrame(
        emptyProjectStreamCursorState(),
        { id, event: "updates", data: {} },
        "run-1",
      );
      expect(result.accepted).toBe(false);
    }
  });

  test("persists the confirmed cursor, dedupes replay, and stops after terminal", async () => {
    const storage = makeSessionStorage();
    const fetcher = rs.fn(async (input: string | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/runs/run-1")) {
        return new Response(JSON.stringify({ status: "running" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      expect(new Headers(init?.headers).get("Last-Event-ID")).toBe("0");
      return new Response(
        [
          "event: updates",
          'data: {"delta":"first"}',
          "id: 7",
          "",
          "event: updates",
          'data: {"delta":"replayed"}',
          "id: 7",
          "",
          "event: end",
          'data: {"status":"completed"}',
          "id: 8",
          "",
          "",
        ].join("\n"),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    rs.stubGlobal("fetch", fetcher);
    const client = getProjectAPIClient(SCOPE);

    const frames: Array<{ id?: string }> = [];
    for await (const frame of client.runs.joinStream("thread-1", "run-1", {
      lastEventId: "-1",
    })) {
      frames.push(frame);
    }

    expect(frames.map((frame) => frame.id)).toEqual(["7", "8"]);
    expect(
      storage.getItem(projectStreamCursorStorageKey(SCOPE, "thread-1")),
    ).toBe(JSON.stringify({ lastEventId: 8, terminalRunId: "run-1" }));
    expect(fetcher).toHaveBeenCalledTimes(2);

    await expect(
      client.runs.joinStream("thread-1", "run-1").next(),
    ).resolves.toMatchObject({ done: true });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});
