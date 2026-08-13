import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

rs.mock("react", () => ({
  useCallback: <T extends (...args: never[]) => unknown>(callback: T) =>
    callback,
}));
rs.mock("@tanstack/react-query", () => ({
  useMutation: rs.fn(),
  useQuery: rs.fn(),
  useQueryClient: rs.fn(),
}));
rs.mock("@/core/private-work/memory/api", () => ({
  admitProjectMemoryDreamPreparation: rs.fn(),
  cancelProjectMemoryDreamPreparation: rs.fn(),
  getLatestProjectMemoryDreamPreparation: rs.fn(),
}));

import { GatewayApiError } from "@/core/api/errors";
import {
  admitProjectMemoryDreamPreparation,
  cancelProjectMemoryDreamPreparation,
  getLatestProjectMemoryDreamPreparation,
} from "@/core/private-work/memory/api";
import {
  MEMORY_DREAM_PREPARATION_POLL_INTERVAL_MS,
  memoryDreamPreparationIsActive,
  useMemoryDreamPreparation,
} from "@/core/private-work/memory/preparation-hooks";
import type { MemoryDreamPreparationStatus } from "@/core/private-work/memory/types";
import type { PrivateWorkAccess } from "@/core/private-work/types";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const JOB_ID = "33333333-3333-4333-8333-333333333333";
const OPERATION_ID = "44444444-4444-4444-8444-444444444444";

const runningStatus: MemoryDreamPreparationStatus = {
  jobId: JOB_ID,
  status: "running",
  phase: "draining",
  compactedPasses: 2,
  dreamJobId: null,
  historyCount: null,
  admissionKind: null,
  resultDisposition: "queued",
  cancelRequested: false,
  publicErrorCode: null,
  updatedAt: "2026-08-13T00:00:00Z",
};

type QueryOptions = {
  queryKey: readonly unknown[];
  queryFn: (context: { signal: AbortSignal }) => Promise<unknown>;
  enabled: boolean;
  refetchInterval: (query: {
    state: { data: MemoryDreamPreparationStatus | null | undefined };
  }) => number | false;
};

type MutationOptions = {
  mutationKey: readonly unknown[];
  mutationFn: (value: string) => Promise<unknown>;
  onSuccess: (value: MemoryDreamPreparationStatus) => Promise<void> | void;
};

const mockedUseQuery = rs.mocked(useQuery);
const mockedUseMutation = rs.mocked(useMutation);
const mockedUseQueryClient = rs.mocked(useQueryClient);
const mockedGetLatest = rs.mocked(getLatestProjectMemoryDreamPreparation);
const mockedAdmit = rs.mocked(admitProjectMemoryDreamPreparation);
const mockedCancel = rs.mocked(cancelProjectMemoryDreamPreparation);

function createAccess() {
  const abortController = new AbortController();
  const runAbortable = rs.fn(
    (operation: (signal: AbortSignal) => Promise<unknown>) =>
      operation(abortController.signal),
  );
  return {
    access: {
      apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
      scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
      runAbortable,
    } as unknown as PrivateWorkAccess,
    abortController,
    runAbortable,
  };
}

function arrangeHooks(data: MemoryDreamPreparationStatus | null = null) {
  const refetch = rs.fn(async () => ({ data }));
  const setQueryData = rs.fn();
  const mutations: MutationOptions[] = [];
  mockedUseQuery.mockReturnValue({
    data,
    isLoading: false,
    refetch,
  } as never);
  mockedUseQueryClient.mockReturnValue({ setQueryData } as never);
  mockedUseMutation.mockImplementation((options) => {
    mutations.push(options as unknown as MutationOptions);
    return {
      isPending: false,
      mutateAsync: rs.fn(),
    } as never;
  });
  return { mutations, refetch, setQueryData };
}

beforeEach(() => {
  rs.clearAllMocks();
});

describe("Memory Dream preparation hooks", () => {
  test("keeps the no-thread recovery query disabled without constructing an invalid key", () => {
    const { access } = createAccess();
    arrangeHooks();

    expect(() =>
      useMemoryDreamPreparation({
        privateWork: access,
        threadId: "",
        enabled: false,
      }),
    ).not.toThrow();

    const options = mockedUseQuery.mock.calls[0]![0] as unknown as QueryOptions;
    expect(options.enabled).toBe(false);
    expect(options.queryKey).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      "memory",
      "dream-preparation",
      "disabled",
    ]);
  });

  test("recovers latest status, treats 404 as empty, and polls only active jobs", async () => {
    const { access } = createAccess();
    arrangeHooks();
    useMemoryDreamPreparation({
      privateWork: access,
      threadId: "thread-1",
      enabled: true,
    });

    const options = mockedUseQuery.mock.calls[0]![0] as unknown as QueryOptions;
    const signal = new AbortController().signal;
    mockedGetLatest.mockResolvedValueOnce(runningStatus);
    await expect(options.queryFn({ signal })).resolves.toBe(runningStatus);
    expect(mockedGetLatest).toHaveBeenCalledWith(access, "thread-1", signal);

    mockedGetLatest.mockRejectedValueOnce(
      new GatewayApiError(404, "MEMORY_DREAM_PREPARE_NOT_FOUND", "Not found"),
    );
    await expect(options.queryFn({ signal })).resolves.toBeNull();

    expect(options.refetchInterval({ state: { data: runningStatus } })).toBe(
      MEMORY_DREAM_PREPARATION_POLL_INTERVAL_MS,
    );
    expect(
      options.refetchInterval({
        state: { data: { ...runningStatus, status: "succeeded" } },
      }),
    ).toBe(false);
    expect(options.refetchInterval({ state: { data: null } })).toBe(false);
    expect(memoryDreamPreparationIsActive(runningStatus)).toBe(true);
    expect(
      memoryDreamPreparationIsActive({
        ...runningStatus,
        status: "cancelled",
      }),
    ).toBe(false);
  });

  test("admits and cancels through the scoped abort lifecycle with one recovery read", async () => {
    const { access, abortController, runAbortable } = createAccess();
    const { mutations, refetch, setQueryData } = arrangeHooks(runningStatus);
    useMemoryDreamPreparation({
      privateWork: access,
      threadId: "thread-1",
      enabled: true,
    });

    const [startOptions, cancelOptions] = mutations;
    expect(startOptions?.mutationKey).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      "memory",
      "mutation",
      "dream-prepare",
    ]);
    expect(cancelOptions?.mutationKey).toEqual([
      "account",
      ACCOUNT_ID,
      "project",
      PROJECT_ID,
      "private-work",
      "memory",
      "mutation",
      "dream-prepare-cancel",
    ]);

    mockedAdmit.mockResolvedValueOnce({
      disposition: "queued",
      jobId: JOB_ID,
      status: "queued",
    });
    await expect(startOptions!.mutationFn(OPERATION_ID)).resolves.toMatchObject(
      {
        jobId: JOB_ID,
      },
    );
    expect(mockedAdmit).toHaveBeenCalledWith(
      access,
      { threadId: "thread-1", operationId: OPERATION_ID },
      abortController.signal,
    );
    await startOptions!.onSuccess(runningStatus);
    expect(refetch).toHaveBeenCalledTimes(1);

    const cancellation = { ...runningStatus, cancelRequested: true };
    mockedCancel.mockResolvedValueOnce(cancellation);
    await expect(cancelOptions!.mutationFn(JOB_ID)).resolves.toBe(cancellation);
    expect(mockedCancel).toHaveBeenCalledWith(
      access,
      JOB_ID,
      abortController.signal,
    );
    await cancelOptions!.onSuccess(cancellation);
    expect(setQueryData).toHaveBeenCalledWith(
      [
        "account",
        ACCOUNT_ID,
        "project",
        PROJECT_ID,
        "private-work",
        "memory",
        "dream-preparation",
        "latest",
        "thread-1",
      ],
      cancellation,
    );
    expect(runAbortable).toHaveBeenCalledTimes(2);
  });
});
