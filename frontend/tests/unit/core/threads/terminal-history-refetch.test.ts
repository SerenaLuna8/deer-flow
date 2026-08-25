import type { Message } from "@langchain/langgraph-sdk";
import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getMessageGroups } from "@/core/messages/utils";
import { messageIdentity } from "@/core/threads/message-projection";
import { fetchCanonicalThreadHistory } from "@/core/threads/use-thread-history";
import {
  captureTerminalLiveMessages,
  mergeCanonicalTerminalHistory,
} from "@/core/threads/use-thread-stream";

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
  test("refetches an explicit target Thread and its complete Run journal", async () => {
    const signal = new AbortController().signal;
    const list = rs.fn(async () => [run()]);
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

    const snapshot = await fetchCanonicalThreadHistory(
      { runs: { list } },
      "/api/projects/22222222-2222-4222-8222-222222222222/private-work",
      THREAD_ID,
      signal,
    );

    expect(snapshot.threadId).toBe(THREAD_ID);
    expect(snapshot.runs.map((entry) => entry.run_id)).toEqual([RUN_ID]);
    expect(snapshot.messages.map((message) => message.id)).toEqual([
      "question",
      "answer",
    ]);
    expect(list).toHaveBeenCalledWith(THREAD_ID, {
      limit: 1000,
      offset: 0,
      signal,
    });
    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining(`/threads/${THREAD_ID}/runs/${RUN_ID}/messages`),
      { method: "GET", signal },
    );
    expect(mockedFetch.mock.calls[1]![0]).toContain("before_seq=2");
  });

  test("keeps canonical payload/order and appends only genuinely missing live messages", () => {
    const canonical = [
      { id: "question", type: "human", content: "question" },
      { id: "answer", type: "ai", content: "canonical final" },
    ] as Message[];
    const live = [
      { id: "answer", type: "ai", content: "stale partial" },
      { id: "late-tool", type: "tool", content: "persisting" },
    ] as Message[];

    const merged = mergeCanonicalTerminalHistory(canonical, live);

    expect(merged.map((message) => message.id)).toEqual([
      "question",
      "answer",
      "late-tool",
    ]);
    expect(merged[1]?.content).toBe("canonical final");
  });

  test("keeps run-scoped clarification cards unique and before the terminal answer", () => {
    const firstRunId = "55555555-5555-4555-8555-555555555555";
    const secondRunId = "66666666-6666-4666-8666-666666666666";
    const clarification = (callId: string, question: string) => ({
      id: `clarification:${callId}`,
      type: "tool",
      name: "ask_clarification",
      tool_call_id: callId,
      content: question,
      artifact: {
        human_input: {
          version: 1,
          kind: "human_input_request",
          source: "ask_clarification",
          request_id: `clarification:${callId}`,
          tool_call_id: callId,
          question,
          input_mode: "text",
        },
      },
    });
    const firstClarification = clarification("call-stage-1", "Stage 1?");
    const secondClarification = clarification("call-stage-2", "Stage 2?");
    const canonical = [
      {
        id: "first-run-input",
        type: "human",
        content: "Create the presentation",
        run_id: firstRunId,
        additional_kwargs: { run_id: firstRunId },
      },
      {
        id: "first-clarification-call",
        type: "ai",
        content: "",
        run_id: firstRunId,
        tool_calls: [
          {
            id: "call-stage-1",
            name: "ask_clarification",
            args: { question: "Stage 1?" },
          },
        ],
      },
      { ...firstClarification, run_id: firstRunId },
      {
        id: "first-clarification-answer",
        type: "human",
        content: "Stage 1 accepted",
        run_id: secondRunId,
        additional_kwargs: {
          hide_from_ui: true,
          run_id: secondRunId,
        },
      },
      {
        id: "second-clarification-call",
        type: "ai",
        content: "",
        run_id: secondRunId,
        tool_calls: [
          {
            id: "call-stage-2",
            name: "ask_clarification",
            args: { question: "Stage 2?" },
          },
        ],
      },
      { ...secondClarification, run_id: secondRunId },
      {
        id: "terminal-answer",
        type: "ai",
        content: "Presentation complete",
        run_id: secondRunId,
      },
    ] as unknown as Message[];
    const liveCheckpoint = canonical.map((message) => {
      const unscoped = { ...message } as Message & {
        run_id?: string;
      };
      delete unscoped.run_id;
      return unscoped;
    });

    const merged = mergeCanonicalTerminalHistory(canonical, liveCheckpoint);
    const groups = getMessageGroups(merged);

    expect(
      groups.filter((group) => group.type === "assistant:clarification"),
    ).toHaveLength(2);
    expect(groups.at(-1)?.type).toBe("assistant");
    expect(merged).toEqual(canonical);
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
