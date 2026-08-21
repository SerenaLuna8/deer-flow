import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn(),
  useQuery: rs.fn(),
  useQueryClient: rs.fn(),
}));
rs.mock("react", () => ({
  useEffect: rs.fn(),
  useMemo: rs.fn(),
  useRef: rs.fn(),
  useState: rs.fn(),
}));
rs.mock("@/core/private-work/provider", () => ({
  usePrivateWorkAccess: rs.fn(),
}));
rs.mock("@/core/skill-builder/api", () => ({
  cancelSkillBuilderSession: rs.fn(),
  commitSkillBuilderSession: rs.fn(),
  createSkillBuilderRevisionSession: rs.fn(),
  createSkillBuilderSession: rs.fn(),
  getSkillBuilderSession: rs.fn(),
  listSkillBuilderActivities: rs.fn(),
  listSkillBuilderSessions: rs.fn(),
  parseSkillBuilderActivity: rs.fn((value) => value),
  skillBuilderActivityStreamURL: rs.fn(() => "/skill-builder/activities"),
  submitSkillBuilderTurn: rs.fn(),
  validateSkillBuilderSession: rs.fn(),
}));

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { PrivateWorkAccess } from "@/core/private-work/types";
import {
  mergeSkillBuilderActivities,
  skillBuilderSessionKey,
  skillBuilderSessionsInvalidation,
  useCreateSkillBuilderRevisionSession,
  useSkillBuilderActivities,
  useSkillBuilderRunStream,
} from "@/core/skill-builder";
import { listSkillBuilderActivities } from "@/core/skill-builder/api";
import type {
  SkillBuilderActivity,
  SkillBuilderSession,
} from "@/core/skill-builder/types";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_ID = "33333333-3333-4333-8333-333333333333";
const NOW = "2026-08-14T08:00:00+08:00";

type MutationOptions = {
  mutationKey: readonly unknown[];
  mutationFn: (input: unknown) => Promise<unknown>;
  onSuccess: (response: { data: SkillBuilderSession }) => void;
};

const mockedUseMutation = rs.mocked(useMutation);
const mockedUseQuery = rs.mocked(useQuery);
const mockedUseQueryClient = rs.mocked(useQueryClient);
const mockedUsePrivateWorkAccess = rs.mocked(usePrivateWorkAccess);
const mockedUseEffect = rs.mocked(useEffect);
const mockedUseMemo = rs.mocked(useMemo);
const mockedUseRef = rs.mocked(useRef);
const mockedUseState = rs.mocked(useState);
const mockedListSkillBuilderActivities = rs.mocked(listSkillBuilderActivities);

type EffectRecord = {
  cleanup?: () => void;
  dependencies?: readonly unknown[];
};

function createHookLifecycleHarness() {
  const states: unknown[] = [];
  const refs: Array<{ current: unknown }> = [];
  const effects: EffectRecord[] = [];
  let stateCursor = 0;
  let refCursor = 0;
  let effectCursor = 0;

  const stateMock = mockedUseState as unknown as {
    mockImplementation: (
      implementation: (
        initialValue?: unknown,
      ) => [unknown, (next: unknown) => void],
    ) => void;
  };
  stateMock.mockImplementation((initialValue) => {
    const index = stateCursor++;
    if (!(index in states)) {
      states[index] =
        typeof initialValue === "function" ? initialValue() : initialValue;
    }
    const setState = (next: unknown) => {
      states[index] =
        typeof next === "function"
          ? (next as (current: unknown) => unknown)(states[index])
          : next;
    };
    return [states[index], setState];
  });
  mockedUseRef.mockImplementation((initialValue) => {
    const index = refCursor++;
    refs[index] ??= { current: initialValue };
    return refs[index] as never;
  });
  mockedUseEffect.mockImplementation((effect, dependencies) => {
    const index = effectCursor++;
    const previous = effects[index];
    const unchanged =
      dependencies?.length === previous?.dependencies?.length &&
      dependencies?.every((value, offset) =>
        Object.is(value, previous?.dependencies?.[offset]),
      ) === true;
    if (unchanged) return;
    previous?.cleanup?.();
    const cleanup = effect();
    effects[index] = {
      cleanup: typeof cleanup === "function" ? cleanup : undefined,
      dependencies,
    };
  });

  return {
    render<T>(hook: () => T): T {
      stateCursor = 0;
      refCursor = 0;
      effectCursor = 0;
      return hook();
    },
    unmount() {
      effects.forEach((effect) => effect.cleanup?.());
      effects.length = 0;
    },
  };
}

function reviseSession(): SkillBuilderSession {
  return {
    id: SESSION_ID,
    project_id: PROJECT_ID,
    owner_user_id: "owner-1",
    thread_id: "44444444-4444-4444-8444-444444444444",
    slug: "catalog-auditor",
    display_name: "catalog-auditor",
    status: "draft_ready",
    revision: 1,
    messages: [],
    active_clarification: null,
    progress: [],
    files: [],
    draft_checksum: "b".repeat(64),
    validation: null,
    error_code: null,
    error_message: null,
    created_skill_id: null,
    session_kind: "revise",
    target_skill_id: "55555555-5555-4555-8555-555555555555",
    base_version_id: "66666666-6666-4666-8666-666666666666",
    base_version_number: 3,
    base_payload_checksum: "b".repeat(64),
    target_skill_deleted: false,
    base_files: [],
    created_at: NOW,
    updated_at: NOW,
  };
}

beforeEach(() => {
  rs.clearAllMocks();
  mockedUseQuery.mockReturnValue({ data: undefined } as never);
  mockedUseMemo.mockImplementation((factory) => factory() as never);
  mockedUsePrivateWorkAccess.mockReturnValue({
    apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
    scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
    runAbortable: (operation: (signal: AbortSignal) => Promise<unknown>) =>
      operation(new AbortController().signal),
    isActive: () => true,
  } as unknown as PrivateWorkAccess);
});

describe("useSkillBuilderActivities", () => {
  const activity = (seq: string, text: string): SkillBuilderActivity => ({
    seq,
    operation_id: "55555555-5555-4555-8555-555555555555",
    run_id: "66666666-6666-4666-8666-666666666666",
    kind: "reasoning",
    attempt: 1,
    payload: { text },
    created_at: NOW,
  });

  test("keeps newer SSE cache entries when a stale REST replay finishes", async () => {
    const cached = [activity("1", "first"), activity("3", "live")];
    const replay = [activity("1", "first"), activity("2", "replay")];
    const subscribeEventStream = rs.fn(() => rs.fn());
    const queryClient = {
      getQueryData: rs.fn(() => cached),
      setQueryData: rs.fn(),
    };
    let queryOptions:
      | {
          queryFn: (input: { signal: AbortSignal }) => Promise<unknown>;
          refetchOnReconnect: boolean;
          refetchOnWindowFocus: boolean;
        }
      | undefined;
    mockedUseQueryClient.mockReturnValue(queryClient as never);
    mockedUseQuery.mockImplementation((options) => {
      queryOptions = options as unknown as typeof queryOptions;
      return { data: cached, isSuccess: true } as never;
    });
    mockedUseEffect.mockImplementation((effect) => {
      effect();
    });
    mockedUsePrivateWorkAccess.mockReturnValue({
      apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
      scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
      subscribeEventStream,
      runAbortable: (operation: (signal: AbortSignal) => Promise<unknown>) =>
        operation(new AbortController().signal),
      isActive: () => true,
    } as unknown as PrivateWorkAccess);
    mockedListSkillBuilderActivities.mockResolvedValue({
      data: replay,
      request_id: "request-replay",
    });

    useSkillBuilderActivities(ACCOUNT_ID, PROJECT_ID, SESSION_ID);
    const result = await queryOptions?.queryFn({
      signal: new AbortController().signal,
    });

    expect(result).toEqual(cached);
    expect(queryOptions?.refetchOnReconnect).toBe(false);
    expect(queryOptions?.refetchOnWindowFocus).toBe(false);
    expect(subscribeEventStream).toHaveBeenCalledTimes(1);
  });

  test("drops duplicate and non-increasing Activity frames", () => {
    const current = activity("2", "已回放");

    expect(
      mergeSkillBuilderActivities(
        [current],
        [
          activity("2", "重复帧"),
          activity("1", "倒退帧"),
          activity("3", "实时增量"),
        ],
      ).map((item) => [item.seq, item.payload]),
    ).toEqual([
      ["2", { text: "已回放" }],
      ["3", { text: "实时增量" }],
    ]);
  });
});

describe("useSkillBuilderRunStream", () => {
  test("keeps one subscription when only initial status changes and still aborts lifecycle boundaries", async () => {
    const harness = createHookLifecycleHarness();
    const openedSignals: AbortSignal[] = [];
    const joinStream = rs.fn(
      (_threadId: string, _runId: string, options: { signal: AbortSignal }) => {
        openedSignals.push(options.signal);
        return (async function* () {
          await new Promise<void>((resolve) => {
            if (options.signal.aborted) {
              resolve();
              return;
            }
            options.signal.addEventListener("abort", () => resolve(), {
              once: true,
            });
          });
        })();
      },
    );
    mockedUsePrivateWorkAccess.mockReturnValue({
      apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
      scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
      client: { runs: { joinStream } },
      runAbortable: (operation: (signal: AbortSignal) => Promise<unknown>) =>
        operation(new AbortController().signal),
      isActive: () => true,
    } as unknown as PrivateWorkAccess);

    const threadId = "44444444-4444-4444-8444-444444444444";
    const firstRunId = "55555555-5555-4555-8555-555555555555";
    const secondRunId = "66666666-6666-4666-8666-666666666666";
    const render = (
      runId: string,
      initialStatus: "pending" | "running",
      enabled = true,
    ) =>
      harness.render(() =>
        useSkillBuilderRunStream({
          threadId,
          runId,
          initialStatus,
          enabled,
        }),
      );

    render(firstRunId, "pending");
    await Promise.resolve();
    render(firstRunId, "running");
    await Promise.resolve();

    expect(joinStream).toHaveBeenCalledTimes(1);
    expect(openedSignals[0]?.aborted).toBe(false);

    render(secondRunId, "running");
    await Promise.resolve();
    expect(joinStream).toHaveBeenCalledTimes(2);
    expect(openedSignals[0]?.aborted).toBe(true);
    expect(openedSignals[1]?.aborted).toBe(false);

    render(secondRunId, "running", false);
    await Promise.resolve();
    expect(joinStream).toHaveBeenCalledTimes(2);
    expect(openedSignals[1]?.aborted).toBe(true);

    render(secondRunId, "running", true);
    await Promise.resolve();
    expect(joinStream).toHaveBeenCalledTimes(3);
    expect(openedSignals[2]?.aborted).toBe(false);

    harness.unmount();
    expect(openedSignals[2]?.aborted).toBe(true);
  });
});

describe("useCreateSkillBuilderRevisionSession", () => {
  test("writes the revise session into the session cache", () => {
    const setQueryData = rs.fn();
    const invalidateQueries = rs.fn(async () => undefined);
    const mutations: MutationOptions[] = [];
    mockedUseQueryClient.mockReturnValue({
      setQueryData,
      invalidateQueries,
    } as never);
    mockedUseMutation.mockImplementation((options) => {
      mutations.push(options as unknown as MutationOptions);
      return { isPending: false, mutate: rs.fn() } as never;
    });

    useCreateSkillBuilderRevisionSession(ACCOUNT_ID, PROJECT_ID);

    const created = reviseSession();
    mutations[0]?.onSuccess({ data: created });

    expect(mutations[0]?.mutationKey).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "skill-builder",
      "mutation",
      "create-revision-session",
    ]);
    expect(setQueryData).toHaveBeenCalledWith(
      skillBuilderSessionKey(ACCOUNT_ID, PROJECT_ID, SESSION_ID),
      created,
    );
    expect(invalidateQueries).toHaveBeenCalledWith(
      skillBuilderSessionsInvalidation(ACCOUNT_ID, PROJECT_ID),
    );
  });
});
