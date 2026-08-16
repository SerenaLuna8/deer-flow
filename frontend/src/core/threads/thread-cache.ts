import type { InfiniteData, QueryClient } from "@tanstack/react-query";

import { privateWorkRoot } from "../private-work/query-keys";
import type { ProjectClientScope } from "../private-work/types";

import { threadContextUsageQueryKey } from "./context-usage";
import { INFINITE_THREADS_QUERY_KEY_PREFIX } from "./thread-lists";
import { scopedThreadQueryKey } from "./thread-query-key";
import { threadTokenUsageQueryKey } from "./token-usage";
import type { AgentThread } from "./types";

function isExactScopedThreadQuery(
  queryKey: readonly unknown[],
  scope: ProjectClientScope,
  threadId: string,
): boolean {
  const root = privateWorkRoot(scope);
  if (
    queryKey.length <= root.length ||
    !root.every((segment, index) => queryKey[index] === segment)
  ) {
    return false;
  }
  return queryKey.slice(root.length).some((segment) => segment === threadId);
}

export async function removeDeletedThreadCaches(
  queryClient: QueryClient,
  threadId: string,
  scope: ProjectClientScope,
): Promise<void> {
  const predicate = (query: { queryKey: readonly unknown[] }) =>
    isExactScopedThreadQuery(query.queryKey, scope, threadId);
  await queryClient.cancelQueries({ predicate });
  queryClient.removeQueries({ predicate });
}

export function upsertThreadInSearchCache(
  queryClient: QueryClient,
  thread: AgentThread,
  scope: ProjectClientScope,
) {
  queryClient.setQueriesData(
    {
      queryKey: scopedThreadQueryKey(scope, "threads", "search"),
      exact: false,
    },
    (oldData: Array<AgentThread> | undefined) => {
      if (!oldData) {
        return [thread];
      }

      const existingIndex = oldData.findIndex(
        (t) => t.thread_id === thread.thread_id,
      );
      if (existingIndex === -1) {
        return [thread, ...oldData];
      }

      return oldData.map((t, index) => {
        if (index !== existingIndex) {
          return t;
        }
        return {
          ...thread,
          ...t,
          metadata: {
            ...(thread.metadata ?? {}),
            ...(t.metadata ?? {}),
          },
          values: {
            ...thread.values,
            ...t.values,
          },
        };
      });
    },
  );
}

export function upsertThreadInInfiniteCache(
  queryClient: QueryClient,
  thread: AgentThread,
  scope: ProjectClientScope,
) {
  queryClient.setQueriesData(
    {
      queryKey: scopedThreadQueryKey(
        scope,
        ...INFINITE_THREADS_QUERY_KEY_PREFIX,
      ),
      exact: false,
    },
    (oldData: InfiniteData<AgentThread[]> | undefined) => {
      if (!oldData) {
        return oldData;
      }

      const merged = oldData.pages.map((page) =>
        page.map((t) =>
          t.thread_id === thread.thread_id
            ? {
                ...thread,
                ...t,
                metadata: {
                  ...(thread.metadata ?? {}),
                  ...(t.metadata ?? {}),
                },
                values: {
                  ...thread.values,
                  ...t.values,
                },
              }
            : t,
        ),
      );

      const exists = merged.some((page) =>
        page.some((t) => t.thread_id === thread.thread_id),
      );
      if (exists) {
        return { ...oldData, pages: merged };
      }

      const firstPage = merged[0] ?? [];
      const restPages = merged.slice(1);
      return {
        ...oldData,
        pages: [[thread, ...firstPage], ...restPages],
      };
    },
  );
}

export function invalidateStoppedThreadCaches(
  queryClient: QueryClient,
  threadId: string | null | undefined,
  isMock = false,
  scope: ProjectClientScope,
) {
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(scope, "threads", "search"),
  });
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(scope, ...INFINITE_THREADS_QUERY_KEY_PREFIX),
  });

  if (!threadId || isMock) {
    return;
  }

  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(scope, "thread", threadId),
  });
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(
      scope,
      "thread",
      "metadata",
      threadId,
      isMock,
    ),
  });
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(
      scope,
      ...threadTokenUsageQueryKey(threadId),
    ),
  });
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(
      scope,
      ...threadContextUsageQueryKey(threadId),
    ),
  });
  void queryClient.invalidateQueries({
    queryKey: scopedThreadQueryKey(scope, "uploads", "list", threadId),
  });
}

export const STOP_THREAD_FINALIZATION_REFETCH_DELAY_MS = 1500;

function scheduleStoppedThreadFinalizationRefetch(
  queryClient: QueryClient,
  threadId: string | null | undefined,
  isMock = false,
  scope: ProjectClientScope,
) {
  if (isMock) {
    return;
  }
  globalThis.setTimeout(() => {
    invalidateStoppedThreadCaches(queryClient, threadId, isMock, scope);
  }, STOP_THREAD_FINALIZATION_REFETCH_DELAY_MS);
}

export async function stopThreadAndInvalidateCaches(
  queryClient: QueryClient,
  stop: () => Promise<void> | void,
  threadId: string | null | undefined,
  isMock = false,
  scope: ProjectClientScope,
) {
  try {
    await stop();
  } finally {
    invalidateStoppedThreadCaches(queryClient, threadId, isMock, scope);
    scheduleStoppedThreadFinalizationRefetch(
      queryClient,
      threadId,
      isMock,
      scope,
    );
  }
}
