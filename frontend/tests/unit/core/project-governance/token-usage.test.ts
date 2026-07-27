import { beforeEach, describe, expect, rs, test } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  fetchProjectTokenUsageSeries,
  projectTokenUsageSeriesQueryKey,
  projectTokenUsageSeriesQueryOptions,
  projectTokenUsageSeriesSchema,
  projectUsageQueryKey,
  type ProjectTokenUsageSeries,
} from "@/core/project-governance/usage";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const OTHER_PROJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const SCOPE = { accountId: ACCOUNT_ID, projectId: PROJECT_ID };
const START_TIME = Date.parse("2026-07-26T13:00:00.000Z");

function bucketStart(index: number): string {
  return new Date(START_TIME + index * 60 * 60 * 1000).toISOString();
}

function makeTokenUsageSeries(): ProjectTokenUsageSeries {
  const points = Array.from({ length: 24 }, (_, index) => ({
    bucket_start: bucketStart(index),
    input_tokens: index === 22 ? 12 : index === 23 ? 10 : 0,
    output_tokens: index === 22 ? 5 : index === 23 ? 7 : 0,
    // Total token usage is provider-reported. It is intentionally not
    // constrained to input + output.
    total_tokens: index === 22 ? 20 : index === 23 ? 19 : 0,
  }));
  return {
    window_start: bucketStart(0),
    window_end: new Date(START_TIME + (23 * 60 + 30) * 60 * 1000).toISOString(),
    bucket_minutes: 60,
    totals: {
      input_tokens: 22,
      output_tokens: 12,
      total_tokens: 39,
    },
    points,
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const mockedFetch = rs.mocked(fetchWithAuth);

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("project token usage series contract", () => {
  test("accepts exactly 24 consecutive hourly buckets and provider totals", () => {
    const series = makeTokenUsageSeries();

    expect(projectTokenUsageSeriesSchema.parse(series)).toEqual(series);
    expect(series.points[22]!.total_tokens).not.toBe(
      series.points[22]!.input_tokens + series.points[22]!.output_tokens,
    );
  });

  test("rejects incomplete, discontinuous, inconsistent, or private data", () => {
    const series = makeTokenUsageSeries();

    expect(
      projectTokenUsageSeriesSchema.safeParse({
        ...series,
        points: series.points.slice(1),
      }).success,
    ).toBe(false);

    const discontinuous = structuredClone(series);
    discontinuous.points[12]!.bucket_start = new Date(
      Date.parse(discontinuous.points[12]!.bucket_start) + 30 * 60 * 1000,
    ).toISOString();
    expect(projectTokenUsageSeriesSchema.safeParse(discontinuous).success).toBe(
      false,
    );

    expect(
      projectTokenUsageSeriesSchema.safeParse({
        ...series,
        totals: { ...series.totals, total_tokens: 40 },
      }).success,
    ).toBe(false);

    expect(
      projectTokenUsageSeriesSchema.safeParse({
        ...series,
        owner_user_id: ACCOUNT_ID,
      }).success,
    ).toBe(false);
    expect(
      projectTokenUsageSeriesSchema.safeParse({
        ...series,
        totals: { ...series.totals, run_ids: ["private-run"] },
      }).success,
    ).toBe(false);
    expect(
      projectTokenUsageSeriesSchema.safeParse({
        ...series,
        points: series.points.map((point, index) =>
          index === 0
            ? {
                input_tokens: point.input_tokens,
                output_tokens: point.output_tokens,
                total_tokens: point.total_tokens,
                started_at: point.bucket_start,
              }
            : point,
        ),
      }).success,
    ).toBe(false);
  });

  test("uses the scoped usage key and forwards the query AbortSignal", async () => {
    const series = makeTokenUsageSeries();
    mockedFetch.mockResolvedValueOnce(jsonResponse(series));
    const signal = new AbortController().signal;

    expect(projectTokenUsageSeriesQueryKey(SCOPE)).toEqual([
      ...projectUsageQueryKey(SCOPE),
      "token-series",
    ]);
    expect(
      projectTokenUsageSeriesQueryKey({
        accountId: ACCOUNT_ID,
        projectId: OTHER_PROJECT_ID,
      }),
    ).not.toEqual(projectTokenUsageSeriesQueryKey(SCOPE));

    const options = projectTokenUsageSeriesQueryOptions(SCOPE);
    expect(options.queryKey).toEqual(projectTokenUsageSeriesQueryKey(SCOPE));
    await expect(options.queryFn({ signal })).resolves.toEqual(series);

    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/usage/token-series`,
      { signal },
    );
  });

  test("the direct fetch helper preserves the caller signal", async () => {
    const series = makeTokenUsageSeries();
    mockedFetch.mockResolvedValueOnce(jsonResponse(series));
    const signal = new AbortController().signal;

    await expect(fetchProjectTokenUsageSeries(SCOPE, signal)).resolves.toEqual(
      series,
    );
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/usage/token-series`,
      { signal },
    );
  });
});
