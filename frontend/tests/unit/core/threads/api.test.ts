import { afterEach, describe, expect, test, rs } from "@rstest/core";

import { GatewayApiError } from "@/core/api/errors";
import {
  compactThreadContext,
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
  test("defaults to the server keep policy and forwards a token keep override", async () => {
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
      keep: { type: "tokens", value: 32_000 },
      signal: controller.signal,
    });

    expect(jsonBody(fetcher.mock.calls[0]?.[1])).toEqual({ force: true });
    expect(jsonBody(fetcher.mock.calls[1]?.[1])).toEqual({
      force: true,
      keep: { type: "tokens", value: 32_000 },
    });
    expect(fetcher.mock.calls[1]?.[1]?.signal).toBe(controller.signal);
  });

  test("rejects the retired message-count keep before any request is sent", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(compactResponse()),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=thread-token" });
    rs.stubGlobal("fetch", fetcher);

    await expect(
      compactThreadContext(THREAD_ID, {
        apiBaseURL: API_BASE_URL,
        keep: { type: "messages", value: 0 } as never,
      }),
    ).rejects.toThrow();

    expect(fetcher.mock.calls.length).toBe(0);
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
