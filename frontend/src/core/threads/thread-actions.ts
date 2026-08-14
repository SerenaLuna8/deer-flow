import type { Run } from "@langchain/langgraph-sdk";
import {
  type InfiniteData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { usePrivateWorkAccess } from "../private-work/provider";
import type { ProjectPrivateWorkScope } from "../private-work/types";
import { isSidecarThread, SIDECAR_METADATA_KEY } from "../sidecar/thread";

import { branchThreadFromTurn, fetchThreadTokenUsage } from "./api";
import {
  fetchThreadContextUsage,
  threadContextUsageQueryKey,
  type ThreadContextUsageResponse,
} from "./context-usage";
import {
  filterInfiniteThreadsCache,
  INFINITE_THREADS_QUERY_KEY_PREFIX,
  mapInfiniteThreadsCache,
} from "./thread-lists";
import { scopedThreadQueryKey } from "./thread-query-key";
import { threadTokenUsageQueryKey } from "./token-usage";
import type { AgentThread, ThreadTokenUsageResponse } from "./types";

type ThreadDeleteClient = {
  threads: {
    delete: (threadId: string) => Promise<unknown>;
    search: (query: Record<string, unknown>) => Promise<AgentThread[]>;
  };
};

type ThreadSidecarSearchClient = {
  threads: {
    search: (query: Record<string, unknown>) => Promise<AgentThread[]>;
  };
};

function getHttpStatus(error: unknown): number | undefined {
  if (typeof error !== "object" || error === null) {
    return undefined;
  }

  const status = Reflect.get(error, "status");
  if (typeof status === "number") {
    return status;
  }

  const response = Reflect.get(error, "response");
  if (typeof response === "object" && response !== null) {
    const responseStatus = Reflect.get(response, "status");
    if (typeof responseStatus === "number") {
      return responseStatus;
    }
  }

  return undefined;
}

function isThreadMissingError(error: unknown): boolean {
  const status = getHttpStatus(error);
  // Treat 403 like 404 here to avoid disclosing whether an inaccessible thread
  // exists; callers redirect stale/inaccessible URLs back to a blank chat.
  return status === 403 || status === 404;
}

export function useThreadMetadata(
  threadId?: string | null,
  {
    enabled = true,
    isMock = false,
    privateWork: explicitPrivateWork,
  }: {
    enabled?: boolean;
    isMock?: boolean;
    privateWork?: ProjectPrivateWorkScope;
  } = {},
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery<AgentThread | null>({
    queryKey: scopedThreadQueryKey(
      privateWork.scope,
      "thread",
      "metadata",
      threadId,
      isMock,
    ),
    queryFn: async () => {
      if (!threadId) {
        return null;
      }
      try {
        const response = await privateWork.client.threads.get(threadId);
        return response as AgentThread;
      } catch (error) {
        if (isThreadMissingError(error)) {
          return null;
        }
        throw error;
      }
    },
    enabled: enabled && Boolean(threadId),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useThreadTokenUsage(
  threadId?: string | null,
  {
    enabled = true,
    privateWork: explicitPrivateWork,
  }: { enabled?: boolean; privateWork?: ProjectPrivateWorkScope } = {},
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery<ThreadTokenUsageResponse | null>({
    queryKey: scopedThreadQueryKey(
      privateWork.scope,
      ...threadTokenUsageQueryKey(threadId),
    ),
    queryFn: async () => {
      if (!threadId) {
        return null;
      }
      return fetchThreadTokenUsage(threadId, privateWork);
    },
    enabled: enabled && Boolean(threadId),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useThreadContextUsage(
  threadId?: string | null,
  {
    enabled = true,
    privateWork: explicitPrivateWork,
  }: { enabled?: boolean; privateWork?: ProjectPrivateWorkScope } = {},
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery<ThreadContextUsageResponse | null>({
    queryKey: scopedThreadQueryKey(
      privateWork.scope,
      ...threadContextUsageQueryKey(threadId),
    ),
    queryFn: async ({ signal }) => {
      if (!threadId) {
        return null;
      }
      return fetchThreadContextUsage(threadId, {
        apiBaseURL: privateWork.apiBaseURL,
        signal,
      });
    },
    enabled: enabled && Boolean(threadId),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useBranchThread(explicitPrivateWork?: ProjectPrivateWorkScope) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      threadId,
      messageId,
      messageIds,
      title,
    }: {
      threadId: string;
      messageId: string;
      messageIds?: string[];
      title?: string;
    }) =>
      branchThreadFromTurn(
        threadId,
        { messageId, messageIds, title },
        privateWork,
      ),
    onSuccess(response, { threadId }) {
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(
          privateWork.scope,
          "thread",
          "metadata",
          response.thread_id,
        ),
      });
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(
          privateWork.scope,
          "thread",
          "metadata",
          threadId,
        ),
      });
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(privateWork.scope, "threads", "search"),
      });
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(
          privateWork.scope,
          ...INFINITE_THREADS_QUERY_KEY_PREFIX,
        ),
      });
    },
  });
}

export function useRunDetail(
  threadId: string,
  runId: string,
  explicitPrivateWork?: ProjectPrivateWorkScope,
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery<Run>({
    queryKey: scopedThreadQueryKey(
      privateWork.scope,
      "thread",
      threadId,
      "run",
      runId,
    ),
    queryFn: async () => {
      const response = await privateWork.client.runs.get(threadId, runId);
      return response;
    },
    refetchOnWindowFocus: false,
  });
}

export async function deleteThreadEverywhere(
  apiClient: ThreadDeleteClient,
  threadId: string,
  _privateWork: ProjectPrivateWorkScope,
) {
  await apiClient.threads.delete(threadId);
}

export async function findSidecarThreadIdsForParent(
  apiClient: ThreadSidecarSearchClient,
  parentThreadId: string,
) {
  const threadIds: string[] = [];
  const limit = 100;
  let offset = 0;

  while (true) {
    const response = await apiClient.threads.search({
      metadata: {
        [SIDECAR_METADATA_KEY]: true,
        parent_thread_id: parentThreadId,
      },
      limit,
      offset,
      sortBy: "updated_at",
      sortOrder: "desc",
      select: ["thread_id", "metadata"],
    });

    for (const thread of response) {
      if (
        isSidecarThread(thread) &&
        thread.metadata?.parent_thread_id === parentThreadId
      ) {
        threadIds.push(thread.thread_id);
      }
    }

    if (response.length < limit) {
      break;
    }
    offset += response.length;
  }

  return threadIds;
}

async function deleteSidecarThreadsForParent(
  apiClient: ThreadDeleteClient,
  parentThreadId: string,
  privateWork: ProjectPrivateWorkScope,
) {
  let sidecarThreadIds: string[];
  try {
    sidecarThreadIds = await findSidecarThreadIdsForParent(
      apiClient,
      parentThreadId,
    );
  } catch (err) {
    console.warn(
      `Failed to look up sidecar threads for parent ${parentThreadId}; skipping cascade cleanup. Orphaned sidecar threads may remain.`,
      err,
    );
    return [];
  }

  const results = await Promise.allSettled(
    sidecarThreadIds.map((threadId) =>
      deleteThreadEverywhere(apiClient, threadId, privateWork),
    ),
  );

  const failedDeletions = results
    .map((result, index) =>
      result.status === "rejected"
        ? { threadId: sidecarThreadIds[index], reason: result.reason }
        : null,
    )
    .filter((entry): entry is { threadId: string; reason: unknown } =>
      Boolean(entry),
    );

  if (failedDeletions.length > 0) {
    console.warn(
      `Failed to delete ${failedDeletions.length} sidecar thread(s) for parent ${parentThreadId}; orphaned sidecar threads may remain.`,
      failedDeletions,
    );
  }

  return sidecarThreadIds.filter((_, index) => {
    return results[index]?.status === "fulfilled";
  });
}

export function useDeleteThread(explicitPrivateWork?: ProjectPrivateWorkScope) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();
  const apiClient = privateWork.client as ThreadDeleteClient;
  return useMutation({
    mutationFn: async ({
      threadId,
      onRemoteDeleted,
    }: {
      threadId: string;
      onRemoteDeleted?: () => void;
    }) => {
      const deletedSidecarThreadIds = await deleteSidecarThreadsForParent(
        apiClient,
        threadId,
        privateWork,
      );
      await deleteThreadEverywhere(apiClient, threadId, privateWork);
      onRemoteDeleted?.();
      return deletedSidecarThreadIds;
    },
    onSuccess(deletedSidecarThreadIds, { threadId }) {
      const deletedThreadIds = new Set([threadId, ...deletedSidecarThreadIds]);
      queryClient.setQueriesData(
        {
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            "threads",
            "search",
          ),
          exact: false,
        },
        (oldData: Array<AgentThread> | undefined) => {
          if (oldData == null) {
            return oldData;
          }
          return oldData.filter((t) => !deletedThreadIds.has(t.thread_id));
        },
      );
      queryClient.setQueriesData(
        {
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...INFINITE_THREADS_QUERY_KEY_PREFIX,
          ),
          exact: false,
        },
        (oldData: InfiniteData<AgentThread[]> | undefined) =>
          filterInfiniteThreadsCache(
            oldData,
            (t) => !deletedThreadIds.has(t.thread_id),
          ),
      );
    },

    onSettled() {
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(privateWork.scope, "threads", "search"),
      });
      void queryClient.invalidateQueries({
        queryKey: scopedThreadQueryKey(
          privateWork.scope,
          ...INFINITE_THREADS_QUERY_KEY_PREFIX,
        ),
      });
    },
  });
}

export function useRenameThread(explicitPrivateWork?: ProjectPrivateWorkScope) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();
  const apiClient = privateWork.client;
  return useMutation({
    mutationFn: async ({
      threadId,
      title,
    }: {
      threadId: string;
      title: string;
    }) => {
      await apiClient.threads.updateState(threadId, {
        values: { title },
      });
    },
    onSuccess(_, { threadId, title }) {
      queryClient.setQueriesData(
        {
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            "threads",
            "search",
          ),
          exact: false,
        },
        (oldData: Array<AgentThread>) => {
          return oldData.map((t) => {
            if (t.thread_id === threadId) {
              return {
                ...t,
                values: {
                  ...t.values,
                  title,
                },
              };
            }
            return t;
          });
        },
      );
      queryClient.setQueriesData(
        {
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            ...INFINITE_THREADS_QUERY_KEY_PREFIX,
          ),
          exact: false,
        },
        (oldData: InfiniteData<AgentThread[]> | undefined) =>
          mapInfiniteThreadsCache(oldData, (t) =>
            t.thread_id === threadId
              ? {
                  ...t,
                  values: {
                    ...t.values,
                    title,
                  },
                }
              : t,
          ),
      );
    },
  });
}
