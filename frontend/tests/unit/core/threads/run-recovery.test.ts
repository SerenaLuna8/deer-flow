import type { Message, Run } from "@langchain/langgraph-sdk";
import { describe, expect, test } from "@rstest/core";

import {
  CURRENT_UPLOAD_UNAVAILABLE,
  MODEL_OUTPUT_LIMIT,
  OUTPUT_DELIVERY_INCOMPLETE,
} from "@/core/private-work/api-client";
import {
  findTerminalFailureRunIdsToReload,
  getLatestRegenerationTarget,
  latestRunFailureCode,
  resolveRunFailureCode,
  resolveRunFailureRunId,
  shouldReloadEmptyRunAfterTerminalFailure,
} from "@/core/threads/hooks";

function run(status: string, error: string | null): Run {
  return {
    run_id: "run-1",
    thread_id: "thread-1",
    assistant_id: "lead_agent",
    created_at: "2026-08-13T00:00:00Z",
    updated_at: "2026-08-13T00:00:01Z",
    status,
    error,
    metadata: {},
    multitask_strategy: "reject",
  } as unknown as Run;
}

describe("run failure recovery", () => {
  test("reloads an already-seen Run when it becomes a terminal failure", () => {
    const previousStatuses = new Map<string, string>([
      ["run-1", "running"],
      ["run-2", "error"],
    ]);

    expect(
      findTerminalFailureRunIdsToReload(previousStatuses, [
        run("error", "MCP_CONNECTION_FAILED"),
      ]),
    ).toEqual(["run-1"]);
    expect(
      findTerminalFailureRunIdsToReload(previousStatuses, [
        { ...run("error", "still failed"), run_id: "run-2" },
      ]),
    ).toEqual([]);
    expect(
      findTerminalFailureRunIdsToReload(previousStatuses, [
        run("success", null),
      ]),
    ).toEqual([]);
  });

  test("retries an empty message request that raced with terminal failure", () => {
    expect(
      shouldReloadEmptyRunAfterTerminalFailure({
        statusAtRequest: "running",
        currentStatus: "error",
        visibleMessageCount: 0,
      }),
    ).toBe(true);
    expect(
      shouldReloadEmptyRunAfterTerminalFailure({
        statusAtRequest: "running",
        currentStatus: "error",
        visibleMessageCount: 1,
      }),
    ).toBe(false);
    expect(
      shouldReloadEmptyRunAfterTerminalFailure({
        statusAtRequest: "error",
        currentStatus: "error",
        visibleMessageCount: 0,
      }),
    ).toBe(false);
  });

  test("recognizes the stable code from live and refreshed durable failures", () => {
    const durableFailure = [run("error", MODEL_OUTPUT_LIMIT)];

    expect(latestRunFailureCode(durableFailure)).toBe(MODEL_OUTPUT_LIMIT);
    expect(resolveRunFailureCode(undefined, durableFailure)).toBe(
      MODEL_OUTPUT_LIMIT,
    );
    expect(resolveRunFailureRunId(undefined, null, durableFailure)).toBe(
      "run-1",
    );

    const liveError = new Error(MODEL_OUTPUT_LIMIT);
    liveError.name = MODEL_OUTPUT_LIMIT;
    expect(resolveRunFailureCode(liveError, [])).toBe(MODEL_OUTPUT_LIMIT);
    expect(resolveRunFailureRunId(liveError, "run-live", [])).toBe("run-live");
  });

  test("keeps unknown and non-failed durable values on the generic path", () => {
    expect(
      latestRunFailureCode([run("error", "NEW_BACKEND_ERROR")]),
    ).toBeNull();
    expect(
      latestRunFailureCode([run("success", MODEL_OUTPUT_LIMIT)]),
    ).toBeNull();
    expect(
      resolveRunFailureRunId(undefined, null, [
        run("error", "NEW_BACKEND_ERROR"),
      ]),
    ).toBe("run-1");
  });

  test("recognizes incomplete output delivery without offering a model replay", () => {
    const durableFailure = [run("error", OUTPUT_DELIVERY_INCOMPLETE)];

    expect(latestRunFailureCode(durableFailure)).toBe(
      OUTPUT_DELIVERY_INCOMPLETE,
    );
    expect(resolveRunFailureCode(undefined, durableFailure)).toBe(
      OUTPUT_DELIVERY_INCOMPLETE,
    );
    expect(resolveRunFailureRunId(undefined, null, durableFailure)).toBe(
      "run-1",
    );

    const liveError = new Error(OUTPUT_DELIVERY_INCOMPLETE);
    liveError.name = OUTPUT_DELIVERY_INCOMPLETE;
    expect(resolveRunFailureCode(liveError, [])).toBe(
      OUTPUT_DELIVERY_INCOMPLETE,
    );
    expect(resolveRunFailureRunId(liveError, "run-live", [])).toBe("run-live");
  });

  test("recognizes a current-upload failure from live and durable terminals", () => {
    const durableFailure = [run("error", CURRENT_UPLOAD_UNAVAILABLE)];

    expect(latestRunFailureCode(durableFailure)).toBe(
      CURRENT_UPLOAD_UNAVAILABLE,
    );
    expect(resolveRunFailureCode(undefined, durableFailure)).toBe(
      CURRENT_UPLOAD_UNAVAILABLE,
    );
    expect(resolveRunFailureRunId(undefined, null, durableFailure)).toBe(
      "run-1",
    );

    const liveError = new Error(CURRENT_UPLOAD_UNAVAILABLE);
    liveError.name = CURRENT_UPLOAD_UNAVAILABLE;
    expect(resolveRunFailureCode(liveError, [])).toBe(
      CURRENT_UPLOAD_UNAVAILABLE,
    );
    expect(resolveRunFailureRunId(liveError, "run-live", [])).toBe("run-live");
  });

  test("reuses the latest assistant turn as the existing regenerate target", () => {
    const messages = [
      { type: "human", id: "human-1", content: "question" },
      {
        type: "ai",
        id: "ai-reasoning",
        content: "",
        run_id: "run-1",
        additional_kwargs: { reasoning_content: "partial" },
      },
      {
        type: "ai",
        id: "ai-partial",
        content: "partial answer",
        run_id: "run-1",
      },
    ] as Message[];

    expect(getLatestRegenerationTarget(messages, "run-1")).toEqual({
      messageId: "ai-partial",
      supersededMessageIds: ["ai-reasoning", "ai-partial"],
    });
  });

  test("keeps a reasoning-only empty AI message retryable", () => {
    const messages = [
      { type: "human", id: "human-1", content: "question" },
      {
        type: "ai",
        id: "ai-reasoning-only",
        content: "",
        run_id: "run-1",
        additional_kwargs: { reasoning_content: "truncated reasoning" },
      },
    ] as Message[];

    expect(getLatestRegenerationTarget(messages, "run-1")).toEqual({
      messageId: "ai-reasoning-only",
      supersededMessageIds: ["ai-reasoning-only"],
    });
  });

  test("never falls back to a previous Run when the failed Run has no AI message", () => {
    const messages = [
      { type: "human", id: "human-old", content: "old question" },
      {
        type: "ai",
        id: "ai-old",
        content: "old answer",
        run_id: "run-old",
      },
      { type: "human", id: "human-new", content: "new question" },
    ] as Message[];

    expect(getLatestRegenerationTarget(messages, "run-failed")).toBeNull();
  });

  test("does not offer the recovery replay after tool activity", () => {
    const messages = [
      { type: "human", id: "human-1", content: "do something" },
      {
        type: "ai",
        id: "ai-tool-call",
        content: "",
        run_id: "run-1",
        tool_calls: [
          { id: "call-1", name: "write_file", args: { path: "result.txt" } },
        ],
      },
      {
        type: "tool",
        id: "tool-1",
        tool_call_id: "call-1",
        content: "done",
        run_id: "run-1",
      },
      {
        type: "ai",
        id: "ai-truncated",
        content: "partial",
        run_id: "run-1",
      },
    ] as Message[];

    expect(getLatestRegenerationTarget(messages, "run-1")).toBeNull();
  });

  test("blocks invalid and legacy tool-call payloads from manual recovery", () => {
    const unsafeMessages = [
      {
        invalid_tool_calls: [
          { id: "invalid-1", name: "write_file", args: "{" },
        ],
      },
      {
        additional_kwargs: {
          function_call: { name: "write_file", arguments: "{}" },
        },
      },
      {
        additional_kwargs: {
          tool_calls: { id: "call-object", function: { name: "write_file" } },
        },
      },
    ];

    for (const [index, unsafe] of unsafeMessages.entries()) {
      const messages = [
        { type: "human", id: `human-${index}`, content: "do something" },
        {
          type: "ai",
          id: `ai-unsafe-${index}`,
          content: "",
          run_id: "run-1",
          ...unsafe,
        },
        {
          type: "ai",
          id: `ai-truncated-${index}`,
          content: "partial",
          run_id: "run-1",
        },
      ] as Message[];

      expect(getLatestRegenerationTarget(messages, "run-1")).toBeNull();
    }
  });
});
