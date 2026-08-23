import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import { useQuery } from "@tanstack/react-query";

rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn(),
  useQuery: rs.fn(),
  useQueryClient: rs.fn(),
}));
rs.mock("@/core/private-work/provider", () => ({
  usePrivateWorkAccess: rs.fn((explicit) => explicit),
}));

import type { ProjectPrivateWorkScope } from "@/core/private-work/types";
import { CONTEXT_AUTHORITY_REFETCH_INTERVAL_MS } from "@/core/threads/context-usage";
import { useThreadContextUsage } from "@/core/threads/thread-actions";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const MODEL_NAME = "44444444-4444-4444-8444-444444444444";
const RUN_A = "55555555-5555-4555-8555-555555555555";
const RUN_B = "66666666-6666-4666-8666-666666666666";

type QueryOptions = {
  queryKey: readonly unknown[];
  enabled: boolean;
  refetchInterval?: number;
  refetchIntervalInBackground?: boolean;
  refetchOnWindowFocus?: boolean;
};

const privateWork = {
  apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
  scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
} as ProjectPrivateWorkScope;
const mockedUseQuery = rs.mocked(useQuery);

beforeEach(() => {
  rs.clearAllMocks();
});

describe("Thread context authority freshness hook", () => {
  test("polls only the lightweight marker and keys the full reading by that marker", () => {
    let marker = `active:${RUN_A}`;
    mockedUseQuery.mockImplementation((options) => {
      const query = options as unknown as QueryOptions;
      if (query.queryKey.at(-1) === "authority") {
        return {
          data: { thread_id: THREAD_ID, cache_marker: marker },
          error: null,
          isError: false,
          isFetching: false,
          isLoading: false,
        } as never;
      }
      return {
        data: { thread_id: THREAD_ID },
        error: null,
        isError: false,
        isFetching: false,
        isLoading: false,
      } as never;
    });

    useThreadContextUsage(THREAD_ID, {
      modelName: MODEL_NAME,
      privateWork,
    });
    const firstMarker = mockedUseQuery.mock
      .calls[0]![0] as unknown as QueryOptions;
    const firstReading = mockedUseQuery.mock
      .calls[1]![0] as unknown as QueryOptions;

    expect(firstMarker).toMatchObject({
      enabled: true,
      refetchInterval: CONTEXT_AUTHORITY_REFETCH_INTERVAL_MS,
      refetchIntervalInBackground: false,
      refetchOnWindowFocus: true,
    });
    expect(firstMarker.queryKey).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      "thread-context-usage",
      THREAD_ID,
      "authority",
    ]);
    expect(firstReading).toMatchObject({
      enabled: true,
      refetchOnWindowFocus: false,
    });
    expect(firstReading.queryKey.at(-1)).toBe(`active:${RUN_A}`);

    mockedUseQuery.mockClear();
    useThreadContextUsage(THREAD_ID, {
      modelName: MODEL_NAME,
      privateWork,
    });
    const unchangedReading = mockedUseQuery.mock
      .calls[1]![0] as unknown as QueryOptions;
    expect(unchangedReading.queryKey).toEqual(firstReading.queryKey);

    marker = `active:${RUN_B}`;
    mockedUseQuery.mockClear();
    useThreadContextUsage(THREAD_ID, {
      modelName: MODEL_NAME,
      privateWork,
    });
    const changedReading = mockedUseQuery.mock
      .calls[1]![0] as unknown as QueryOptions;
    expect(changedReading.queryKey).not.toEqual(firstReading.queryKey);
    expect(changedReading.queryKey.at(-1)).toBe(`active:${RUN_B}`);
  });
});
