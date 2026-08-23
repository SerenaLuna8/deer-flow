import {
  afterEach,
  beforeEach,
  describe,
  expect,
  rs,
  test,
} from "@rstest/core";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

type TestEffect = () => void | (() => void);

const refs: Array<{ current: unknown }> = [];
const stateValues: unknown[] = [];
let pendingEffects: TestEffect[] = [];
let refCursor = 0;
let stateCursor = 0;

rs.mock("react", () => ({
  useCallback: <T extends (...args: never[]) => unknown>(callback: T) =>
    callback,
  useEffect: (effect: TestEffect) => {
    pendingEffects.push(effect);
  },
  useMemo: <T>(factory: () => T) => factory(),
  useRef: <T>(initialValue: T) => {
    const index = refCursor;
    refCursor += 1;
    refs[index] ??= { current: initialValue };
    return refs[index] as { current: T };
  },
  useState: <T>(initialValue: T | (() => T)) => {
    const index = stateCursor;
    stateCursor += 1;
    if (stateValues.length <= index) {
      stateValues[index] =
        typeof initialValue === "function"
          ? (initialValue as () => T)()
          : initialValue;
    }
    const setValue = (nextValue: T | ((current: T) => T)) => {
      const current = stateValues[index] as T;
      stateValues[index] =
        typeof nextValue === "function"
          ? (nextValue as (value: T) => T)(current)
          : nextValue;
    };
    return [stateValues[index] as T, setValue] as const;
  },
}));
rs.mock("@tanstack/react-query", () => ({
  useQueryClient: rs.fn(),
}));
rs.mock("next/navigation", () => ({
  useRouter: rs.fn(),
}));
rs.mock("sonner", () => ({
  toast: {
    error: rs.fn(),
    info: rs.fn(),
    success: rs.fn(),
  },
}));
rs.mock("@/core/i18n/hooks", () => ({
  useI18n: () => ({
    t: {
      inputBox: {
        compactFailed: "compact failed",
        compactPromptBudgetTooSmall: "compact prompt budget too small",
        compactSkipped: "compact skipped",
        compactSourceTooLarge: "compact source too large",
        compactSuccess: "compact success",
        dreamAlreadyRunning: "dream already running",
        dreamFailed: "dream failed",
        dreamModelUnavailable: "dream model unavailable",
        dreamNothingPending: "nothing pending",
        dreamPreparationCancelled: "dream preparation cancelled",
        dreamPreparationCompleted: "dream preparation completed",
        dreamPreparationFailed: "dream preparation failed",
        dreamPreparationQueued: "dream preparation queued",
        dreamPreparationRunning: "dream preparation running",
        dreamPreparationStarted: "dream preparation started",
        dreamPreparationVerifying: "dream preparation verifying",
        dreamQueued: "dream queued: {count}",
        dreamRequiresThread: "dream requires thread",
        dreamRestoreFailed: "restore failed",
        dreamRestoreSuccess: "restored version {version}",
        dreamRouteUnavailable: "memory route unavailable",
        goalActive: "active goal: {goal}",
        goalCleared: "goal cleared",
        goalFailed: "goal failed",
        goalNone: "no goal",
        goalSet: "goal set",
      },
      projectMemory: {
        dreamQueuedBudget: "budget rewrite queued",
      },
    },
  }),
}));
rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
}));
rs.mock("@/core/private-work/memory/api", () => ({
  getProjectMemory: rs.fn(),
  restoreProjectMemoryVersion: rs.fn(),
}));
rs.mock("@/core/private-work/memory/preparation-hooks", () => ({
  useMemoryDreamPreparation: rs.fn(),
}));
rs.mock("@/core/private-work/memory-freshness", () => ({
  commitProjectMemoryCacheChange: rs.fn(),
}));
rs.mock("@/core/threads/api", () => ({
  compactThreadContext: rs.fn(),
}));

import {
  useInputBoxCommands as invokeInputBoxCommands,
  type UseInputBoxCommandsOptions,
} from "@/components/workspace/use-input-box-commands";
import { GatewayApiError } from "@/core/api/errors";
import { fetch as authenticatedFetch } from "@/core/api/fetcher";
import {
  getProjectMemory,
  restoreProjectMemoryVersion,
} from "@/core/private-work/memory/api";
import { useMemoryDreamPreparation } from "@/core/private-work/memory/preparation-hooks";
import { projectMemoryRootQueryKey } from "@/core/private-work/memory/query-keys";
import type { MemoryDreamPreparationStatus } from "@/core/private-work/memory/types";
import { commitProjectMemoryCacheChange } from "@/core/private-work/memory-freshness";
import { privateWorkQueryKey } from "@/core/private-work/query-keys";
import type { PrivateWorkAccess } from "@/core/private-work/types";
import type { GoalState } from "@/core/threads";
import { compactThreadContext } from "@/core/threads/api";
import { threadContextUsageQueryKey } from "@/core/threads/context-usage";
import { threadTokenUsageQueryKey } from "@/core/threads/token-usage";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "thread-a";
const API_BASE_URL = `/api/projects/${PROJECT_ID}/private-work`;
const MEMORY_ROUTE = "/projects/project-a/memory";

const privateWork = {
  apiBaseURL: API_BASE_URL,
  scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
} as unknown as PrivateWorkAccess;

const goal: GoalState = {
  objective: "Ship $& safely",
  status: "active",
  created_at: "2026-08-13T00:00:00Z",
  updated_at: "2026-08-13T00:00:00Z",
  continuation_count: 0,
  max_continuations: 4,
  no_progress_count: 0,
  max_no_progress_continuations: 2,
};

const terminalPreparation: MemoryDreamPreparationStatus = {
  jobId: "33333333-3333-4333-8333-333333333333",
  status: "succeeded",
  phase: "succeeded",
  compactedPasses: 2,
  dreamJobId: "44444444-4444-4444-8444-444444444444",
  historyCount: 3,
  admissionKind: "history",
  resultDisposition: "queued",
  cancelRequested: false,
  publicErrorCode: null,
  updatedAt: "2026-08-13T00:00:00Z",
};

const mockedUseQueryClient = rs.mocked(useQueryClient);
const mockedUseRouter = rs.mocked(useRouter);
const mockedFetch = rs.mocked(authenticatedFetch);
const mockedGetProjectMemory = rs.mocked(getProjectMemory);
const mockedRestoreProjectMemoryVersion = rs.mocked(
  restoreProjectMemoryVersion,
);
const mockedUseMemoryDreamPreparation = rs.mocked(useMemoryDreamPreparation);
const mockedCommitProjectMemoryCacheChange = rs.mocked(
  commitProjectMemoryCacheChange,
);
const mockedCompactThreadContext = rs.mocked(compactThreadContext);

const invalidateQueries = rs.fn(async () => undefined);
const routerPush = rs.fn();
const startDreamPreparation = rs.fn();
const cancelDreamPreparation = rs.fn(async () => undefined);
let preparation: MemoryDreamPreparationStatus | null = null;

function beginRender() {
  refCursor = 0;
  stateCursor = 0;
  pendingEffects = [];
}

function flushEffects() {
  const effects = pendingEffects;
  pendingEffects = [];
  return effects.flatMap((effect) => {
    const cleanup = effect();
    return typeof cleanup === "function" ? [cleanup] : [];
  });
}

function createOptions(
  overrides: Partial<UseInputBoxCommandsOptions> = {},
): UseInputBoxCommandsOptions {
  return {
    clearMemoryCommandInput: rs.fn(),
    compactCommandEnabled: true,
    isMock: false,
    markLatestCheckpoint: rs.fn(),
    memoryRoutePath: MEMORY_ROUTE,
    onGoalChange: rs.fn(),
    privateWork,
    threadExists: true,
    threadId: THREAD_ID,
    ...overrides,
  };
}

function renderCommands(options = createOptions()) {
  beginRender();
  const result = invokeInputBoxCommands(options);
  const cleanups = flushEffects();
  return { cleanups, options, result };
}

function response(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: rs.fn(async () => body),
  } as unknown as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

beforeEach(() => {
  refs.length = 0;
  stateValues.length = 0;
  pendingEffects = [];
  refCursor = 0;
  stateCursor = 0;
  preparation = null;
  rs.clearAllMocks();
  mockedUseQueryClient.mockReturnValue({
    invalidateQueries,
  } as never);
  mockedUseRouter.mockReturnValue({ push: routerPush } as never);
  mockedUseMemoryDreamPreparation.mockImplementation(
    () =>
      ({
        preparation,
        recovering: false,
        starting: false,
        cancelling: false,
        start: startDreamPreparation,
        cancel: cancelDreamPreparation,
      }) as never,
  );
  mockedCommitProjectMemoryCacheChange.mockResolvedValue(undefined);
});

afterEach(() => {
  for (const effect of pendingEffects) {
    effect();
  }
  pendingEffects = [];
});

describe("useInputBoxCommands", () => {
  test("preserves goal status, clear, and set HTTP contracts", async () => {
    const options = createOptions();
    const { result } = renderCommands(options);
    mockedFetch
      .mockResolvedValueOnce(response({ goal }))
      .mockResolvedValueOnce(response({}))
      .mockResolvedValueOnce(response({ goal }));

    await expect(result.handleGoalCommand({ kind: "status" })).resolves.toBe(
      true,
    );
    await expect(result.handleGoalCommand({ kind: "clear" })).resolves.toBe(
      true,
    );
    await expect(
      result.handleGoalCommand({ kind: "set", objective: goal.objective }),
    ).resolves.toBe(true);

    expect(mockedFetch).toHaveBeenNthCalledWith(
      1,
      `${API_BASE_URL}/threads/${THREAD_ID}/goal`,
      {
        method: "GET",
        signal: expect.any(AbortSignal),
      },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      2,
      `${API_BASE_URL}/threads/${THREAD_ID}/goal`,
      {
        method: "DELETE",
        signal: expect.any(AbortSignal),
      },
    );
    expect(mockedFetch).toHaveBeenNthCalledWith(
      3,
      `${API_BASE_URL}/threads/${THREAD_ID}/goal`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ objective: goal.objective }),
        signal: expect.any(AbortSignal),
      },
    );
    expect(toast.info).toHaveBeenCalledWith("active goal: Ship $& safely");
    expect(toast.success).toHaveBeenNthCalledWith(1, "goal cleared");
    expect(toast.success).toHaveBeenNthCalledWith(2, "goal set");
    expect(options.onGoalChange).toHaveBeenNthCalledWith(1, goal);
    expect(options.onGoalChange).toHaveBeenNthCalledWith(2, null);
    expect(options.onGoalChange).toHaveBeenNthCalledWith(3, goal);
  });

  test("keeps compact clear/mark order and cache invalidation set", async () => {
    const order: string[] = [];
    const options = createOptions({
      clearMemoryCommandInput: rs.fn(() => {
        order.push("clear");
      }),
      markLatestCheckpoint: rs.fn(() => {
        order.push("mark");
      }),
    });
    mockedCompactThreadContext.mockResolvedValueOnce({
      compacted: true,
    } as never);
    const { result } = renderCommands(options);

    await result.handleCompactCommand();

    expect(mockedCompactThreadContext).toHaveBeenCalledWith(THREAD_ID, {
      apiBaseURL: API_BASE_URL,
      signal: expect.any(AbortSignal),
    });
    expect(order).toEqual(["mark", "clear"]);
    expect(toast.success).toHaveBeenCalledWith("compact success");
    expect(invalidateQueries).toHaveBeenCalledTimes(3);
    expect(invalidateQueries).toHaveBeenNthCalledWith(1, {
      queryKey: privateWorkQueryKey(privateWork.scope, "thread", THREAD_ID),
    });
    expect(invalidateQueries).toHaveBeenNthCalledWith(2, {
      queryKey: privateWorkQueryKey(
        privateWork.scope,
        ...threadTokenUsageQueryKey(THREAD_ID),
      ),
    });
    expect(invalidateQueries).toHaveBeenNthCalledWith(3, {
      queryKey: privateWorkQueryKey(
        privateWork.scope,
        ...threadContextUsageQueryKey(THREAD_ID),
      ),
    });
    expect(mockedCommitProjectMemoryCacheChange).toHaveBeenCalledWith(
      expect.anything(),
      privateWork.scope,
      "pending",
    );
  });

  test("distinguishes permanent compact failures from a context that needs no work", async () => {
    mockedCompactThreadContext
      .mockResolvedValueOnce({
        compacted: false,
        reason: "source_too_large",
      } as never)
      .mockResolvedValueOnce({
        compacted: false,
        reason: "prompt_budget_too_small",
      } as never)
      .mockResolvedValueOnce({
        compacted: false,
        reason: "compaction_failed",
      } as never)
      .mockResolvedValueOnce({
        compacted: false,
        reason: "not_enough_messages",
      } as never);
    const { result } = renderCommands();

    await result.handleCompactCommand();
    await result.handleCompactCommand();
    await result.handleCompactCommand();
    await result.handleCompactCommand();

    expect(toast.error).toHaveBeenNthCalledWith(1, "compact source too large");
    expect(toast.error).toHaveBeenNthCalledWith(
      2,
      "compact prompt budget too small",
    );
    expect(toast.error).toHaveBeenNthCalledWith(3, "compact failed");
    expect(toast.info).toHaveBeenCalledWith("compact skipped");
  });

  test("starts Dream, routes logs, and projects terminal preparation state", async () => {
    preparation = terminalPreparation;
    startDreamPreparation.mockResolvedValueOnce({
      disposition: "queued",
    });
    const options = createOptions();
    const { result } = renderCommands(options);

    expect(result.dreamPreparation).toBe(terminalPreparation);
    expect(result.dreamPreparationLabel).toBe("dream preparation completed");
    expect(options.markLatestCheckpoint).toHaveBeenCalledTimes(1);
    expect(invalidateQueries).toHaveBeenCalledTimes(3);
    expect(mockedCommitProjectMemoryCacheChange).toHaveBeenNthCalledWith(
      1,
      expect.anything(),
      privateWork.scope,
      "pending",
    );
    expect(mockedCommitProjectMemoryCacheChange).toHaveBeenNthCalledWith(
      2,
      expect.anything(),
      privateWork.scope,
      "document",
    );
    expect(toast.success).toHaveBeenCalledWith("dream queued: 3");

    await result.handleDreamCommand();
    expect(startDreamPreparation).toHaveBeenCalledWith(expect.any(String));
    expect(options.clearMemoryCommandInput).toHaveBeenCalledTimes(1);
    expect(toast.success).toHaveBeenCalledWith("dream preparation started");

    result.handleDreamLogCommand(7);
    expect(options.clearMemoryCommandInput).toHaveBeenCalledTimes(2);
    expect(routerPush).toHaveBeenCalledWith(`${MEMORY_ROUTE}?version=7`);
    result.handleDreamLogCommand(null);
    expect(routerPush).toHaveBeenLastCalledWith(MEMORY_ROUTE);
  });

  test("shows the localized unavailable-model notice for failed Dream preparation", () => {
    preparation = {
      ...terminalPreparation,
      status: "failed",
      phase: "failed",
      dreamJobId: null,
      historyCount: null,
      admissionKind: null,
      resultDisposition: "failed",
      publicErrorCode: "MEMORY_DREAM_MODEL_UNAVAILABLE",
    };

    renderCommands();

    expect(toast.error).toHaveBeenCalledWith("dream model unavailable");
    expect(toast.error).not.toHaveBeenCalledWith("dream failed");
  });

  test.each([
    {
      publicErrorCode: "MEMORY_DREAM_PREPARE_SOURCE_TOO_LARGE",
      expected: "compact source too large",
    },
    {
      publicErrorCode: "MEMORY_DREAM_PREPARE_PROMPT_BUDGET_TOO_SMALL",
      expected: "compact prompt budget too small",
    },
  ])(
    "shows the localized permanent compaction notice for $publicErrorCode",
    ({ publicErrorCode, expected }) => {
      preparation = {
        ...terminalPreparation,
        status: "failed",
        phase: "failed",
        dreamJobId: null,
        historyCount: null,
        admissionKind: null,
        resultDisposition: "failed",
        publicErrorCode,
      };

      renderCommands();

      expect(toast.error).toHaveBeenCalledWith(expected);
      expect(toast.error).not.toHaveBeenCalledWith("dream failed");
    },
  );

  test("restores against the current version and routes only after freshness commit", async () => {
    const options = createOptions();
    let { result } = renderCommands(options);
    result.handleDreamRestoreCommand(7);
    expect(options.clearMemoryCommandInput).toHaveBeenCalledTimes(1);

    ({ result } = renderCommands(options));
    expect(result.pendingDreamRestoreVersion).toBe(7);
    mockedGetProjectMemory.mockResolvedValueOnce({ version: 9 } as never);
    mockedRestoreProjectMemoryVersion.mockResolvedValueOnce({
      version: 10,
    } as never);

    await result.confirmDreamRestore();

    expect(mockedGetProjectMemory).toHaveBeenCalledWith(
      privateWork,
      expect.any(AbortSignal),
    );
    const restoreSignal = mockedGetProjectMemory.mock.calls[0]![1]!;
    expect(mockedRestoreProjectMemoryVersion).toHaveBeenCalledWith(
      privateWork,
      7,
      { expectedCurrentVersion: 9 },
      restoreSignal,
    );
    expect(mockedCommitProjectMemoryCacheChange).toHaveBeenCalledWith(
      expect.anything(),
      privateWork.scope,
      "document",
    );
    expect(toast.success).toHaveBeenCalledWith("restored version 10");
    expect(routerPush).toHaveBeenCalledWith(`${MEMORY_ROUTE}?version=10`);

    ({ result } = renderCommands(options));
    expect(result.pendingDreamRestoreVersion).toBeNull();
    expect(result.restoringMemoryVersion).toBe(false);
  });

  test("invalidates the Memory root and rethrows restore conflicts", async () => {
    const options = createOptions();
    let { result } = renderCommands(options);
    result.handleDreamRestoreCommand(4);
    ({ result } = renderCommands(options));
    mockedGetProjectMemory.mockResolvedValueOnce({ version: 9 } as never);
    const conflict = new GatewayApiError(
      409,
      "MEMORY_VERSION_CONFLICT",
      "Memory changed",
    );
    mockedRestoreProjectMemoryVersion.mockRejectedValueOnce(conflict);

    await expect(result.confirmDreamRestore()).rejects.toBe(conflict);

    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: projectMemoryRootQueryKey(privateWork.scope),
    });
    expect(toast.error).toHaveBeenCalledWith("Memory changed");
    expect(routerPush).not.toHaveBeenCalled();
  });

  test("invalidates Dream and aborts goal, compact, and restore through cleanup", async () => {
    const options = createOptions();
    let { result } = renderCommands(options);
    const pendingGoal = deferred<Response>();
    mockedFetch.mockReturnValueOnce(pendingGoal.promise);
    const goalRequest = result.handleGoalCommand({ kind: "status" });
    const goalSignal = mockedFetch.mock.calls[0]?.[1]?.signal;

    const pendingCompact = deferred<never>();
    mockedCompactThreadContext.mockReturnValueOnce(pendingCompact.promise);
    const compactRequest = result.handleCompactCommand();
    const compactSignal = mockedCompactThreadContext.mock.calls[0]?.[1]?.signal;

    const pendingDream = deferred<{ disposition: "queued" }>();
    startDreamPreparation.mockReturnValueOnce(pendingDream.promise);
    const dreamRequest = result.handleDreamCommand();

    result.handleDreamRestoreCommand(5);
    ({ result } = renderCommands(options));
    const pendingMemory = deferred<never>();
    mockedGetProjectMemory.mockReturnValueOnce(pendingMemory.promise);
    const restoreRequest = result.confirmDreamRestore();
    const restoreSignal = mockedGetProjectMemory.mock.calls[0]?.[1];

    result.cleanupCommandRequests();

    expect(goalSignal?.aborted).toBe(true);
    expect(compactSignal?.aborted).toBe(true);
    expect(restoreSignal?.aborted).toBe(true);

    const requestAssertions = [
      expect(goalRequest).rejects.toMatchObject({ name: "AbortError" }),
      expect(compactRequest).rejects.toMatchObject({ name: "AbortError" }),
      expect(dreamRequest).rejects.toMatchObject({ name: "AbortError" }),
      expect(restoreRequest).rejects.toMatchObject({ name: "AbortError" }),
    ];
    pendingGoal.resolve(response({ goal }));
    pendingCompact.reject(new DOMException("Aborted", "AbortError"));
    pendingDream.resolve({ disposition: "queued" });
    pendingMemory.reject(new DOMException("Aborted", "AbortError"));
    await Promise.all(requestAssertions);
    expect(options.clearMemoryCommandInput).toHaveBeenCalledTimes(1);
    expect(toast.error).not.toHaveBeenCalled();
  });
});
