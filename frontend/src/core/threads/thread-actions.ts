import type { Run } from "@langchain/langgraph-sdk";
import {
  type InfiniteData,
  type QueryClient,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useMemo, useSyncExternalStore } from "react";

import {
  clearProjectThreadRuntimeState,
  deleteProjectThread,
} from "../private-work/api-client";
import { usePrivateWorkAccess } from "../private-work/provider";
import type { ProjectPrivateWorkScope } from "../private-work/types";
import { isSidecarThread, SIDECAR_METADATA_KEY } from "../sidecar/thread";

import { branchThreadFromTurn, fetchThreadTokenUsage } from "./api";
import {
  getThreadContextProjectionReadModel,
  type ContextProjectionReadState,
  type ContextProjectionSubjectRequest,
} from "./context-usage";
import { removeDeletedThreadCaches } from "./thread-cache";
import {
  filterInfiniteThreadsCache,
  INFINITE_THREADS_QUERY_KEY_PREFIX,
  mapInfiniteThreadsCache,
} from "./thread-lists";
import { scopedThreadQueryKey } from "./thread-query-key";
import { threadTokenUsageQueryKey } from "./token-usage";
import type { AgentThread, ThreadTokenUsageResponse } from "./types";

const DISABLED_CONTEXT_PROJECTION_READ_STATE: ContextProjectionReadState =
  Object.freeze({ error: null, isLoading: false });

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

function getErrorString(error: unknown, key: string): string | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  const value = Reflect.get(error, key);
  return typeof value === "string" && value.trim() ? value : undefined;
}

export function projectThreadDeleteErrorMessage(error: unknown): string {
  const status = getHttpStatus(error);
  const code = getErrorString(error, "code");
  const requestId =
    getErrorString(error, "requestId") ?? getErrorString(error, "request_id");

  let message: string;
  if (status === 409 || code === "PRIVATE_WORK_CONFLICT") {
    message = "会话状态已在确认后发生变化，已刷新最新状态，请重新确认删除。";
  } else if (status === 403 || code === "PRIVATE_WORK_FORBIDDEN") {
    message = "你没有权限删除这个会话。";
  } else if (status === 404 || code === "PRIVATE_WORK_NOT_FOUND") {
    message = "这个会话已不存在，会话列表已刷新。";
  } else if (status === 503 || code === "PRIVATE_WORK_UNAVAILABLE") {
    message = "删除服务暂时不可用，请稍后重试。";
  } else if (status === 422 || code === "PRIVATE_WORK_INVALID") {
    message = "删除请求无效，会话状态已刷新，请重新确认。";
  } else {
    message = "删除会话失败，请稍后重试。";
  }

  return requestId ? `${message}（请求编号：${requestId}）` : message;
}

export function isProjectThreadDeleteConflict(error: unknown): boolean {
  return (
    getHttpStatus(error) === 409 ||
    getErrorString(error, "code") === "PRIVATE_WORK_CONFLICT"
  );
}

function isThreadMissingError(error: unknown): boolean {
  const status = getHttpStatus(error);
  // Treat 403 like 404 here to avoid disclosing whether an inaccessible thread
  // exists; callers redirect stale/inaccessible URLs back to a blank chat.
  return status === 403 || status === 404;
}

export type ThreadAvailability =
  | "loading"
  | "available"
  | "not-found"
  | "error";

/**
 * Classify the authoritative Thread metadata read before deriving any
 * thread-scoped UI state. A null result is intentionally shared by 403 and
 * 404 so the UI never reveals whether an inaccessible Thread exists.
 */
export function resolveThreadAvailability({
  data,
  error,
  isLoading,
  isFetching,
}: {
  data: unknown | null | undefined;
  error: unknown;
  isLoading: boolean;
  isFetching: boolean;
}): ThreadAvailability {
  if (isLoading || (isFetching && (data === undefined || data === null))) {
    return "loading";
  }
  if (error) return "error";
  if (data === null) return "not-found";
  if (data === undefined) return "loading";
  return "available";
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
    subject = { kind: "lead_thread" },
    privateWork: explicitPrivateWork,
  }: {
    enabled?: boolean;
    subject?: ContextProjectionSubjectRequest;
    privateWork?: ProjectPrivateWorkScope;
  } = {},
): ContextProjectionReadState {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const subjectExecutionId =
    subject.kind === "subagent_task" ? subject.executionId : null;
  const stableSubject = useMemo<ContextProjectionSubjectRequest>(
    () =>
      subject.kind === "lead_thread"
        ? { kind: "lead_thread" }
        : { kind: "subagent_task", executionId: subjectExecutionId! },
    [subject.kind, subjectExecutionId],
  );
  const readModel = useMemo(
    () =>
      threadId && typeof window !== "undefined"
        ? getThreadContextProjectionReadModel(privateWork, threadId)
        : null,
    [privateWork, threadId],
  );
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      if (!enabled || !readModel) return () => undefined;
      return readModel.subscribe(stableSubject, onStoreChange);
    },
    [enabled, readModel, stableSubject],
  );
  const getSnapshot = useCallback(
    () =>
      enabled && readModel
        ? readModel.getSnapshot(stableSubject)
        : DISABLED_CONTEXT_PROJECTION_READ_STATE,
    [enabled, readModel, stableSubject],
  );
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
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
  threadId: string,
  privateWork: ProjectPrivateWorkScope,
  expectedVersion: number,
  deleteRequest: typeof deleteProjectThread = deleteProjectThread,
  onDeleted?: (deletedThreadId: string) => Promise<void> | void,
) {
  await deleteRequest(privateWork.scope, { threadId, expectedVersion });
  try {
    await onDeleted?.(threadId);
  } catch {
    // The server-side tombstone is already authoritative. A best-effort local
    // teardown failure must not turn a successful irreversible DELETE into a
    // false failure response.
  }
}

export function privateWorkThreadVersion(
  thread: Pick<AgentThread, "metadata">,
): number | null {
  const value = thread.metadata?.private_work_version;
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0
    ? value
    : null;
}

type DiscoveredSidecarThread = Readonly<{
  threadId: string;
  expectedVersion: number;
}>;

type SidecarThreadDiscovery = Readonly<{
  threads: DiscoveredSidecarThread[];
  allThreadIds: string[];
  incomplete: boolean;
}>;

const SIDECAR_SEARCH_PAGE_LIMIT = 100;
const SIDECAR_SEARCH_MAX_PAGES = 1_000;

async function findSidecarThreadsForParent(
  apiClient: ThreadSidecarSearchClient,
  parentThreadId: string,
): Promise<SidecarThreadDiscovery> {
  const threads: DiscoveredSidecarThread[] = [];
  const allThreadIds: string[] = [];
  const seenThreadIds = new Set<string>();
  let incomplete = false;
  let offset = 0;

  for (let page = 0; page < SIDECAR_SEARCH_MAX_PAGES; page += 1) {
    const response = await apiClient.threads.search({
      metadata: {
        [SIDECAR_METADATA_KEY]: true,
        parent_thread_id: parentThreadId,
      },
      limit: SIDECAR_SEARCH_PAGE_LIMIT,
      offset,
      sortBy: "updated_at",
      sortOrder: "desc",
      select: ["thread_id", "metadata"],
    });

    let pageAdvanced = false;
    for (const thread of response) {
      if (seenThreadIds.has(thread.thread_id)) continue;
      seenThreadIds.add(thread.thread_id);
      pageAdvanced = true;
      if (
        isSidecarThread(thread) &&
        thread.metadata?.parent_thread_id === parentThreadId
      ) {
        allThreadIds.push(thread.thread_id);
        const expectedVersion = privateWorkThreadVersion(thread);
        if (expectedVersion == null) {
          incomplete = true;
        } else {
          threads.push({ threadId: thread.thread_id, expectedVersion });
        }
      }
    }

    if (response.length < SIDECAR_SEARCH_PAGE_LIMIT) {
      return { threads, allThreadIds, incomplete };
    }
    if (!pageAdvanced) {
      throw new Error("Sidecar thread pagination made no progress.");
    }
    if (!Number.isSafeInteger(offset + response.length)) {
      throw new Error("Sidecar thread pagination exceeded the offset limit.");
    }
    offset += response.length;
  }

  throw new Error("Sidecar thread pagination exceeded the page limit.");
}

export async function findSidecarThreadIdsForParent(
  apiClient: ThreadSidecarSearchClient,
  parentThreadId: string,
) {
  const discovery = await findSidecarThreadsForParent(
    apiClient,
    parentThreadId,
  );
  return discovery.allThreadIds;
}

type SidecarThreadCleanupResult = Readonly<{
  deletedSidecarThreadIds: string[];
  sidecarCleanupIncomplete: boolean;
}>;

async function deleteDiscoveredSidecarThreads(
  sidecarThreads: DiscoveredSidecarThread[],
  privateWork: ProjectPrivateWorkScope,
  deleteRequest: typeof deleteProjectThread,
  onDeleted?: (deletedThreadId: string) => Promise<void> | void,
): Promise<SidecarThreadCleanupResult> {
  const results = await Promise.allSettled(
    sidecarThreads.map(({ threadId, expectedVersion }) =>
      deleteThreadEverywhere(
        threadId,
        privateWork,
        expectedVersion,
        deleteRequest,
        onDeleted,
      ),
    ),
  );

  const failedDeletionCount = results.filter(
    (result) => result.status === "rejected",
  ).length;

  return {
    deletedSidecarThreadIds: sidecarThreads.flatMap((thread, index) =>
      results[index]?.status === "fulfilled" ? [thread.threadId] : [],
    ),
    sidecarCleanupIncomplete: failedDeletionCount > 0,
  };
}

export async function deleteThreadWithSidecarCleanup(
  apiClient: ThreadSidecarSearchClient,
  threadId: string,
  privateWork: ProjectPrivateWorkScope,
  expectedVersion: number,
  deleteRequest: typeof deleteProjectThread = deleteProjectThread,
  onDeleted?: (deletedThreadId: string) => Promise<void> | void,
): Promise<SidecarThreadCleanupResult> {
  let discovery: SidecarThreadDiscovery | null = null;
  try {
    // Discovery is read-only and may happen first, but the parent is always the
    // first destructive operation. A rejected parent delete therefore leaves
    // every associated sidecar untouched.
    discovery = await findSidecarThreadsForParent(apiClient, threadId);
  } catch {
    // The parent delete can still succeed. Surface the incomplete cleanup to
    // the caller instead of silently claiming a complete cascade.
  }

  await deleteThreadEverywhere(
    threadId,
    privateWork,
    expectedVersion,
    deleteRequest,
    onDeleted,
  );

  if (discovery == null) {
    return {
      deletedSidecarThreadIds: [],
      sidecarCleanupIncomplete: true,
    };
  }
  const cleanup = await deleteDiscoveredSidecarThreads(
    discovery.threads,
    privateWork,
    deleteRequest,
    onDeleted,
  );
  return {
    ...cleanup,
    sidecarCleanupIncomplete:
      discovery.incomplete || cleanup.sidecarCleanupIncomplete,
  };
}

function removeDeletedThreadsFromCatalogCaches(
  queryClient: QueryClient,
  scope: ProjectPrivateWorkScope["scope"],
  deletedThreadIds: ReadonlySet<string>,
): void {
  queryClient.setQueriesData(
    {
      queryKey: scopedThreadQueryKey(scope, "threads", "search"),
      exact: false,
    },
    (oldData: Array<AgentThread> | undefined) => {
      if (oldData == null) return oldData;
      return oldData.filter(
        (thread) => !deletedThreadIds.has(thread.thread_id),
      );
    },
  );
  queryClient.setQueriesData(
    {
      queryKey: scopedThreadQueryKey(
        scope,
        ...INFINITE_THREADS_QUERY_KEY_PREFIX,
      ),
      exact: false,
    },
    (oldData: InfiniteData<AgentThread[]> | undefined) =>
      filterInfiniteThreadsCache(
        oldData,
        (thread) => !deletedThreadIds.has(thread.thread_id),
      ),
  );
}

export function useDeleteThread(explicitPrivateWork?: ProjectPrivateWorkScope) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();
  const apiClient = privateWork.client as ThreadSidecarSearchClient;
  return useMutation({
    mutationFn: async ({
      threadId,
      expectedVersion,
    }: {
      threadId: string;
      expectedVersion: number;
    }) => {
      return deleteThreadWithSidecarCleanup(
        apiClient,
        threadId,
        privateWork,
        expectedVersion,
        deleteProjectThread,
        async (deletedThreadId) => {
          clearProjectThreadRuntimeState(privateWork.scope, deletedThreadId);
          await removeDeletedThreadCaches(
            queryClient,
            deletedThreadId,
            privateWork.scope,
          );
          removeDeletedThreadsFromCatalogCaches(
            queryClient,
            privateWork.scope,
            new Set([deletedThreadId]),
          );
        },
      );
    },
    onSuccess(result, { threadId }) {
      const deletedThreadIds = new Set([
        threadId,
        ...result.deletedSidecarThreadIds,
      ]);
      removeDeletedThreadsFromCatalogCaches(
        queryClient,
        privateWork.scope,
        deletedThreadIds,
      );
    },

    onSettled(_data, error, { threadId }) {
      if (error) {
        void queryClient.invalidateQueries({
          queryKey: scopedThreadQueryKey(
            privateWork.scope,
            "thread",
            "metadata",
            threadId,
          ),
        });
      }
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
