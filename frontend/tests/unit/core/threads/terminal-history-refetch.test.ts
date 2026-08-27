import type { Message } from "@langchain/langgraph-sdk";
import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { messageIdentity } from "@/core/threads/message-projection";
import type { RunMessage } from "@/core/threads/types";
import {
  fetchCanonicalRunHistory,
  pruneConfirmedTerminalRunFallbacks,
  projectTerminalRunFallbacks,
  resolveTerminalRunHistoryCommit,
} from "@/core/threads/use-thread-history";
import { captureTerminalLiveMessages } from "@/core/threads/use-thread-stream";

const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const RUN_ID = "44444444-4444-4444-8444-444444444444";
const mockedFetch = rs.mocked(fetchWithAuth);

function run() {
  return {
    run_id: RUN_ID,
    thread_id: THREAD_ID,
    assistant_id: null,
    created_at: "2026-08-24T00:00:00Z",
    updated_at: "2026-08-24T00:00:02Z",
    status: "success",
    metadata: {},
    multitask_strategy: "reject",
    error: null,
    model_name: null,
    execution_profile: null,
  };
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("terminal canonical history", () => {
  test("refetches only the exact terminal Run and its complete journal", async () => {
    const signal = new AbortController().signal;
    const get = rs.fn(async () => run());
    mockedFetch
      .mockResolvedValueOnce(
        Response.json({
          data: [
            {
              run_id: RUN_ID,
              seq: "2",
              content: { id: "answer", type: "ai", content: "final" },
              metadata: {},
              created_at: "2026-08-24T00:00:02Z",
            },
          ],
          has_more: true,
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          data: [
            {
              run_id: RUN_ID,
              seq: "1",
              content: { id: "question", type: "human", content: "question" },
              metadata: {},
              created_at: "2026-08-24T00:00:01Z",
            },
          ],
          has_more: false,
        }),
      );

    const snapshot = await fetchCanonicalRunHistory(
      { runs: { get } },
      "/api/projects/22222222-2222-4222-8222-222222222222/private-work",
      THREAD_ID,
      RUN_ID,
      signal,
    );

    expect(snapshot.threadId).toBe(THREAD_ID);
    expect(snapshot.run.run_id).toBe(RUN_ID);
    expect(snapshot.rows.map((row) => row.content.id)).toEqual([
      "question",
      "answer",
    ]);
    expect(get).toHaveBeenCalledWith(THREAD_ID, RUN_ID, { signal });
    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining(`/threads/${THREAD_ID}/runs/${RUN_ID}/messages`),
      { method: "GET", signal },
    );
    expect(mockedFetch.mock.calls[1]![0]).toContain("before_seq=2");
  });

  test("rejects a Run that canonical REST has not confirmed as terminal", async () => {
    const signal = new AbortController().signal;
    const get = rs.fn(async () => ({ ...run(), status: "running" }));

    await expect(
      fetchCanonicalRunHistory(
        { runs: { get } },
        "/api/projects/22222222-2222-4222-8222-222222222222/private-work",
        THREAD_ID,
        RUN_ID,
        signal,
      ),
    ).rejects.toThrow("Canonical REST did not confirm the terminal Run.");
    expect(mockedFetch).not.toHaveBeenCalled();
  });

  test("prepares canonical payloads with only captured-only messages as a session fallback", () => {
    const canonicalRows = [
      {
        run_id: RUN_ID,
        seq: "1",
        content: {
          id: "commit-question",
          type: "human",
          content: "canonical question",
        },
        metadata: { caller: "lead_agent" },
        created_at: "2026-08-24T00:00:01Z",
      },
      {
        run_id: RUN_ID,
        seq: "3",
        content: {
          id: "commit-final",
          type: "ai",
          content: "canonical final",
        },
        metadata: { caller: "lead_agent" },
        created_at: "2026-08-24T00:00:03Z",
      },
    ] as RunMessage[];
    const captured = [
      {
        id: "commit-question",
        type: "human",
        content: "stale question",
        run_id: RUN_ID,
      },
      {
        id: "commit-process",
        type: "ai",
        content: "captured process",
        run_id: RUN_ID,
      },
      {
        id: "commit-final",
        type: "ai",
        content: "stale final",
        run_id: RUN_ID,
      },
      {
        id: "other-run-message",
        type: "ai",
        content: "must stay outside the exact commit",
        run_id: "55555555-5555-4555-8555-555555555555",
      },
    ] as unknown as Message[];

    const result = resolveTerminalRunHistoryCommit({
      boundThreadId: THREAD_ID,
      snapshot: {
        threadId: THREAD_ID,
        run: run() as never,
        rows: canonicalRows,
      },
      capturedMessages: captured,
    });

    expect(result.kind).toBe("committed");
    if (result.kind !== "committed") return;
    expect(result.messages.map((message) => message.id)).toEqual([
      "commit-question",
      "commit-process",
      "commit-final",
    ]);
    expect(result.messages[0]?.content).toBe("canonical question");
    expect(result.messages[2]?.content).toBe("canonical final");
    expect(result.capturedFallback.map((message) => message.id)).toEqual([
      "commit-question",
      "commit-process",
      "commit-final",
    ]);
  });

  test("projects a captured fallback inside only its exact Run window", () => {
    const olderRunId = "55555555-5555-4555-8555-555555555555";
    const canonicalHistory = [
      {
        id: "older-answer",
        type: "ai",
        content: "older",
        run_id: olderRunId,
      },
      {
        id: "window-question",
        type: "human",
        content: "canonical question",
        run_id: RUN_ID,
      },
      {
        id: "window-final",
        type: "ai",
        content: "canonical final",
        run_id: RUN_ID,
      },
    ] as unknown as Message[];
    const capturedFallbacks = new Map<string, Message[]>([
      [
        RUN_ID,
        [
          {
            id: "window-question",
            type: "human",
            content: "stale question",
            run_id: RUN_ID,
          },
          {
            id: "window-process",
            type: "ai",
            content: "captured process",
            run_id: RUN_ID,
          },
          {
            id: "window-final",
            type: "ai",
            content: "stale final",
            run_id: RUN_ID,
          },
        ] as unknown as Message[],
      ],
    ]);
    const olderRun = {
      ...run(),
      run_id: olderRunId,
      created_at: "2026-08-23T00:00:00Z",
      updated_at: "2026-08-23T00:00:02Z",
    };

    const messages = projectTerminalRunFallbacks(
      canonicalHistory,
      capturedFallbacks,
      [run() as never, olderRun as never],
      new Set(),
    );

    expect(messages.map((message) => message.id)).toEqual([
      "older-answer",
      "window-question",
      "window-process",
      "window-final",
    ]);
    expect(messages[1]?.content).toBe("canonical question");
    expect(messages[3]?.content).toBe("canonical final");
  });

  test("does not guess where to place a captured-only projection without a shared anchor", () => {
    expect(() =>
      resolveTerminalRunHistoryCommit({
        boundThreadId: THREAD_ID,
        snapshot: {
          threadId: THREAD_ID,
          run: run() as never,
          rows: [
            {
              run_id: RUN_ID,
              seq: "2",
              content: {
                id: "unanchored-canonical-final",
                type: "ai",
                content: "canonical final",
              },
              metadata: { caller: "lead_agent" },
              created_at: "2026-08-24T00:00:02Z",
            },
          ],
        },
        capturedMessages: [
          {
            id: "unanchored-captured-process",
            type: "ai",
            content: "captured process",
            run_id: RUN_ID,
          } as Message,
        ],
      }),
    ).toThrow("Captured terminal messages have no canonical anchor.");
  });

  test("rejects a late commit after the history hook has switched Threads", () => {
    expect(
      resolveTerminalRunHistoryCommit({
        boundThreadId: "66666666-6666-4666-8666-666666666666",
        snapshot: {
          threadId: THREAD_ID,
          run: run() as never,
          rows: [],
        },
        capturedMessages: [],
      }),
    ).toEqual({ kind: "stale" });
  });

  test("releases session fallbacks after canonical absorption or Run supersession", () => {
    const absorbedRunId = RUN_ID;
    const supersededRunId = "77777777-7777-4777-8777-777777777777";
    const pendingRunId = "88888888-8888-4888-8888-888888888888";
    const message = (id: string, runId: string) =>
      ({ id, type: "ai", content: id, run_id: runId }) as Message;
    const fallbacks = new Map<string, Message[]>([
      [absorbedRunId, [message("absorbed", absorbedRunId)]],
      [supersededRunId, [message("superseded", supersededRunId)]],
      [pendingRunId, [message("pending", pendingRunId)]],
    ]);

    const remaining = pruneConfirmedTerminalRunFallbacks(
      fallbacks,
      [message("absorbed", absorbedRunId)],
      new Set([supersededRunId]),
    );

    expect([...remaining.keys()]).toEqual([pendingRunId]);
  });

  test("keeps reused tool-call ids isolated by checkpoint Run boundaries", () => {
    const firstRunId = "77777777-7777-4777-8777-777777777777";
    const secondRunId = "88888888-8888-4888-8888-888888888888";
    const reusedToolCallId = "reused-tool-call";
    const captured = captureTerminalLiveMessages([], [
      {
        id: "first-run-boundary",
        type: "human",
        content: "first",
        additional_kwargs: { run_id: firstRunId },
      },
      {
        id: "first-tool-result",
        type: "tool",
        name: "read_file",
        tool_call_id: reusedToolCallId,
        content: "first result",
      },
      {
        id: "second-run-boundary",
        type: "human",
        content: "second",
        additional_kwargs: {
          hide_from_ui: true,
          run_id: secondRunId,
        },
      },
      {
        id: "second-tool-result",
        type: "tool",
        name: "read_file",
        tool_call_id: reusedToolCallId,
        content: "second result",
      },
    ] as Message[]);

    expect(
      captured
        .filter((message) => message.type === "tool")
        .map(messageIdentity),
    ).toEqual([
      `run:${firstRunId}\u0000tool:${reusedToolCallId}`,
      `run:${secondRunId}\u0000tool:${reusedToolCallId}`,
    ]);
  });

  test("carries an archived Run boundary into a compacted checkpoint tail", () => {
    const runId = "99999999-9999-4999-8999-999999999999";
    const toolCallId = "compacted-tool-call";
    const captured = captureTerminalLiveMessages(
      [
        {
          id: "archived-run-boundary",
          type: "human",
          content: "archived input",
          additional_kwargs: { run_id: runId },
        },
      ] as Message[],
      [
        {
          id: "compacted-tool-result",
          type: "tool",
          name: "read_file",
          tool_call_id: toolCallId,
          content: "tail result",
        },
      ] as Message[],
    );

    expect(messageIdentity(captured.at(-1)!)).toBe(
      `run:${runId}\u0000tool:${toolCallId}`,
    );
  });
});
