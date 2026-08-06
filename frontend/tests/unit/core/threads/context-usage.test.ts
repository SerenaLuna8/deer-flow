import { afterEach, describe, expect, test, rs } from "@rstest/core";

import {
  fetchThreadContextUsage,
  threadContextUsageQueryKey,
  type ThreadContextUsageResponse,
} from "@/core/threads/context-usage";

const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const API_BASE_URL = `/api/projects/${PROJECT_ID}/private-work`;

function fractionUsage(): ThreadContextUsageResponse {
  const primary = {
    type: "fraction" as const,
    configured_value: 0.8,
    current_value: 0.45,
    threshold_value: 0.8,
    remaining_value: 0.35,
    progress_percent: 56.25,
    reached: false,
    context_window_tokens: 258_000,
    threshold_tokens: 206_400,
  };
  return {
    thread_id: THREAD_ID,
    enabled: true,
    estimated_tokens: 115_000,
    message_count: 18,
    summary_present: true,
    context_window_tokens: 258_000,
    triggers: [primary],
    primary_trigger: primary,
  };
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("thread context usage client", () => {
  test("uses the project-private endpoint, forwards AbortSignal, and parses the strict response", async () => {
    const controller = new AbortController();
    const body = fractionUsage();
    const fetcher = rs.fn(async () => Response.json(body));
    rs.stubGlobal("fetch", fetcher);

    await expect(
      fetchThreadContextUsage(THREAD_ID, {
        apiBaseURL: API_BASE_URL,
        signal: controller.signal,
      }),
    ).resolves.toEqual(body);

    expect(fetcher).toHaveBeenCalledWith(
      `${API_BASE_URL}/threads/${THREAD_ID}/context-usage`,
      expect.objectContaining({ method: "GET", signal: controller.signal }),
    );
  });

  test("keeps the query identity thread-specific before the project scope is applied", () => {
    expect(threadContextUsageQueryKey(THREAD_ID)).toEqual([
      "thread-context-usage",
      THREAD_ID,
    ]);
  });

  test("returns unavailable for an authority-hidden thread", async () => {
    rs.stubGlobal("fetch", async () => new Response(null, { status: 404 }));

    await expect(
      fetchThreadContextUsage(THREAD_ID, { apiBaseURL: API_BASE_URL }),
    ).resolves.toBeNull();
  });

  test("rejects unknown root and nested fields instead of accepting a drifting contract", async () => {
    const body = fractionUsage();
    rs.stubGlobal("fetch", async () =>
      Response.json({
        ...body,
        private_policy: "must-not-leak",
        triggers: [{ ...body.triggers[0], unexpected: true }],
      }),
    );

    await expect(
      fetchThreadContextUsage(THREAD_ID, { apiBaseURL: API_BASE_URL }),
    ).rejects.toThrow();
  });

  test("rejects an invalid fraction trigger instead of rendering misleading progress", async () => {
    const body = fractionUsage();
    rs.stubGlobal("fetch", async () =>
      Response.json({
        ...body,
        triggers: [
          {
            ...body.triggers[0],
            configured_value: 1.2,
            threshold_value: 1.2,
          },
        ],
        primary_trigger: {
          ...body.primary_trigger,
          configured_value: 1.2,
          threshold_value: 1.2,
        },
      }),
    );

    await expect(
      fetchThreadContextUsage(THREAD_ID, { apiBaseURL: API_BASE_URL }),
    ).rejects.toThrow();
  });
});
