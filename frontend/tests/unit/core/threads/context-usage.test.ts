import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  CONTEXT_PROJECTION_EVENT_NAME,
  createThreadContextProjectionReadModel,
  fetchThreadContextUsage,
  mergeThreadContextProjection,
  parseThreadContextProjection,
  threadContextProjectionStreamURL,
  threadContextUsageQueryKey,
  type ContextProjectionReadState,
  type ThreadContextProjection,
} from "@/core/threads/context-usage";

const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const EXECUTION_ID = "44444444-4444-4444-8444-444444444444";
const API_BASE_URL =
  "/api/projects/22222222-2222-4222-8222-222222222222/private-work";

export function contextProjection(
  overrides: Partial<ThreadContextProjection> = {},
): ThreadContextProjection {
  return {
    contract_version: 2,
    thread_id: THREAD_ID,
    subject: {
      kind: "lead_thread",
      thread_id: THREAD_ID,
      execution_id: null,
    },
    phase: "idle",
    projection_seq: "9007199254740993",
    evidence_seq: "27",
    context_window_generation: "55555555-5555-4555-8555-555555555555",
    checkpoint_id: "checkpoint-27",
    projector_revision: "context-projector-v2",
    model: {
      identity_digest:
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      context_window_tokens: 300_000,
    },
    basis: "hybrid",
    coverage: "complete",
    freshness: "current",
    totals: {
      projected_tokens: 134_100,
      lower_bound_tokens: 134_100,
      safety_upper_bound_tokens: 141_000,
      context_window_tokens: 300_000,
      remaining_tokens: 165_900,
      progress_percent: 44.7,
    },
    lanes: [
      {
        lane: "system_prompt",
        projected_tokens: 5_700,
        lower_bound_tokens: 5_700,
        safety_upper_bound_tokens: 6_000,
      },
      {
        lane: "agent_instructions",
        projected_tokens: 4_800,
        lower_bound_tokens: 4_800,
        safety_upper_bound_tokens: 5_100,
      },
      {
        lane: "tool_definitions",
        projected_tokens: 23_500,
        lower_bound_tokens: 23_500,
        safety_upper_bound_tokens: 24_500,
      },
      {
        lane: "skills",
        projected_tokens: 2_800,
        lower_bound_tokens: 2_800,
        safety_upper_bound_tokens: 3_000,
      },
      {
        lane: "mcp_dynamic_tools",
        projected_tokens: 3_300,
        lower_bound_tokens: 3_300,
        safety_upper_bound_tokens: 3_500,
      },
      {
        lane: "subagent_definitions",
        projected_tokens: 1_600,
        lower_bound_tokens: 1_600,
        safety_upper_bound_tokens: 1_700,
      },
      {
        lane: "summarized_conversation",
        projected_tokens: 7_400,
        lower_bound_tokens: 7_400,
        safety_upper_bound_tokens: 7_800,
      },
      {
        lane: "conversation",
        projected_tokens: 85_000,
        lower_bound_tokens: 85_000,
        safety_upper_bound_tokens: 89_400,
      },
    ],
    last_provider_observation: {
      provider_call_id: "b".repeat(64),
      input_tokens: 132_800,
      observed_at: "2026-08-27T08:00:00Z",
    },
    compaction: {
      enabled: true,
      threshold_tokens: 240_000,
      reached: false,
      authority: "idle_history",
      blocked_reason: null,
    },
    notices: [],
    as_of: "2026-08-27T08:00:01Z",
    ...overrides,
  };
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("Context Projection v2 contract", () => {
  test("accepts the strict safe payload and preserves BIGINT sequences as decimal strings", () => {
    const value = contextProjection({
      projection_seq: "9223372036854775807",
      evidence_seq: "9223372036854775806",
    });

    expect(parseThreadContextProjection(value)).toEqual(value);
    expect(parseThreadContextProjection(value).projection_seq).toBe(
      "9223372036854775807",
    );
  });

  test("rejects unknown fields, unsafe sequence numbers, and totals that disagree with the lanes", () => {
    const value = contextProjection();

    expect(() =>
      parseThreadContextProjection({ ...value, private_evidence: "secret" }),
    ).toThrow();
    expect(() =>
      parseThreadContextProjection({ ...value, projection_seq: 27 }),
    ).toThrow();
    expect(() =>
      parseThreadContextProjection({
        ...value,
        totals: { ...value.totals, projected_tokens: 134_101 },
      }),
    ).toThrow();
  });

  test("loads Lead history without a composer model and addresses a Sub-Agent by execution id", async () => {
    const controller = new AbortController();
    const lead = contextProjection();
    const subagent = contextProjection({
      subject: {
        kind: "subagent_task",
        thread_id: THREAD_ID,
        execution_id: EXECUTION_ID,
      },
      phase: "settled",
    });
    const fetcher = rs
      .fn()
      .mockResolvedValueOnce(Response.json(lead))
      .mockResolvedValueOnce(Response.json(subagent));
    rs.stubGlobal("fetch", fetcher);

    await expect(
      fetchThreadContextUsage(THREAD_ID, {
        apiBaseURL: API_BASE_URL,
        signal: controller.signal,
        subject: { kind: "lead_thread" },
      }),
    ).resolves.toEqual(lead);
    await expect(
      fetchThreadContextUsage(THREAD_ID, {
        apiBaseURL: API_BASE_URL,
        subject: { kind: "subagent_task", executionId: EXECUTION_ID },
      }),
    ).resolves.toEqual(subagent);

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      `${API_BASE_URL}/threads/${THREAD_ID}/context-usage`,
      expect.objectContaining({ method: "GET", signal: controller.signal }),
    );
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      `${API_BASE_URL}/threads/${THREAD_ID}/context-usage?subject_kind=subagent_task&subject_id=${EXECUTION_ID}`,
      expect.objectContaining({ method: "GET" }),
    );
    expect(threadContextUsageQueryKey(THREAD_ID)).toEqual([
      "thread-context-usage",
      THREAD_ID,
    ]);
  });

  test("uses one Thread-owned stream from a decimal cursor", () => {
    expect(
      threadContextProjectionStreamURL(API_BASE_URL, THREAD_ID, "27"),
    ).toBe(
      `${API_BASE_URL}/threads/${THREAD_ID}/context-usage/stream?after_seq=27`,
    );
    expect(CONTEXT_PROJECTION_EVENT_NAME).toBe("context.projection.updated.v2");
  });
});

describe("Context Projection monotonic read model", () => {
  test("a late REST result cannot replace a newer SSE projection", () => {
    const streamed = contextProjection({ projection_seq: "9007199254740994" });
    const lateRest = contextProjection({ projection_seq: "9007199254740993" });

    expect(mergeThreadContextProjection(streamed, lateRest)).toBe(streamed);
    expect(mergeThreadContextProjection(lateRest, streamed)).toBe(streamed);
    expect(mergeThreadContextProjection(streamed, streamed)).toBe(streamed);
  });

  test("shares one stream across Lead and Sub-Agent subjects and prevents writes after disposal", async () => {
    let resolveLead!: (value: ThreadContextProjection | null) => void;
    const leadRequest = new Promise<ThreadContextProjection | null>(
      (resolve) => {
        resolveLead = resolve;
      },
    );
    let streamListener!: (value: string) => void;
    const closeStream = rs.fn();
    const openStream = rs.fn((_afterSeq, listener) => {
      streamListener = listener;
      return closeStream;
    });
    const load = rs.fn(
      (
        subject: { kind: "lead_thread" } | { kind: "subagent_task" },
        _signal: AbortSignal,
      ) =>
        subject.kind === "lead_thread"
          ? leadRequest
          : Promise.resolve(
              contextProjection({
                projection_seq: "12",
                subject: {
                  kind: "subagent_task",
                  thread_id: THREAD_ID,
                  execution_id: EXECUTION_ID,
                },
              }),
            ),
    );
    const model = createThreadContextProjectionReadModel({
      threadId: THREAD_ID,
      load,
      openStream,
      isActive: () => true,
    });
    const leadStates: ContextProjectionReadState[] = [];
    const subagentStates: ContextProjectionReadState[] = [];
    const unsubscribeLead = model.subscribe({ kind: "lead_thread" }, (state) =>
      leadStates.push(state),
    );
    const unsubscribeSubagent = model.subscribe(
      { kind: "subagent_task", executionId: EXECUTION_ID },
      (state) => subagentStates.push(state),
    );

    expect(openStream).toHaveBeenCalledTimes(1);
    streamListener(JSON.stringify(contextProjection({ projection_seq: "11" })));
    resolveLead(contextProjection({ projection_seq: "10" }));
    await Promise.resolve();
    await Promise.resolve();

    expect(
      model.getSnapshot({ kind: "lead_thread" }).data?.projection_seq,
    ).toBe("11");
    expect(
      model.getSnapshot({
        kind: "subagent_task",
        executionId: EXECUTION_ID,
      }).data?.projection_seq,
    ).toBe("12");

    model.refresh();
    await Promise.resolve();
    await Promise.resolve();
    expect(load).toHaveBeenCalledTimes(4);
    expect(
      model.getSnapshot({ kind: "lead_thread" }).data?.projection_seq,
    ).toBe("11");

    unsubscribeLead();
    unsubscribeSubagent();
    await Promise.resolve();
    expect(closeStream).toHaveBeenCalledTimes(1);
    const notificationsBeforeLateFrame = leadStates.length;
    streamListener(JSON.stringify(contextProjection({ projection_seq: "13" })));
    expect(leadStates).toHaveLength(notificationsBeforeLateFrame);
    expect(model.getSnapshot({ kind: "lead_thread" }).data).toBeUndefined();
  });

  test("keeps one Thread-wide cursor without caching unrequested historical Task heads", async () => {
    let streamListener!: (value: string) => void;
    const closeStreams: Array<ReturnType<typeof rs.fn>> = [];
    const openStream = rs.fn((_afterSeq, listener) => {
      streamListener = listener;
      const close = rs.fn();
      closeStreams.push(close);
      return close;
    });
    const load = rs.fn(async () => null);
    const model = createThreadContextProjectionReadModel({
      threadId: THREAD_ID,
      load,
      openStream,
      isActive: () => true,
    });
    const unsubscribeLead = model.subscribe(
      { kind: "lead_thread" },
      () => undefined,
    );
    const historicalExecutionId = "66666666-6666-4666-8666-666666666666";

    expect(openStream).toHaveBeenNthCalledWith(1, "0", expect.any(Function));
    streamListener(
      JSON.stringify(
        contextProjection({
          projection_seq: "9223372036854775806",
          subject: {
            kind: "subagent_task",
            thread_id: THREAD_ID,
            execution_id: historicalExecutionId,
          },
        }),
      ),
    );

    expect(
      model.getSnapshot({
        kind: "subagent_task",
        executionId: historicalExecutionId,
      }).data,
    ).toBeUndefined();

    model.reopenStream();

    expect(closeStreams[0]).toHaveBeenCalledTimes(1);
    expect(openStream).toHaveBeenNthCalledWith(
      2,
      "9223372036854775806",
      expect.any(Function),
    );
    expect(load).toHaveBeenCalledTimes(1);

    const unsubscribeSubagent = model.subscribe(
      { kind: "subagent_task", executionId: historicalExecutionId },
      () => undefined,
    );
    await Promise.resolve();
    expect(load).toHaveBeenCalledTimes(2);

    unsubscribeLead();
    unsubscribeSubagent();
    await Promise.resolve();
  });

  test("scope inactivity prevents a late REST write", async () => {
    let active = true;
    let resolveRequest!: (value: ThreadContextProjection | null) => void;
    const model = createThreadContextProjectionReadModel({
      threadId: THREAD_ID,
      load: () =>
        new Promise((resolve) => {
          resolveRequest = resolve;
        }),
      openStream: () => () => undefined,
      isActive: () => active,
    });
    const listener = rs.fn();
    model.subscribe({ kind: "lead_thread" }, listener);
    active = false;
    resolveRequest(contextProjection());
    await Promise.resolve();
    await Promise.resolve();

    expect(model.getSnapshot({ kind: "lead_thread" }).data).toBeUndefined();
    expect(listener).not.toHaveBeenCalled();
  });

  test("maps hidden Threads to null without exposing whether they are forbidden or missing", async () => {
    rs.stubGlobal("fetch", async () => new Response(null, { status: 404 }));
    await expect(
      fetchThreadContextUsage(THREAD_ID, {
        apiBaseURL: API_BASE_URL,
        subject: { kind: "lead_thread" },
      }),
    ).resolves.toBeNull();

    rs.stubGlobal("fetch", async () => new Response(null, { status: 403 }));
    await expect(
      fetchThreadContextUsage(THREAD_ID, {
        apiBaseURL: API_BASE_URL,
        subject: { kind: "lead_thread" },
      }),
    ).resolves.toBeNull();
  });
});
