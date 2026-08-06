import { afterEach, describe, expect, test, rs } from "@rstest/core";

import { GatewayApiError } from "@/core/api/errors";
import {
  compactThreadContext,
  compactThreadForDream,
  DREAM_COMPACTION_MAX_PASSES,
  DreamThreadCompactionError,
  type ThreadCompactResponse,
} from "@/core/threads/api";

const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const API_BASE_URL = `/api/projects/${PROJECT_ID}/private-work`;

function compactResponse(
  overrides: Partial<ThreadCompactResponse> = {},
): ThreadCompactResponse {
  return {
    thread_id: THREAD_ID,
    compacted: false,
    reason: "not_enough_messages",
    removed_message_count: 0,
    preserved_message_count: 0,
    summary_updated: false,
    checkpoint_id: null,
    total_tokens: 0,
    ...overrides,
  };
}

function jsonBody(init: RequestInit | undefined) {
  if (typeof init?.body !== "string") {
    throw new Error("Expected a JSON string request body");
  }
  return JSON.parse(init.body) as unknown;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("thread compaction client", () => {
  test("keeps ordinary compact unchanged and sends keep=0 only for Dream", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(compactResponse()),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=thread-token" });
    rs.stubGlobal("fetch", fetcher);

    await compactThreadContext(THREAD_ID, {
      apiBaseURL: API_BASE_URL,
      signal: controller.signal,
    });
    await compactThreadContext(THREAD_ID, {
      apiBaseURL: API_BASE_URL,
      keep: { type: "messages", value: 0 },
      signal: controller.signal,
    });

    expect(jsonBody(fetcher.mock.calls[0]?.[1])).toEqual({ force: true });
    expect(jsonBody(fetcher.mock.calls[1]?.[1])).toEqual({
      force: true,
      keep: { type: "messages", value: 0 },
    });
    expect(fetcher.mock.calls[1]?.[1]?.signal).toBe(controller.signal);
  });

  test("drains every model-bounded Dream fragment before accepting the terminal reason", async () => {
    const responses = [
      compactResponse({
        compacted: true,
        reason: null,
        removed_message_count: 4,
        preserved_message_count: 5,
        summary_updated: true,
        checkpoint_id: "checkpoint-1",
        total_tokens: 90,
      }),
      compactResponse({
        compacted: true,
        reason: null,
        removed_message_count: 3,
        preserved_message_count: 1,
        summary_updated: true,
        checkpoint_id: "checkpoint-2",
        total_tokens: 45,
      }),
      compactResponse({ preserved_message_count: 1, total_tokens: 10 }),
    ];
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(responses.shift()),
    );
    const onCompacted = rs.fn();
    rs.stubGlobal("fetch", fetcher);

    const result = await compactThreadForDream(
      THREAD_ID,
      { apiBaseURL: API_BASE_URL },
      onCompacted,
    );

    expect(result).toEqual({
      compactedPasses: 2,
      latestCheckpointId: "checkpoint-2",
    });
    expect(onCompacted).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenCalledTimes(3);
    for (const [, init] of fetcher.mock.calls) {
      expect(jsonBody(init)).toEqual({
        force: true,
        keep: { type: "messages", value: 0 },
      });
    }
  });

  test("fails closed when the server returns an unexpected stop reason", async () => {
    const fetcher = rs.fn(async () =>
      Response.json(compactResponse({ reason: "compaction_failed" })),
    );
    rs.stubGlobal("fetch", fetcher);

    const promise = compactThreadForDream(THREAD_ID, {
      apiBaseURL: API_BASE_URL,
    });

    await expect(promise).rejects.toMatchObject({
      name: "DreamThreadCompactionError",
      reason: "unexpected_result",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  test("fails closed when a successful response cannot prove checkpoint progress", async () => {
    const fetcher = rs.fn(async () =>
      Response.json(
        compactResponse({
          compacted: true,
          reason: null,
          summary_updated: true,
          checkpoint_id: "checkpoint-without-removal",
        }),
      ),
    );
    const onCompacted = rs.fn();
    rs.stubGlobal("fetch", fetcher);

    const promise = compactThreadForDream(
      THREAD_ID,
      { apiBaseURL: API_BASE_URL },
      onCompacted,
    );

    await expect(promise).rejects.toMatchObject({
      name: "DreamThreadCompactionError",
      reason: "no_progress",
    });
    expect(onCompacted).not.toHaveBeenCalled();
  });

  test("fails closed when a repeated checkpoint proves the drain stopped progressing", async () => {
    const responses = [
      compactResponse({
        compacted: true,
        reason: null,
        removed_message_count: 1,
        preserved_message_count: 2,
        summary_updated: true,
        checkpoint_id: "repeated-checkpoint",
      }),
      compactResponse({
        compacted: true,
        reason: null,
        removed_message_count: 1,
        preserved_message_count: 1,
        summary_updated: true,
        checkpoint_id: "repeated-checkpoint",
      }),
    ];
    const fetcher = rs.fn(async () => Response.json(responses.shift()));
    const onCompacted = rs.fn();
    rs.stubGlobal("fetch", fetcher);

    const promise = compactThreadForDream(
      THREAD_ID,
      { apiBaseURL: API_BASE_URL },
      onCompacted,
    );

    await expect(promise).rejects.toBeInstanceOf(DreamThreadCompactionError);
    await expect(promise).rejects.toMatchObject({ reason: "no_progress" });
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(onCompacted).toHaveBeenCalledTimes(1);
  });

  test("fails closed at the bounded pass limit instead of admitting an unbounded drain", async () => {
    expect(DREAM_COMPACTION_MAX_PASSES).toBe(64);
    let pass = 0;
    const fetcher = rs.fn(async () => {
      pass += 1;
      if (pass > DREAM_COMPACTION_MAX_PASSES) {
        throw new Error("Dream compaction exceeded the expected request bound");
      }
      return Response.json(
        compactResponse({
          compacted: true,
          reason: null,
          removed_message_count: 1,
          preserved_message_count: 1,
          summary_updated: true,
          checkpoint_id: `bounded-checkpoint-${pass}`,
        }),
      );
    });
    const onCompacted = rs.fn();
    rs.stubGlobal("fetch", fetcher);

    const promise = compactThreadForDream(
      THREAD_ID,
      { apiBaseURL: API_BASE_URL },
      onCompacted,
    );

    await expect(promise).rejects.toMatchObject({
      name: "DreamThreadCompactionError",
      reason: "pass_limit",
    });
    expect(fetcher).toHaveBeenCalledTimes(DREAM_COMPACTION_MAX_PASSES);
    expect(onCompacted).toHaveBeenCalledTimes(DREAM_COMPACTION_MAX_PASSES);
  });

  test("preserves the Gateway error envelope for compact failures", async () => {
    rs.stubGlobal("fetch", async () =>
      Response.json(
        {
          detail: {
            code: "PRIVATE_WORK_CONFLICT",
            message: "Private work conflict.",
          },
        },
        { status: 409 },
      ),
    );

    const promise = compactThreadContext(THREAD_ID, {
      apiBaseURL: API_BASE_URL,
    });

    await expect(promise).rejects.toBeInstanceOf(GatewayApiError);
    await expect(promise).rejects.toMatchObject({
      status: 409,
      code: "PRIVATE_WORK_CONFLICT",
    });
  });
});
