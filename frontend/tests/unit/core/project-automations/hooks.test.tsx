import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import type { PrivateWorkAccess } from "@/core/private-work/types";
import {
  AutomationApiError,
  type triggerAutomation,
} from "@/core/project-automations/api";
import {
  AUTOMATION_RUN_REFRESH_INTERVAL_MS,
  automationTriggerMutationOptions,
  automationMutationOptions,
  createAutomationTriggerIdempotencyRegistry,
  projectAutomationRunsQueryOptions,
  projectAutomationsQueryOptions,
} from "@/core/project-automations/hooks";
import { automationRoot } from "@/core/project-automations/query-keys";

import { AUTOMATION_RUN } from "./fixtures";

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const OTHER_SCOPE = {
  accountId: SCOPE.accountId,
  projectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
};
const KEY_ONE = "55555555-5555-4555-8555-555555555555";
const KEY_TWO = "66666666-6666-4666-8666-666666666666";

type TriggerTransport = typeof triggerAutomation;

function access(scope: typeof SCOPE, active = true): PrivateWorkAccess {
  return {
    scope,
    apiBaseURL: `/api/projects/${scope.projectId}/private-work`,
    client: {} as PrivateWorkAccess["client"],
    queryKeyPrefix: [],
    reconnectOnMount: true,
    isActive: () => active,
  };
}

describe("project automation hooks", () => {
  test("derives list options only from the current private-work provider scope", () => {
    const active = projectAutomationsQueryOptions(access(SCOPE));
    expect(active.queryKey).toEqual([...automationRoot(SCOPE), "list", 50, 0]);
    expect(active.enabled).toBe(true);
  });

  test.each(["queued", "launching", "running"] as const)(
    "polls run history while an occurrence is %s",
    (status) => {
      const options = projectAutomationRunsQueryOptions(
        access(SCOPE),
        "task-1",
      );

      expect(
        options.refetchInterval({
          state: { data: [{ ...AUTOMATION_RUN, status }] },
        }),
      ).toBe(AUTOMATION_RUN_REFRESH_INTERVAL_MS);
    },
  );

  test.each([
    "success",
    "failed",
    "skipped",
    "interrupted",
    "cancelled",
    "rejected",
  ] as const)("stops polling run history after %s", (status) => {
    const options = projectAutomationRunsQueryOptions(access(SCOPE), "task-1");

    expect(
      options.refetchInterval({
        state: { data: [{ ...AUTOMATION_RUN, status }] },
      }),
    ).toBe(false);
  });

  test("uses a scoped mutation key and forwards the provider abort signal", async () => {
    const controller = new AbortController();
    const scopedAccess = access(SCOPE);
    scopedAccess.runAbortable = (operation) => operation(controller.signal);
    const operation = rs.fn(async (_scope, _input: string, signal) => {
      expect(signal).toBe(controller.signal);
      return "created";
    });
    const queryClient = new QueryClient();
    const options = automationMutationOptions(
      queryClient,
      scopedAccess,
      "create",
      operation,
    );

    expect(options.mutationKey).toEqual([
      ...automationRoot(SCOPE),
      "mutation",
      "create",
    ]);
    await expect(options.mutationFn("input")).resolves.toBe("created");
    expect(operation).toHaveBeenCalledWith(SCOPE, "input", controller.signal);
  });

  test("does not invalidate after the scope becomes stale", async () => {
    const queryClient = new QueryClient();
    const invalidate = rs.spyOn(queryClient, "invalidateQueries");
    const operation = rs.fn(async () => "result");
    const stale = automationMutationOptions(
      queryClient,
      access(SCOPE, false),
      "pause",
      operation,
    );
    await stale.onSuccess();
    expect(invalidate).not.toHaveBeenCalled();
  });

  test("invalidates only the originating account/project root on current success", async () => {
    const queryClient = new QueryClient();
    const invalidate = rs.spyOn(queryClient, "invalidateQueries");
    const options = automationMutationOptions(
      queryClient,
      access(SCOPE),
      "resume",
      async () => "result",
    );

    await options.onSuccess();
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: automationRoot(SCOPE),
    });
  });

  test("reuses one key for transport retry and explicit retry without caching it", async () => {
    const queryClient = new QueryClient();
    const registry = createAutomationTriggerIdempotencyRegistry();
    const error = new AutomationApiError(
      0,
      "AUTOMATION_NETWORK_ERROR",
      "Automation service is unavailable",
    );
    const transport = rs.fn<TriggerTransport>(async () => {
      throw error;
    });
    const createKey = rs.fn(() => KEY_ONE);
    const options = automationTriggerMutationOptions(
      queryClient,
      access(SCOPE),
      registry,
      transport,
      createKey,
    );
    const mutation = queryClient.getMutationCache().build(queryClient, {
      ...options,
      retry: 1,
      retryDelay: 0,
    });

    await expect(mutation.execute("task-1")).rejects.toBe(error);
    expect(transport.mock.calls.map((call) => call[2])).toEqual([
      KEY_ONE,
      KEY_ONE,
    ]);
    expect(mutation.state.variables).toBe("task-1");
    expect(
      JSON.stringify(
        queryClient
          .getMutationCache()
          .getAll()
          .map((item) => ({
            mutationKey: item.options.mutationKey,
            meta: item.options.meta,
            state: item.state,
          })),
      ),
    ).not.toContain(KEY_ONE);

    await expect(options.mutationFn("task-1")).rejects.toBe(error);
    expect(transport.mock.calls.map((call) => call[2])).toEqual([
      KEY_ONE,
      KEY_ONE,
      KEY_ONE,
    ]);
    expect(createKey).toHaveBeenCalledTimes(1);
  });

  test("rotates the key only after a confirmed success", async () => {
    const registry = createAutomationTriggerIdempotencyRegistry();
    const transport = rs.fn<TriggerTransport>(async () => AUTOMATION_RUN);
    const createKey = rs
      .fn<() => string>()
      .mockReturnValueOnce(KEY_ONE)
      .mockReturnValueOnce(KEY_TWO);
    const options = automationTriggerMutationOptions(
      new QueryClient(),
      access(SCOPE),
      registry,
      transport,
      createKey,
    );

    await options.mutationFn("task-1");
    await options.mutationFn("task-1");

    expect(transport.mock.calls.map((call) => call[2])).toEqual([
      KEY_ONE,
      KEY_TWO,
    ]);
  });

  test("shares a key for concurrent same-task clicks and isolates different tasks", async () => {
    const registry = createAutomationTriggerIdempotencyRegistry();
    const resolvers: Array<(value: typeof AUTOMATION_RUN) => void> = [];
    const transport = rs.fn<TriggerTransport>(
      async () =>
        await new Promise<typeof AUTOMATION_RUN>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    const createKey = rs
      .fn<() => string>()
      .mockReturnValueOnce(KEY_ONE)
      .mockReturnValueOnce(KEY_TWO);
    const options = automationTriggerMutationOptions(
      new QueryClient(),
      access(SCOPE),
      registry,
      transport,
      createKey,
    );

    const first = options.mutationFn("task-1");
    const duplicate = options.mutationFn("task-1");
    const other = options.mutationFn("task-2");
    expect(transport.mock.calls.map((call) => call[2])).toEqual([
      KEY_ONE,
      KEY_ONE,
      KEY_TWO,
    ]);
    for (const resolve of resolvers) resolve(AUTOMATION_RUN);
    await Promise.all([first, duplicate, other]);
  });

  test("scope cleanup isolates a new scope from a late old completion", async () => {
    const registry = createAutomationTriggerIdempotencyRegistry();
    let resolveOld!: (value: typeof AUTOMATION_RUN) => void;
    const oldTransport = rs.fn<TriggerTransport>(
      async () =>
        await new Promise<typeof AUTOMATION_RUN>((resolve) => {
          resolveOld = resolve;
        }),
    );
    const ambiguous = new AutomationApiError(
      0,
      "AUTOMATION_NETWORK_ERROR",
      "Automation service is unavailable",
    );
    const newTransport = rs.fn<TriggerTransport>(async () => {
      throw ambiguous;
    });
    const createKey = rs
      .fn<() => string>()
      .mockReturnValueOnce(KEY_ONE)
      .mockReturnValueOnce(KEY_TWO);
    const oldOptions = automationTriggerMutationOptions(
      new QueryClient(),
      access(SCOPE),
      registry,
      oldTransport,
      createKey,
    );
    const pendingOld = oldOptions.mutationFn("task-1");

    registry.clearScope(SCOPE);
    const newOptions = automationTriggerMutationOptions(
      new QueryClient(),
      access(OTHER_SCOPE),
      registry,
      newTransport,
      createKey,
    );
    await expect(newOptions.mutationFn("task-1")).rejects.toBe(ambiguous);
    resolveOld(AUTOMATION_RUN);
    await pendingOld;
    await expect(newOptions.mutationFn("task-1")).rejects.toBe(ambiguous);

    expect(oldTransport.mock.calls[0]?.[2]).toBe(KEY_ONE);
    expect(newTransport.mock.calls.map((call) => call[2])).toEqual([
      KEY_TWO,
      KEY_TWO,
    ]);
  });

  test("clears on abort and definitive 4xx but retains retryable conflicts", async () => {
    const registry = createAutomationTriggerIdempotencyRegistry();
    const aborted = new DOMException("Aborted", "AbortError");
    const forbidden = new AutomationApiError(
      403,
      "AUTOMATION_FORBIDDEN",
      "Automation action is forbidden.",
    );
    const activeRun = new AutomationApiError(
      409,
      "AUTOMATION_ACTIVE_RUN",
      "Automation has an active run.",
    );
    const transport = rs
      .fn<TriggerTransport>()
      .mockRejectedValueOnce(aborted)
      .mockRejectedValueOnce(forbidden)
      .mockRejectedValueOnce(activeRun)
      .mockRejectedValueOnce(activeRun);
    const createKey = rs
      .fn<() => string>()
      .mockReturnValueOnce(KEY_ONE)
      .mockReturnValueOnce(KEY_TWO)
      .mockReturnValueOnce("77777777-7777-4777-8777-777777777777");
    const options = automationTriggerMutationOptions(
      new QueryClient(),
      access(SCOPE),
      registry,
      transport,
      createKey,
    );

    await expect(options.mutationFn("task-1")).rejects.toBe(aborted);
    await expect(options.mutationFn("task-1")).rejects.toBe(forbidden);
    await expect(options.mutationFn("task-1")).rejects.toBe(activeRun);
    await expect(options.mutationFn("task-1")).rejects.toBe(activeRun);

    expect(transport.mock.calls.map((call) => call[2])).toEqual([
      KEY_ONE,
      KEY_TWO,
      "77777777-7777-4777-8777-777777777777",
      "77777777-7777-4777-8777-777777777777",
    ]);
  });
});
