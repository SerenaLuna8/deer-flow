import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  acceptProjectStreamFrame,
  advanceProjectStreamCursorState,
  clearProjectReconnectStorage,
  clearProjectThreadRuntimeState,
  CONTEXT_CAPACITY_EXCEEDED,
  CONTEXT_PROVIDER_CALL_AMBIGUOUS,
  disposeProjectAPIClient,
  emptyProjectStreamCursorState,
  getProjectAPIClient,
  isModelOutputLimitError,
  isOutputDeliveryIncompleteError,
  LLM_PROVIDER_UNAVAILABLE,
  LOOP_SAFETY_LIMIT,
  MODEL_OUTPUT_LIMIT,
  OUTPUT_DELIVERY_INCOMPLETE,
  PROJECT_STREAM_INCOMPLETE,
  SIDE_EFFECT_STATE_UNKNOWN,
  projectReconnectStorage,
  projectRunTerminalFailureEventToError,
  projectStreamFrameForUI,
  projectStreamFailureName,
  projectStreamCursorStorageKey,
  shouldReconnectProjectStream,
} from "@/core/private-work/api-client";

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const THREAD_ID = "22222222-2222-4222-8222-222222222222";
const OTHER_THREAD_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_SCOPE = {
  accountId: SCOPE.accountId,
  projectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
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
  clearProjectReconnectStorage(OTHER_SCOPE);
  disposeProjectAPIClient(SCOPE);
  disposeProjectAPIClient(OTHER_SCOPE);
  rs.unstubAllGlobals();
});

describe("private stream reconnect", () => {
  test("never lets a late consumer move the diagnostic cursor backward", () => {
    const cursor12 = {
      lastEventId: "12",
      terminalRunId: null,
    };
    const lateCursor11 = {
      lastEventId: "11",
      terminalRunId: "old-run",
    };

    expect(advanceProjectStreamCursorState(cursor12, lateCursor11)).toBe(
      cursor12,
    );
  });

  test("a stale reconnect owner cannot delete a newer run id", () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    getProjectAPIClient(SCOPE);
    const stale = projectReconnectStorage(SCOPE);
    const current = projectReconnectStorage(SCOPE);
    const key = `lg:stream:${THREAD_ID}` as const;

    stale.setItem(key, "run-old");
    current.setItem(key, "run-new");
    stale.removeItem(key);

    expect(current.getItem(key)).toBe("run-new");
    current.removeItem(key);
    expect(current.getItem(key)).toBeNull();
  });

  test("deleted-thread teardown aborts and blocks only that thread runtime", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });

    let targetSignal: AbortSignal | undefined;
    let otherSignal: AbortSignal | undefined;
    let markTargetStarted!: () => void;
    let markOtherStarted!: () => void;
    const targetStarted = new Promise<void>((resolve) => {
      markTargetStarted = resolve;
    });
    const otherStarted = new Promise<void>((resolve) => {
      markOtherStarted = resolve;
    });
    const pendingResponse = (signal: AbortSignal | null | undefined) =>
      new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener(
          "abort",
          () => reject(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      });
    rs.stubGlobal(
      "fetch",
      rs.fn(async (input: string | URL, init?: RequestInit) => {
        const url = input.toString();
        if (url.includes(THREAD_ID)) {
          targetSignal = init?.signal ?? undefined;
          markTargetStarted();
          return await pendingResponse(targetSignal);
        }
        if (url.includes(OTHER_THREAD_ID)) {
          otherSignal = init?.signal ?? undefined;
          markOtherStarted();
          return await pendingResponse(otherSignal);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const client = getProjectAPIClient(SCOPE);
    getProjectAPIClient(OTHER_SCOPE);
    const targetReconnect = projectReconnectStorage(SCOPE);
    const otherProjectReconnect = projectReconnectStorage(OTHER_SCOPE);
    const targetReconnectKey = `lg:stream:${THREAD_ID}` as const;
    const otherThreadReconnectKey = `lg:stream:${OTHER_THREAD_ID}` as const;
    targetReconnect.setItem(targetReconnectKey, "run-target");
    targetReconnect.setItem(otherThreadReconnectKey, "run-other");
    otherProjectReconnect.setItem(targetReconnectKey, "run-other-project");
    storage.setItem(
      projectStreamCursorStorageKey(SCOPE, THREAD_ID),
      JSON.stringify({ lastEventId: "9", terminalRunId: null }),
    );
    storage.setItem(
      projectStreamCursorStorageKey(SCOPE, OTHER_THREAD_ID),
      JSON.stringify({ lastEventId: "8", terminalRunId: null }),
    );
    storage.setItem(
      projectStreamCursorStorageKey(OTHER_SCOPE, THREAD_ID),
      JSON.stringify({ lastEventId: "7", terminalRunId: null }),
    );

    const targetJoin = client.runs
      .joinStream(THREAD_ID, "run-target")
      .next()
      .catch((error: unknown) => error);
    const otherJoin = client.runs
      .joinStream(OTHER_THREAD_ID, "run-other")
      .next()
      .catch((error: unknown) => error);
    await Promise.all([targetStarted, otherStarted]);

    clearProjectThreadRuntimeState(SCOPE, THREAD_ID);

    expect(targetSignal?.aborted).toBe(true);
    expect(otherSignal?.aborted).toBe(false);
    expect(targetReconnect.getItem(targetReconnectKey)).toBeNull();
    targetReconnect.setItem(targetReconnectKey, "run-late");
    expect(targetReconnect.getItem(targetReconnectKey)).toBeNull();
    expect(targetReconnect.getItem(otherThreadReconnectKey)).toBe("run-other");
    expect(otherProjectReconnect.getItem(targetReconnectKey)).toBe(
      "run-other-project",
    );
    expect(
      storage.getItem(projectStreamCursorStorageKey(SCOPE, THREAD_ID)),
    ).toBeNull();
    expect(
      storage.getItem(projectStreamCursorStorageKey(SCOPE, OTHER_THREAD_ID)),
    ).not.toBeNull();
    expect(
      storage.getItem(projectStreamCursorStorageKey(OTHER_SCOPE, THREAD_ID)),
    ).not.toBeNull();

    await targetJoin;
    disposeProjectAPIClient(SCOPE);
    await otherJoin;
  });

  test("scope disposal aborts a join and ignores its late frame", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    let streamSignal: AbortSignal | undefined;
    let resolveStream!: (response: Response) => void;
    let markStreamStarted!: () => void;
    const streamStarted = new Promise<void>((resolve) => {
      markStreamStarted = resolve;
    });
    const fetcher = rs.fn(async (input: string | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/runs/run-live")) {
        return new Response(JSON.stringify({ status: "running" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      streamSignal = init?.signal ?? undefined;
      markStreamStarted();
      return await new Promise<Response>((resolve) => {
        resolveStream = resolve;
      });
    });
    rs.stubGlobal("fetch", fetcher);
    const staleClient = getProjectAPIClient(SCOPE);
    const pending = staleClient.runs
      .joinStream(THREAD_ID, "run-live")
      .next()
      .then(
        (value) => value,
        (error: unknown) => error,
      );
    await streamStarted;

    disposeProjectAPIClient(SCOPE);
    expect(streamSignal?.aborted).toBe(true);
    const replacement = getProjectAPIClient(SCOPE);
    expect(replacement).not.toBe(staleClient);
    const cursorKey = projectStreamCursorStorageKey(SCOPE, THREAD_ID);
    storage.setItem(
      cursorKey,
      JSON.stringify({ lastEventId: "12", terminalRunId: null }),
    );
    resolveStream(
      new Response(
        [
          "event: updates",
          'data: {"delta":"late-old-consumer"}',
          "id: 11",
          "",
          "",
        ].join("\n"),
        { headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    await pending;

    expect(storage.getItem(cursorKey)).toBe(
      JSON.stringify({ lastEventId: "12", terminalRunId: null }),
    );
  });

  test("keeps the caller abort signal on initial run streams", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    const caller = new AbortController();
    let requestSignal: AbortSignal | undefined;
    let resolveStream!: (response: Response) => void;
    let markStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      markStarted = resolve;
    });
    rs.stubGlobal(
      "fetch",
      rs.fn(async (_input: string | URL, init?: RequestInit) => {
        requestSignal = init?.signal ?? undefined;
        markStarted();
        return await new Promise<Response>((resolve) => {
          resolveStream = resolve;
        });
      }),
    );

    const pending = getProjectAPIClient(SCOPE)
      .runs.stream(THREAD_ID, "lead_agent", {
        input: { messages: [] },
        signal: caller.signal,
      })
      .next()
      .then(
        (value) => value,
        (error: unknown) => error,
      );
    await started;

    expect(requestSignal?.aborted).toBe(false);
    caller.abort();
    expect(requestSignal?.aborted).toBe(true);
    resolveStream(
      new Response("", {
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    await pending;
  });

  test("replays the current run from durable origin instead of trusting another consumer cursor", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    projectReconnectStorage(SCOPE).setItem(`lg:stream:${THREAD_ID}`, "run-1");
    storage.setItem(
      projectStreamCursorStorageKey(SCOPE, THREAD_ID),
      JSON.stringify({ lastEventId: 7, terminalRunId: null }),
    );
    const fetcher = rs.fn(async (input: string | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/runs/run-1")) {
        return new Response(JSON.stringify({ status: "running" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      expect(url).not.toContain(`/threads/${THREAD_ID}/state`);
      expect(new Headers(init?.headers).get("Last-Event-ID")).toBe("0");
      return new Response(
        [
          "event: values",
          'data: {"messages":[{"id":"human-current","type":"human","content":"current prompt","additional_kwargs":{"run_id":"run-1"}},{"id":"ai-task-current","type":"ai","content":"","tool_calls":[{"id":"task-1","name":"task","args":{"description":"research"}}]}]}',
          "id: 1",
          "",
          "event: custom",
          'data: {"type":"task_running","task_id":"task-1"}',
          "id: 8",
          "",
          "event: end",
          'data: {"status":"completed"}',
          "id: 9",
          "",
          "",
        ].join("\n"),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });
    rs.stubGlobal("fetch", fetcher);
    const client = getProjectAPIClient(SCOPE);

    const frames: Array<{ id?: string; event: string; data: unknown }> = [];
    for await (const frame of client.runs.joinStream(THREAD_ID, "run-1")) {
      frames.push(frame);
    }

    expect(frames).toEqual([
      {
        id: "1",
        event: "values",
        data: {
          messages: [
            {
              id: "human-current",
              type: "human",
              content: "current prompt",
              additional_kwargs: { run_id: "run-1" },
            },
            {
              id: "ai-task-current",
              type: "ai",
              content: "",
              tool_calls: [
                {
                  id: "task-1",
                  name: "task",
                  args: { description: "research" },
                },
              ],
            },
          ],
        },
      },
      {
        id: "8",
        event: "custom",
        data: { type: "task_running", task_id: "task-1" },
      },
      {
        id: "9",
        event: "end",
        data: { status: "completed" },
      },
    ]);
  });

  test("yields a browser task within a bounded origin replay batch", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    const replayFrameCount = 128;
    const terminalEventId = replayFrameCount + 1;
    const replayBody = [
      ...Array.from({ length: replayFrameCount }, (_, index) => [
        "event: updates",
        `data: {"delta":${index + 1}}`,
        `id: ${index + 1}`,
        "",
      ]).flat(),
      "event: end",
      'data: {"status":"completed"}',
      `id: ${terminalEventId}`,
      "",
      "",
    ].join("\n");
    const encodedReplay = new TextEncoder().encode(replayBody);
    rs.stubGlobal(
      "fetch",
      rs.fn(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(encodedReplay);
                controller.close();
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
      ),
    );

    let consumedFrameCount = 0;
    let resolveBrowserTurn!: (value: number) => void;
    const browserTurn = new Promise<number>((resolve) => {
      resolveBrowserTurn = resolve;
    });
    const drainReplay = (async () => {
      for await (const frame of getProjectAPIClient(SCOPE).runs.joinStream(
        THREAD_ID,
        "run-buffered",
      )) {
        consumedFrameCount += frame ? 1 : 0;
        if (consumedFrameCount === 1) {
          setTimeout(() => resolveBrowserTurn(consumedFrameCount), 0);
        }
      }
    })();

    const consumedBeforeBrowserTurn = await browserTurn;
    await drainReplay;

    expect(consumedBeforeBrowserTurn).toBeLessThanOrEqual(64);
    expect(consumedFrameCount).toBe(terminalEventId);
  });

  test("yields a browser task while dropping a buffered duplicate replay batch", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    const duplicateFrameCount = 128;
    const replayBody = [
      "event: updates",
      'data: {"delta":"first"}',
      "id: 1",
      "",
      ...Array.from({ length: duplicateFrameCount }, () => [
        "event: updates",
        'data: {"delta":"duplicate"}',
        "id: 1",
        "",
      ]).flat(),
      "event: end",
      'data: {"status":"completed"}',
      "id: 2",
      "",
      "",
    ].join("\n");
    const encodedReplay = new TextEncoder().encode(replayBody);
    rs.stubGlobal(
      "fetch",
      rs.fn(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(encodedReplay);
                controller.close();
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
      ),
    );

    let emittedFrameCount = 0;
    let resolveBrowserTurn!: (value: number) => void;
    const browserTurn = new Promise<number>((resolve) => {
      resolveBrowserTurn = resolve;
    });
    const drainReplay = (async () => {
      for await (const frame of getProjectAPIClient(SCOPE).runs.joinStream(
        THREAD_ID,
        "run-duplicate-buffer",
      )) {
        emittedFrameCount += frame ? 1 : 0;
        if (emittedFrameCount === 1) {
          setTimeout(() => resolveBrowserTurn(emittedFrameCount), 0);
        }
      }
    })();

    const emittedBeforeBrowserTurn = await browserTurn;
    await drainReplay;

    expect(emittedBeforeBrowserTurn).toBe(1);
    expect(emittedFrameCount).toBe(2);
  });

  test("rejects a started durable stream that reaches clean EOF before terminal", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    const reconnect = projectReconnectStorage(SCOPE);
    const reconnectKey = `lg:stream:${THREAD_ID}` as const;
    reconnect.setItem(reconnectKey, "run-incomplete");
    rs.stubGlobal(
      "fetch",
      rs.fn(
        async () =>
          new Response(
            [
              "event: values",
              'data: {"messages":[{"id":"ai-partial","type":"ai","content":"partial"}]}',
              "id: 31",
              "",
              "",
            ].join("\n"),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
      ),
    );

    let frameCount = 0;
    const consume = async () => {
      for await (const frame of getProjectAPIClient(SCOPE).runs.joinStream(
        THREAD_ID,
        "run-incomplete",
      )) {
        frameCount += frame ? 1 : 0;
      }
    };

    await expect(consume()).rejects.toMatchObject({
      name: PROJECT_STREAM_INCOMPLETE,
    });
    expect(frameCount).toBe(1);
    expect(reconnect.getItem(reconnectKey)).toBe("run-incomplete");
  });

  test("allows an empty durable stream that never started", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    rs.stubGlobal(
      "fetch",
      rs.fn(
        async () =>
          new Response("", {
            headers: { "Content-Type": "text/event-stream" },
          }),
      ),
    );

    const frames: unknown[] = [];
    for await (const frame of getProjectAPIClient(SCOPE).runs.joinStream(
      THREAD_ID,
      "run-empty",
    )) {
      frames.push(frame);
    }

    expect(frames).toEqual([]);
  });

  test("allows caller abort after a durable stream has started", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    const caller = new AbortController();
    const encoder = new TextEncoder();
    rs.stubGlobal(
      "fetch",
      rs.fn(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(
                  encoder.encode(
                    [
                      "event: values",
                      'data: {"messages":[{"id":"ai-live","type":"ai","content":"live"}]}',
                      "id: 41",
                      "",
                      "",
                    ].join("\n"),
                  ),
                );
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
      ),
    );
    const stream = getProjectAPIClient(SCOPE).runs.joinStream(
      THREAD_ID,
      "run-aborted",
      { signal: caller.signal },
    );

    await expect(stream.next()).resolves.toMatchObject({
      done: false,
      value: { id: "41", event: "values" },
    });
    caller.abort();
    await expect(stream.next()).resolves.toMatchObject({ done: true });
  });

  test("an old terminal cannot delete a newer run reconnect key", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    let resolveStream!: (response: Response) => void;
    let markStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      markStarted = resolve;
    });
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => {
        markStarted();
        return await new Promise<Response>((resolve) => {
          resolveStream = resolve;
        });
      }),
    );
    const client = getProjectAPIClient(SCOPE);
    const reconnect = projectReconnectStorage(SCOPE);
    const reconnectKey = `lg:stream:${THREAD_ID}` as const;
    reconnect.setItem(reconnectKey, "run-old");
    let oldFrameCount = 0;
    const pending = (async () => {
      for await (const frame of client.runs.joinStream(THREAD_ID, "run-old")) {
        oldFrameCount += frame ? 1 : 0;
      }
    })();
    await started;

    reconnect.setItem(reconnectKey, "run-new");
    resolveStream(
      new Response(
        ["event: end", 'data: {"status":"completed"}', "id: 51", "", ""].join(
          "\n",
        ),
        { headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    await pending;

    expect(oldFrameCount).toBe(1);
    expect(reconnect.getItem(reconnectKey)).toBe("run-new");
  });

  test("uses onRunCreated to clear a terminal initial stream with no metadata frame", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => {
        return new Response(
          [
            "event: error",
            'data: {"error":"worker failed","message":"worker failed"}',
            "id: 60",
            "",
            "event: end",
            'data: {"status":"error"}',
            "id: 61",
            "",
            "",
          ].join("\n"),
          {
            headers: {
              "Content-Type": "text/event-stream",
              "Content-Location": `/threads/${THREAD_ID}/runs/run-no-metadata`,
            },
          },
        );
      }),
    );
    const client = getProjectAPIClient(SCOPE);
    const reconnect = projectReconnectStorage(SCOPE);
    const reconnectKey = `lg:stream:${THREAD_ID}` as const;
    const frames: unknown[] = [];

    for await (const frame of client.runs.stream(THREAD_ID, "lead_agent", {
      input: { messages: [] },
      onRunCreated(meta) {
        reconnect.setItem(reconnectKey, meta.run_id);
      },
    })) {
      frames.push(frame);
    }

    expect(frames).toEqual([
      {
        id: "61",
        event: "custom",
        data: {
          type: "project_run_terminal_failure",
          error: "PROJECT_RUN_TERMINAL_FAILURE",
          message: "PROJECT_RUN_TERMINAL_FAILURE",
        },
      },
    ]);
    expect(reconnect.getItem(reconnectKey)).toBeNull();
  });

  test("keeps the reconnect key when raw error reaches clean EOF before terminal", async () => {
    const storage = makeSessionStorage();
    rs.stubGlobal("window", {
      location: { origin: "http://localhost:2026" },
      sessionStorage: storage,
    });
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => {
        return new Response(
          [
            "event: error",
            'data: {"error":"worker failed","message":"worker failed"}',
            "id: 62",
            "",
            "",
          ].join("\n"),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }),
    );
    const reconnect = projectReconnectStorage(SCOPE);
    const reconnectKey = `lg:stream:${THREAD_ID}` as const;
    reconnect.setItem(reconnectKey, "run-error-eof");

    let visibleFrameCount = 0;
    const consume = async () => {
      for await (const frame of getProjectAPIClient(SCOPE).runs.joinStream(
        THREAD_ID,
        "run-error-eof",
      )) {
        visibleFrameCount += frame ? 1 : 0;
      }
    };

    await expect(consume()).rejects.toMatchObject({
      name: PROJECT_STREAM_INCOMPLETE,
    });
    expect(visibleFrameCount).toBe(0);
    expect(reconnect.getItem(reconnectKey)).toBe("run-error-eof");
  });

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
    expect(first.state).toEqual({
      lastEventId: "7",
      terminalRunId: "run-1",
    });
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

  test("preserves the full signed PostgreSQL BIGINT cursor range", () => {
    const beyondJavaScriptSafeInteger = acceptProjectStreamFrame(
      emptyProjectStreamCursorState(),
      {
        id: "9007199254740992",
        event: "updates",
        data: { delta: "large" },
      },
      "run-1",
    );
    const postgresBigintMax = acceptProjectStreamFrame(
      beyondJavaScriptSafeInteger.state,
      {
        id: "9223372036854775807",
        event: "end",
        data: { status: "completed" },
      },
      "run-1",
    );
    const overflow = acceptProjectStreamFrame(
      postgresBigintMax.state,
      {
        id: "9223372036854775808",
        event: "updates",
        data: {},
      },
      "run-2",
    );

    expect(beyondJavaScriptSafeInteger.accepted).toBe(true);
    expect(postgresBigintMax.accepted).toBe(true);
    expect(postgresBigintMax.state).toEqual({
      lastEventId: "9223372036854775807",
      terminalRunId: "run-1",
    });
    expect(overflow.accepted).toBe(false);
    expect(overflow.state).toBe(postgresBigintMax.state);
  });

  test("accepts a BIGINT frame without trusting the stored diagnostic cursor", async () => {
    const storage = makeSessionStorage();
    storage.setItem(
      projectStreamCursorStorageKey(SCOPE, THREAD_ID),
      JSON.stringify({
        lastEventId: "9223372036854775806",
        terminalRunId: null,
      }),
    );
    const fetcher = rs.fn(async (input: string | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/runs/run-bigint")) {
        return new Response(JSON.stringify({ status: "running" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      expect(new Headers(init?.headers).get("Last-Event-ID")).toBe("0");
      return new Response(
        [
          "event: end",
          'data: {"status":"completed"}',
          "id: 9223372036854775807",
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

    const frames: Array<{ id?: string }> = [];
    for await (const frame of getProjectAPIClient(SCOPE).runs.joinStream(
      THREAD_ID,
      "run-bigint",
    )) {
      frames.push(frame);
    }

    expect(frames.map((frame) => frame.id)).toEqual(["9223372036854775807"]);
    expect(
      storage.getItem(projectStreamCursorStorageKey(SCOPE, THREAD_ID)),
    ).toBe(
      JSON.stringify({
        lastEventId: "9223372036854775807",
        terminalRunId: "run-bigint",
      }),
    );
  });

  test("turns a durable failed terminal into a lifecycle custom event", () => {
    for (const status of ["error", "failed", "timeout"]) {
      expect(
        projectStreamFrameForUI({
          id: "9",
          event: "end",
          data: { status },
        }),
      ).toEqual({
        id: "9",
        event: "custom",
        data: {
          type: "project_run_terminal_failure",
          error: "PROJECT_RUN_TERMINAL_FAILURE",
          message: "PROJECT_RUN_TERMINAL_FAILURE",
        },
      });
    }

    const completed = {
      id: "10",
      event: "end",
      data: { status: "completed" },
    };
    expect(projectStreamFrameForUI(completed)).toBe(completed);

    const suspendedForApproval = {
      id: "11",
      event: "end",
      data: {
        status: "success",
        suspended_approval_id: "33333333-3333-4333-8333-333333333333",
      },
    };
    expect(projectStreamFrameForUI(suspendedForApproval)).toBe(
      suspendedForApproval,
    );
  });

  test("keeps an expected durable Run failure out of the SDK error channel", () => {
    const frame = projectStreamFrameForUI({
      id: "1",
      event: "end",
      data: { status: "error" },
    });

    // The LangGraph SDK unconditionally calls console.error for event:error.
    // Durable Run failures are expected business outcomes and must reach the
    // conversation lifecycle without triggering Next.js' console overlay.
    expect(frame).toEqual({
      id: "1",
      event: "custom",
      data: {
        type: "project_run_terminal_failure",
        error: "PROJECT_RUN_TERMINAL_FAILURE",
        message: "PROJECT_RUN_TERMINAL_FAILURE",
      },
    });
    expect(projectRunTerminalFailureEventToError(frame.data)).toMatchObject({
      name: "PROJECT_RUN_TERMINAL_FAILURE",
      message: "PROJECT_RUN_TERMINAL_FAILURE",
    });
  });

  test("preserves only the stable model output-limit name until the durable terminal", () => {
    const diagnostic = {
      id: "8",
      event: "error",
      data: {
        name: MODEL_OUTPUT_LIMIT,
        message: "safe public detail",
      },
    };

    expect(projectStreamFailureName(diagnostic)).toBe(MODEL_OUTPUT_LIMIT);
    expect(
      projectStreamFrameForUI({
        id: "9",
        event: "end",
        data: { status: "error", error_code: MODEL_OUTPUT_LIMIT },
      }),
    ).toEqual({
      id: "9",
      event: "custom",
      data: {
        type: "project_run_terminal_failure",
        error: MODEL_OUTPUT_LIMIT,
        message: MODEL_OUTPUT_LIMIT,
      },
    });
    expect(
      projectStreamFrameForUI(
        { id: "10", event: "end", data: { status: "error" } },
        projectStreamFailureName(diagnostic),
      ),
    ).toEqual({
      id: "10",
      event: "custom",
      data: {
        type: "project_run_terminal_failure",
        error: MODEL_OUTPUT_LIMIT,
        message: MODEL_OUTPUT_LIMIT,
      },
    });
    expect(
      projectStreamFailureName({
        event: "error",
        data: { name: "UNKNOWN_NEW_BACKEND_ERROR" },
      }),
    ).toBeNull();
    expect(isModelOutputLimitError({ name: MODEL_OUTPUT_LIMIT })).toBe(true);
    expect(isModelOutputLimitError(new Error(MODEL_OUTPUT_LIMIT))).toBe(true);
    expect(isModelOutputLimitError({ name: "other" })).toBe(false);
  });

  test("preserves the stable loop safety limit at the durable terminal", () => {
    const failureCode = LOOP_SAFETY_LIMIT;
    const terminal = {
      id: "9",
      event: "end",
      data: { status: "error", error_code: failureCode },
    };

    expect(projectStreamFailureName(terminal)).toBe(failureCode);
    expect(projectStreamFrameForUI(terminal)).toEqual({
      id: "9",
      event: "custom",
      data: {
        type: "project_run_terminal_failure",
        error: failureCode,
        message: failureCode,
      },
    });
  });

  test("preserves unknown side-effect state at the durable terminal", () => {
    const terminal = {
      id: "9",
      event: "end",
      data: { status: "error", error_code: SIDE_EFFECT_STATE_UNKNOWN },
    };

    expect(projectStreamFailureName(terminal)).toBe(SIDE_EFFECT_STATE_UNKNOWN);
    expect(projectStreamFrameForUI(terminal)).toEqual({
      id: "9",
      event: "custom",
      data: {
        type: "project_run_terminal_failure",
        error: SIDE_EFFECT_STATE_UNKNOWN,
        message: SIDE_EFFECT_STATE_UNKNOWN,
      },
    });
  });

  test("preserves both terminal Context authority failures", () => {
    for (const failureCode of [
      CONTEXT_CAPACITY_EXCEEDED,
      CONTEXT_PROVIDER_CALL_AMBIGUOUS,
    ] as const) {
      const terminal = {
        id: "9",
        event: "end",
        data: { status: "error", error_code: failureCode },
      };

      expect(projectStreamFailureName(terminal)).toBe(failureCode);
      expect(projectStreamFrameForUI(terminal)).toEqual({
        id: "9",
        event: "custom",
        data: {
          type: "project_run_terminal_failure",
          error: failureCode,
          message: failureCode,
        },
      });
    }
  });

  test("preserves the stable output-delivery failure until the durable terminal", () => {
    const diagnostic = {
      id: "8",
      event: "error",
      data: {
        name: OUTPUT_DELIVERY_INCOMPLETE,
        message: "safe public detail",
      },
    };

    expect(projectStreamFailureName(diagnostic)).toBe(
      OUTPUT_DELIVERY_INCOMPLETE,
    );
    expect(
      projectStreamFrameForUI(
        { id: "9", event: "end", data: { status: "error" } },
        projectStreamFailureName(diagnostic),
      ),
    ).toEqual({
      id: "9",
      event: "custom",
      data: {
        type: "project_run_terminal_failure",
        error: OUTPUT_DELIVERY_INCOMPLETE,
        message: OUTPUT_DELIVERY_INCOMPLETE,
      },
    });
    expect(
      projectStreamFrameForUI({
        id: "10",
        event: "end",
        data: {
          status: "error",
          error_code: OUTPUT_DELIVERY_INCOMPLETE,
        },
      }),
    ).toEqual({
      id: "10",
      event: "custom",
      data: {
        type: "project_run_terminal_failure",
        error: OUTPUT_DELIVERY_INCOMPLETE,
        message: OUTPUT_DELIVERY_INCOMPLETE,
      },
    });
    expect(
      isOutputDeliveryIncompleteError({
        error_code: OUTPUT_DELIVERY_INCOMPLETE,
      }),
    ).toBe(true);
    expect(isOutputDeliveryIncompleteError({ name: "other" })).toBe(false);
  });

  test("preserves the stable provider-unavailable failure until the durable terminal", () => {
    const diagnostic = {
      id: "8",
      event: "error",
      data: {
        name: LLM_PROVIDER_UNAVAILABLE,
        message: "safe public detail",
      },
    };

    expect(projectStreamFailureName(diagnostic)).toBe(LLM_PROVIDER_UNAVAILABLE);
    expect(
      projectStreamFrameForUI(
        {
          id: "9",
          event: "end",
          data: { status: "error" },
        },
        projectStreamFailureName(diagnostic),
      ),
    ).toEqual({
      id: "9",
      event: "custom",
      data: {
        type: "project_run_terminal_failure",
        error: LLM_PROVIDER_UNAVAILABLE,
        message: LLM_PROVIDER_UNAVAILABLE,
      },
    });
  });

  test("carries a live output-limit diagnostic through its durable terminal", async () => {
    const storage = makeSessionStorage();
    const fetcher = rs.fn(async (input: string | URL) => {
      const url = input.toString();
      if (url.endsWith("/runs/run-output-limit")) {
        return new Response(JSON.stringify({ status: "error" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(
        [
          "event: metadata",
          'data: {"run_id":"run-output-limit","thread_id":"thread-1"}',
          "id: 1",
          "",
          "event: error",
          `data: {"name":"${MODEL_OUTPUT_LIMIT}","message":"output limit"}`,
          "id: 2",
          "",
          "event: end",
          'data: {"status":"error"}',
          "id: 3",
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

    const frames: unknown[] = [];
    for await (const frame of getProjectAPIClient(SCOPE).runs.joinStream(
      "thread-1",
      "run-output-limit",
    )) {
      frames.push(frame);
    }

    expect(frames.at(-1)).toEqual({
      id: "3",
      event: "custom",
      data: {
        type: "project_run_terminal_failure",
        error: MODEL_OUTPUT_LIMIT,
        message: MODEL_OUTPUT_LIMIT,
      },
    });
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
    ).toBe(JSON.stringify({ lastEventId: "8", terminalRunId: "run-1" }));
    expect(fetcher).toHaveBeenCalledTimes(1);

    const replayed: Array<{ id?: string }> = [];
    for await (const frame of client.runs.joinStream("thread-1", "run-1")) {
      replayed.push(frame);
    }
    expect(replayed.map((frame) => frame.id)).toEqual(["7", "8"]);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  test("surfaces a durable terminal failure to the SDK stream consumer", async () => {
    const storage = makeSessionStorage();
    const fetcher = rs.fn(async (input: string | URL) => {
      const url = input.toString();
      if (url.endsWith("/runs/run-failed")) {
        return new Response(JSON.stringify({ status: "error" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(
        [
          "event: metadata",
          'data: {"run_id":"run-failed","thread_id":"thread-1"}',
          "id: 1",
          "",
          "event: error",
          'data: {"error":"RuntimeError","message":"worker failed"}',
          "id: 2",
          "",
          "event: end",
          'data: {"status":"error"}',
          "id: 3",
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
    const reconnect = projectReconnectStorage(SCOPE);
    const reconnectKey = "lg:stream:thread-1" as const;
    reconnect.setItem(reconnectKey, "run-failed");

    const frames: unknown[] = [];
    for await (const frame of client.runs.joinStream(
      "thread-1",
      "run-failed",
    )) {
      frames.push(frame);
    }

    expect(frames).toEqual([
      {
        id: "1",
        event: "metadata",
        data: { run_id: "run-failed", thread_id: "thread-1" },
      },
      {
        id: "3",
        event: "custom",
        data: {
          type: "project_run_terminal_failure",
          error: "PROJECT_RUN_TERMINAL_FAILURE",
          message: "PROJECT_RUN_TERMINAL_FAILURE",
        },
      },
    ]);
    expect(reconnect.getItem(reconnectKey)).toBeNull();
  });
});
