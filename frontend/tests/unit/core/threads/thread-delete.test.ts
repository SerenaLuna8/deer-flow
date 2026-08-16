import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import type { deleteProjectThread } from "@/core/private-work/api-client";
import type { ProjectClientScope } from "@/core/private-work/types";
import {
  deleteThreadWithSidecarCleanup,
  projectThreadDeleteErrorMessage,
} from "@/core/threads/thread-actions";
import { removeDeletedThreadCaches } from "@/core/threads/thread-cache";
import { scopedThreadQueryKey } from "@/core/threads/thread-query-key";
import type { AgentThread } from "@/core/threads/types";

const PARENT = "11111111-1111-4111-8111-111111111111";
const SIDECAR = "22222222-2222-4222-8222-222222222222";
const OTHER_THREAD = "33333333-3333-4333-8333-333333333333";
const SCOPE: ProjectClientScope = {
  accountId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  projectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
};
const OTHER_SCOPE: ProjectClientScope = {
  ...SCOPE,
  projectId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
};

function sidecarThread(): AgentThread {
  return {
    thread_id: SIDECAR,
    metadata: {
      deerflow_sidecar: true,
      parent_thread_id: PARENT,
      private_work_version: 2,
    },
  } as unknown as AgentThread;
}

describe("project thread deletion", () => {
  test("never deletes a sidecar before the parent delete succeeds", async () => {
    const calls: string[] = [];
    const parentFailure = new Error("parent conflict");
    const apiClient = {
      threads: {
        search: rs.fn(async () => {
          calls.push("search");
          return [sidecarThread()];
        }),
      },
    };
    const deleteRequest: typeof deleteProjectThread = async (_scope, input) => {
      calls.push(`delete:${input.threadId}@${input.expectedVersion}`);
      if (input.threadId === PARENT) throw parentFailure;
    };
    const onDeleted = rs.fn();

    await expect(
      deleteThreadWithSidecarCleanup(
        apiClient,
        PARENT,
        {} as never,
        3,
        deleteRequest,
        onDeleted,
      ),
    ).rejects.toBe(parentFailure);

    expect(calls).toEqual(["search", `delete:${PARENT}@3`]);
    expect(onDeleted).not.toHaveBeenCalled();
  });

  test("reports incomplete sidecar cleanup after the parent was deleted", async () => {
    const calls: string[] = [];
    const apiClient = {
      threads: {
        search: rs.fn(async () => {
          calls.push("search");
          return [sidecarThread()];
        }),
      },
    };
    const deleteRequest: typeof deleteProjectThread = async (_scope, input) => {
      calls.push(`delete:${input.threadId}@${input.expectedVersion}`);
      if (input.threadId === SIDECAR) throw new Error("cleanup failed");
    };
    const onDeleted = rs.fn();

    const result = await deleteThreadWithSidecarCleanup(
      apiClient,
      PARENT,
      {} as never,
      3,
      deleteRequest,
      onDeleted,
    );

    expect(calls).toEqual([
      "search",
      `delete:${PARENT}@3`,
      `delete:${SIDECAR}@2`,
    ]);
    expect(result).toEqual({
      deletedSidecarThreadIds: [],
      sidecarCleanupIncomplete: true,
    });
    expect(onDeleted).toHaveBeenCalledTimes(1);
    expect(onDeleted).toHaveBeenCalledWith(PARENT);
  });

  test("tears down the parent and each successfully deleted sidecar", async () => {
    const deleted: string[] = [];
    const apiClient = {
      threads: {
        search: rs.fn(async () => [sidecarThread()]),
      },
    };
    const deleteRequest: typeof deleteProjectThread = async () => undefined;

    const result = await deleteThreadWithSidecarCleanup(
      apiClient,
      PARENT,
      {} as never,
      3,
      deleteRequest,
      (threadId) => {
        deleted.push(threadId);
      },
    );

    expect(deleted).toEqual([PARENT, SIDECAR]);
    expect(result).toEqual({
      deletedSidecarThreadIds: [SIDECAR],
      sidecarCleanupIncomplete: false,
    });
  });

  test("removes only the exact deleted-thread query subtree", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const targetKeys = [
      scopedThreadQueryKey(SCOPE, "thread", PARENT),
      scopedThreadQueryKey(SCOPE, "thread", "metadata", PARENT, false),
      scopedThreadQueryKey(SCOPE, "thread-token-usage", PARENT),
      scopedThreadQueryKey(SCOPE, "thread-context-usage", PARENT),
      scopedThreadQueryKey(SCOPE, "uploads", "list", PARENT),
      scopedThreadQueryKey(SCOPE, "thread", PARENT, "run", "run-1"),
    ];
    targetKeys.forEach((queryKey) => queryClient.setQueryData(queryKey, true));
    const unrelatedKey = scopedThreadQueryKey(SCOPE, "thread", OTHER_THREAD);
    const otherProjectKey = scopedThreadQueryKey(OTHER_SCOPE, "thread", PARENT);
    const catalogKey = scopedThreadQueryKey(SCOPE, "threads", "search");
    queryClient.setQueryData(unrelatedKey, "same-project-other-thread");
    queryClient.setQueryData(otherProjectKey, "other-project-same-thread");
    queryClient.setQueryData(catalogKey, "catalog");

    let activeSignal: AbortSignal | undefined;
    let markStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      markStarted = resolve;
    });
    const activeKey = scopedThreadQueryKey(SCOPE, "thread", PARENT, "active");
    const activeQuery = queryClient
      .fetchQuery({
        queryKey: activeKey,
        queryFn: ({ signal }) => {
          activeSignal = signal;
          markStarted();
          return new Promise<never>((resolve) => {
            void resolve;
          });
        },
      })
      .catch((error: unknown) => error);
    await started;

    await removeDeletedThreadCaches(queryClient, PARENT, SCOPE);

    expect(activeSignal?.aborted).toBe(true);
    targetKeys.forEach((queryKey) => {
      expect(queryClient.getQueryData(queryKey)).toBeUndefined();
    });
    expect(queryClient.getQueryData(activeKey)).toBeUndefined();
    expect(queryClient.getQueryData(unrelatedKey)).toBe(
      "same-project-other-thread",
    );
    expect(queryClient.getQueryData(otherProjectKey)).toBe(
      "other-project-same-thread",
    );
    expect(queryClient.getQueryData(catalogKey)).toBe("catalog");
    await activeQuery;
    queryClient.clear();
  });

  test("skips a sidecar whose confirmed version is unavailable", async () => {
    const calls: string[] = [];
    const apiClient = {
      threads: {
        search: rs.fn(async () => [
          {
            ...sidecarThread(),
            metadata: {
              deerflow_sidecar: true,
              parent_thread_id: PARENT,
            },
          } as AgentThread,
        ]),
      },
    };
    const deleteRequest: typeof deleteProjectThread = async (_scope, input) => {
      calls.push(`delete:${input.threadId}@${input.expectedVersion}`);
    };

    const result = await deleteThreadWithSidecarCleanup(
      apiClient,
      PARENT,
      {} as never,
      3,
      deleteRequest,
    );

    expect(calls).toEqual([`delete:${PARENT}@3`]);
    expect(result.sidecarCleanupIncomplete).toBe(true);
  });

  test("bounds a duplicate sidecar search page and still deletes the parent first", async () => {
    const calls: string[] = [];
    const repeatedPage = Array.from({ length: 100 }, () => sidecarThread());
    const apiClient = {
      threads: {
        search: rs.fn(async () => repeatedPage),
      },
    };
    const deleteRequest: typeof deleteProjectThread = async (_scope, input) => {
      calls.push(`delete:${input.threadId}@${input.expectedVersion}`);
    };

    const result = await deleteThreadWithSidecarCleanup(
      apiClient,
      PARENT,
      {} as never,
      3,
      deleteRequest,
    );

    expect(apiClient.threads.search).toHaveBeenCalledTimes(2);
    expect(calls).toEqual([`delete:${PARENT}@3`]);
    expect(result).toEqual({
      deletedSidecarThreadIds: [],
      sidecarCleanupIncomplete: true,
    });
  });

  test("renders a conflict with a Chinese retry instruction and request id", () => {
    const error = Object.assign(new Error("Failed to delete project thread"), {
      status: 409,
      code: "PRIVATE_WORK_CONFLICT",
      requestId: "request-delete-1",
    });

    expect(projectThreadDeleteErrorMessage(error)).toBe(
      "会话状态已在确认后发生变化，已刷新最新状态，请重新确认删除。（请求编号：request-delete-1）",
    );
  });
});
