import { beforeEach, describe, expect, rs, test } from "@rstest/core";

import { GatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { buildRunMessagesUrl, fetchRunMessagesPage } from "@/core/threads/api";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));

const mockedFetch = rs.mocked(fetchWithAuth);
const projectBaseURL =
  "/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function runMessage(seq: unknown = "1") {
  return {
    run_id: "run-1",
    seq,
    content: {
      type: "ai",
      content: "Hello",
      additional_kwargs: {},
      response_metadata: {},
    },
    metadata: { caller: "lead_agent" },
    created_at: "2026-07-22T00:00:00Z",
  };
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("project run message history API", () => {
  test("builds only the encoded project-scoped per-run route", () => {
    expect(
      buildRunMessagesUrl(projectBaseURL, "thread/with space", "run?one", "18"),
    ).toBe(
      `${projectBaseURL}/threads/thread%2Fwith%20space/runs/run%3Fone/messages?before_seq=18`,
    );
    expect(() => buildRunMessagesUrl("/api", "thread-1", "run-1")).toThrow(
      "project private-work URL",
    );
  });

  test("loads and strictly parses the snake-case page contract", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({
        data: [runMessage("9007199254740993")],
        has_more: false,
      }),
    );

    await expect(
      fetchRunMessagesPage(
        projectBaseURL,
        "thread-1",
        "run-1",
        "9007199254740994",
      ),
    ).resolves.toEqual({
      data: [runMessage("9007199254740993")],
      has_more: false,
    });
    expect(mockedFetch).toHaveBeenCalledWith(
      `${projectBaseURL}/threads/thread-1/runs/run-1/messages?before_seq=9007199254740994`,
      expect.objectContaining({ method: "GET" }),
    );
  });

  test("rejects malformed or widened message page responses", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse({ data: [runMessage()], hasMore: false }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          data: [{ ...runMessage(), owner_user_id: "must-not-pass" }],
          has_more: false,
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ data: [], has_more: true }))
      .mockResolvedValueOnce(
        jsonResponse({
          data: [runMessage(9_007_199_254_740_993)],
          has_more: false,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ data: [runMessage("01")], has_more: false }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          data: [runMessage("9223372036854775808")],
          has_more: false,
        }),
      );

    await expect(
      fetchRunMessagesPage(projectBaseURL, "thread-1", "run-1"),
    ).rejects.toThrow();
    await expect(
      fetchRunMessagesPage(projectBaseURL, "thread-1", "run-1"),
    ).rejects.toThrow();
    await expect(
      fetchRunMessagesPage(projectBaseURL, "thread-1", "run-1"),
    ).rejects.toThrow("cannot advance without data");
    await expect(
      fetchRunMessagesPage(projectBaseURL, "thread-1", "run-1"),
    ).rejects.toThrow();
    await expect(
      fetchRunMessagesPage(projectBaseURL, "thread-1", "run-1"),
    ).rejects.toThrow();
    await expect(
      fetchRunMessagesPage(projectBaseURL, "thread-1", "run-1"),
    ).rejects.toThrow("exceeds PostgreSQL BIGINT");
  });

  test("throws one typed HTTP error and leaves retry to the user", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            code: "PRIVATE_WORK_NOT_FOUND",
            message: "Private work was not found.",
          },
        },
        404,
      ),
    );

    const request = fetchRunMessagesPage(projectBaseURL, "thread-1", "run-1");
    await expect(request).rejects.toBeInstanceOf(GatewayApiError);
    await expect(request).rejects.toMatchObject({
      status: 404,
      code: "PRIVATE_WORK_NOT_FOUND",
    });
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });
});
