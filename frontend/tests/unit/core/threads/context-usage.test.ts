import { afterEach, describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

import {
  CONTEXT_AUTHORITY_REFETCH_INTERVAL_MS,
  fetchThreadContextAuthority,
  fetchThreadContextUsage,
  threadContextAuthorityQueryKey,
  threadContextUsageReadingQueryKey,
  threadContextUsageQueryKey,
  type ThreadContextUsageResponse,
} from "@/core/threads/context-usage";
import {
  invalidateStartedThreadContextUsage,
  invalidateStoppedThreadCaches,
  latestContextUsageRunObservation,
  stopThreadAndInvalidateCaches,
} from "@/core/threads/hooks";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const MODEL_NAME = "44444444-4444-4444-8444-444444444444";
const ACTIVE_RUN_ID = "55555555-5555-4555-8555-555555555555";
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
    error_allowance_tokens: 23_000,
    safety_bound_tokens: 138_000,
    provider_input_tokens: 116_250,
    estimator_revision: "provider-request-engineering-v1",
    error_contract:
      "versioned_engineering_allowance_for_app_owned_serialized_material_plus_declared_provider_overhead",
    components: {
      compressible: {
        estimated_tokens: 100_000,
        error_allowance_tokens: 20_000,
        safety_bound_tokens: 120_000,
      },
      fixed: {
        estimated_tokens: 14_000,
        error_allowance_tokens: 2_800,
        safety_bound_tokens: 16_800,
      },
      ephemeral: {
        estimated_tokens: 1_000,
        error_allowance_tokens: 200,
        safety_bound_tokens: 1_200,
      },
    },
    fixed_over_trigger: false,
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

  test("loads the lightweight authority marker without loading full usage inputs", async () => {
    const controller = new AbortController();
    const fetcher = rs.fn(async () =>
      Response.json({
        thread_id: THREAD_ID,
        cache_marker: `active:${ACTIVE_RUN_ID}`,
      }),
    );
    rs.stubGlobal("fetch", fetcher);

    await expect(
      fetchThreadContextAuthority(THREAD_ID, {
        apiBaseURL: API_BASE_URL,
        signal: controller.signal,
      }),
    ).resolves.toEqual({
      thread_id: THREAD_ID,
      cache_marker: `active:${ACTIVE_RUN_ID}`,
    });
    expect(fetcher).toHaveBeenCalledWith(
      `${API_BASE_URL}/threads/${THREAD_ID}/context-usage/authority`,
      expect.objectContaining({ method: "GET", signal: controller.signal }),
    );
    expect(threadContextAuthorityQueryKey(THREAD_ID)).toEqual([
      "thread-context-usage",
      THREAD_ID,
      "authority",
    ]);
    expect(CONTEXT_AUTHORITY_REFETCH_INTERVAL_MS).toBe(5_000);
  });

  test("binds the expensive reading identity to the authority marker", () => {
    expect(
      threadContextUsageReadingQueryKey(
        THREAD_ID,
        MODEL_NAME,
        `active:${ACTIVE_RUN_ID}`,
      ),
    ).toEqual([
      "thread-context-usage",
      THREAD_ID,
      MODEL_NAME,
      `active:${ACTIVE_RUN_ID}`,
    ]);
  });

  test("includes the composer-selected model in both request and query identity", async () => {
    const body = fractionUsage();
    const fetcher = rs.fn(async () => Response.json(body));
    rs.stubGlobal("fetch", fetcher);

    await fetchThreadContextUsage(THREAD_ID, {
      apiBaseURL: API_BASE_URL,
      modelName: MODEL_NAME,
    });

    expect(fetcher).toHaveBeenCalledWith(
      `${API_BASE_URL}/threads/${THREAD_ID}/context-usage?model_name=${MODEL_NAME}`,
      expect.objectContaining({ method: "GET" }),
    );
    expect(threadContextUsageQueryKey(THREAD_ID, MODEL_NAME)).toEqual([
      "thread-context-usage",
      THREAD_ID,
      MODEL_NAME,
    ]);
  });

  test("cancels and invalidates the scoped reading when a run stops", async () => {
    const cancelQueries = rs.fn(() => Promise.resolve());
    const invalidateQueries = rs.fn(() => Promise.resolve());
    const queryClient = {
      cancelQueries,
      invalidateQueries,
    } as unknown as QueryClient;

    invalidateStoppedThreadCaches(queryClient, THREAD_ID, false, {
      accountId: ACCOUNT_ID,
      projectId: PROJECT_ID,
    });
    await Promise.resolve();

    expect(cancelQueries).toHaveBeenCalledWith({
      queryKey: [
        "account",
        ACCOUNT_ID,
        "project",
        PROJECT_ID,
        "private-work",
        "thread-context-usage",
        THREAD_ID,
      ],
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: [
        "account",
        ACCOUNT_ID,
        "project",
        PROJECT_ID,
        "private-work",
        "thread-context-usage",
        THREAD_ID,
      ],
    });
  });

  test("refetches every selected-model reading for the exact Thread when a Run starts", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const scope = { accountId: ACCOUNT_ID, projectId: PROJECT_ID };
    const selectedModelKey = [
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      ...threadContextUsageReadingQueryKey(
        THREAD_ID,
        MODEL_NAME,
        `active:${ACTIVE_RUN_ID}`,
      ),
    ] as const;
    const otherModelKey = [
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      ...threadContextUsageReadingQueryKey(
        THREAD_ID,
        "55555555-5555-4555-8555-555555555555",
        `active:${ACTIVE_RUN_ID}`,
      ),
    ] as const;
    const otherThreadKey = [
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      ...threadContextUsageReadingQueryKey(
        "thread-other",
        MODEL_NAME,
        `active:${ACTIVE_RUN_ID}`,
      ),
    ] as const;
    const otherProjectKey = [
      "account",
      ACCOUNT_ID,
      "project",
      "22222222-2222-4222-8222-222222222223",
      "private-work",
      ...threadContextUsageReadingQueryKey(
        THREAD_ID,
        MODEL_NAME,
        `active:${ACTIVE_RUN_ID}`,
      ),
    ] as const;
    const otherAccountKey = [
      "account",
      "11111111-1111-4111-8111-111111111112",
      "project",
      PROJECT_ID,
      "private-work",
      ...threadContextUsageReadingQueryKey(
        THREAD_ID,
        MODEL_NAME,
        `active:${ACTIVE_RUN_ID}`,
      ),
    ] as const;
    queryClient.setQueryData(selectedModelKey, { estimated_tokens: 10 });
    queryClient.setQueryData(otherModelKey, { estimated_tokens: 11 });
    queryClient.setQueryData(otherThreadKey, { estimated_tokens: 12 });
    queryClient.setQueryData(otherProjectKey, { estimated_tokens: 13 });
    queryClient.setQueryData(otherAccountKey, { estimated_tokens: 14 });
    const refetch = rs.fn(async () => ({ estimated_tokens: 20 }));
    const observer = new QueryObserver(queryClient, {
      queryKey: selectedModelKey,
      queryFn: refetch,
      staleTime: Number.POSITIVE_INFINITY,
    });
    const unsubscribe = observer.subscribe(() => undefined);

    await invalidateStartedThreadContextUsage(
      queryClient,
      THREAD_ID,
      false,
      scope,
    );

    expect(refetch).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryState(otherModelKey)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(otherThreadKey)?.isInvalidated).toBe(
      false,
    );
    expect(queryClient.getQueryState(otherProjectKey)?.isInvalidated).toBe(
      false,
    );
    expect(queryClient.getQueryState(otherAccountKey)?.isInvalidated).toBe(
      false,
    );

    unsubscribe();
    queryClient.clear();
  });

  test("cancels a pending composer-model read before resolving active-Run authority", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const scope = { accountId: ACCOUNT_ID, projectId: PROJECT_ID };
    const queryKey = [
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      ...threadContextUsageReadingQueryKey(
        THREAD_ID,
        MODEL_NAME,
        `active:${ACTIVE_RUN_ID}`,
      ),
    ] as const;
    let resolveComposerRead!: (value: { estimated_tokens: number }) => void;
    let composerSignal: AbortSignal | undefined;
    let callCount = 0;
    const queryFn = rs.fn(({ signal }: { signal: AbortSignal }) => {
      callCount += 1;
      if (callCount === 1) {
        composerSignal = signal;
        return new Promise<{ estimated_tokens: number }>((resolve) => {
          resolveComposerRead = resolve;
        });
      }
      return Promise.resolve({ estimated_tokens: 20 });
    });
    const observer = new QueryObserver(queryClient, {
      queryKey,
      queryFn,
      staleTime: Number.POSITIVE_INFINITY,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    await Promise.resolve();

    const refresh = invalidateStartedThreadContextUsage(
      queryClient,
      THREAD_ID,
      false,
      scope,
    );
    await Promise.resolve();
    resolveComposerRead({ estimated_tokens: 10 });
    await refresh;
    await Promise.resolve();

    expect(composerSignal?.aborted).toBe(true);
    expect(queryFn).toHaveBeenCalledTimes(2);
    expect(queryClient.getQueryData(queryKey)).toEqual({
      estimated_tokens: 20,
    });

    unsubscribe();
    queryClient.clear();
  });

  test("cancels a pending active-Run read before a terminal or error refresh", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const scope = { accountId: ACCOUNT_ID, projectId: PROJECT_ID };
    const queryKey = [
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      ...threadContextUsageReadingQueryKey(
        THREAD_ID,
        MODEL_NAME,
        `active:${ACTIVE_RUN_ID}`,
      ),
    ] as const;
    let resolveActiveRead!: (value: { estimated_tokens: number }) => void;
    let activeSignal: AbortSignal | undefined;
    let callCount = 0;
    const queryFn = rs.fn(({ signal }: { signal: AbortSignal }) => {
      callCount += 1;
      if (callCount === 1) {
        activeSignal = signal;
        return new Promise<{ estimated_tokens: number }>((resolve) => {
          resolveActiveRead = resolve;
        });
      }
      return Promise.resolve({ estimated_tokens: 20 });
    });
    const observer = new QueryObserver(queryClient, {
      queryKey,
      queryFn,
      staleTime: Number.POSITIVE_INFINITY,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    await Promise.resolve();

    invalidateStoppedThreadCaches(queryClient, THREAD_ID, false, scope);
    await Promise.resolve();
    resolveActiveRead({ estimated_tokens: 10 });
    for (
      let index = 0;
      index < 10 && queryFn.mock.calls.length < 2;
      index += 1
    ) {
      await Promise.resolve();
    }
    for (
      let index = 0;
      index < 10 && queryClient.getQueryData(queryKey) === undefined;
      index += 1
    ) {
      await Promise.resolve();
    }

    expect(activeSignal?.aborted).toBe(true);
    expect(queryFn).toHaveBeenCalledTimes(2);
    expect(queryClient.getQueryData(queryKey)).toEqual({
      estimated_tokens: 20,
    });

    unsubscribe();
    queryClient.clear();
  });

  test("stop cancels pending context usage before invalidating it", async () => {
    const cancelQueries = rs.fn(() => Promise.resolve());
    const invalidateQueries = rs.fn(() => Promise.resolve());
    const queryClient = {
      cancelQueries,
      invalidateQueries,
    } as unknown as QueryClient;
    rs.stubGlobal(
      "setTimeout",
      rs.fn(() => 0),
    );

    await stopThreadAndInvalidateCaches(
      queryClient,
      () => Promise.resolve(),
      THREAD_ID,
      false,
      { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
    );

    expect(cancelQueries).toHaveBeenCalledWith({
      queryKey: [
        "account",
        ACCOUNT_ID,
        "project",
        PROJECT_ID,
        "private-work",
        "thread-context-usage",
        THREAD_ID,
      ],
    });
  });

  test("classifies an observed history Run only when it belongs to the exact Thread", () => {
    const activeRun = {
      run_id: "run-active",
      thread_id: THREAD_ID,
      status: "pending",
    };
    const terminalRun = {
      ...activeRun,
      status: "success",
    };

    expect(
      latestContextUsageRunObservation(THREAD_ID, [activeRun] as never),
    ).toEqual({ runId: "run-active", authority: "active" });
    expect(
      latestContextUsageRunObservation(THREAD_ID, [terminalRun] as never),
    ).toEqual({ runId: "run-active", authority: "idle" });
    expect(
      latestContextUsageRunObservation("thread-other", [activeRun] as never),
    ).toBeNull();
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
