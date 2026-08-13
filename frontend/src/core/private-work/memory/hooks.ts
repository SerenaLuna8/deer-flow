"use client";

import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { GatewayApiError } from "@/core/api/errors";
import { useI18n } from "@/core/i18n/hooks";
import {
  dreamProjectMemory,
  getProjectMemory,
  getProjectMemoryVersion,
  listProjectMemoryEpisodes,
  listProjectMemoryPending,
  listProjectMemoryVersions,
  restoreProjectMemoryVersion,
} from "@/core/private-work/memory/api";
import {
  projectMemoryDocumentQueryKey,
  projectMemoryEpisodesQueryKey,
  projectMemoryMutationKey,
  projectMemoryPendingQueryKey,
  projectMemoryRootQueryKey,
  projectMemoryVersionQueryKey,
  projectMemoryVersionsQueryKey,
} from "@/core/private-work/memory/query-keys";
import {
  MEMORY_EPISODE_PAGE_SIZE,
  MEMORY_EPISODE_SEARCH_LIMIT,
  MEMORY_VERSION_PAGE_SIZE,
} from "@/core/private-work/memory/schemas";
import type {
  MemoryEpisodeTag,
  ProjectMemoryPermissions,
} from "@/core/private-work/memory/types";
import { commitProjectMemoryCacheChanges } from "@/core/private-work/memory-freshness";
import type { PrivateWorkAccess } from "@/core/private-work/types";
import { runPrivateWorkAbortable } from "@/core/private-work/types";

import {
  FIRST_MEMORY_VERSION_REQUEST,
  MEMORY_IDLE_REFRESH_INTERVAL,
  memoryEpisodesFilter,
  nextMemoryEpisodePageParam,
  normalizeMemoryEpisodeSearch,
  projectMemoryVersionRequest,
  type MemoryEpisodePageParam,
} from "./query-model";

export type ProjectMemoryQueryModelStatus =
  | { kind: "forbidden" }
  | { kind: "loading" }
  | { kind: "error"; error: Error }
  | { kind: "ready" };

export function useProjectMemoryQueryModel({
  privateWork,
  permissions,
  selectedVersion,
  versionPage,
  selectVersion,
  previousVersionPage,
  nextVersionPage,
}: {
  privateWork: PrivateWorkAccess;
  permissions: ProjectMemoryPermissions;
  selectedVersion: number | null;
  versionPage: number;
  selectVersion: (version: number | null) => void;
  previousVersionPage: () => void;
  nextVersionPage: () => void;
}) {
  const { t } = useI18n();
  const copy = t.projectMemory;
  const queryClient = useQueryClient();
  const scope = privateWork.scope;
  const previousMemorySnapshotRef = useRef<{
    version: number;
    pendingCount: number;
    dreamRunning: boolean;
  } | null>(null);
  const versionRequest = useMemo(
    () => projectMemoryVersionRequest(versionPage),
    [versionPage],
  );

  const documentQuery = useQuery({
    queryKey: projectMemoryDocumentQueryKey(scope),
    queryFn: ({ signal }) => getProjectMemory(privateWork, signal),
    enabled: permissions.canRead,
    refetchInterval: (query) =>
      query.state.data?.dreamRunning ? 2_000 : MEMORY_IDLE_REFRESH_INTERVAL,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
  });
  const versionsQuery = useQuery({
    queryKey: projectMemoryVersionsQueryKey(scope, versionRequest),
    queryFn: ({ signal }) =>
      listProjectMemoryVersions(privateWork, versionRequest, signal),
    enabled: permissions.canRead,
    placeholderData: keepPreviousData,
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
  });
  // Page zero and this latest-summary observer share an identical key, so
  // React Query performs one request rather than a second fetch.
  const latestVersionsQuery = useQuery({
    queryKey: projectMemoryVersionsQueryKey(
      scope,
      FIRST_MEMORY_VERSION_REQUEST,
    ),
    queryFn: ({ signal }) =>
      listProjectMemoryVersions(
        privateWork,
        FIRST_MEMORY_VERSION_REQUEST,
        signal,
      ),
    enabled: permissions.canRead,
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
  });
  const detailQuery = useQuery({
    queryKey:
      selectedVersion === null
        ? [...projectMemoryRootQueryKey(scope), "version", "none"]
        : projectMemoryVersionQueryKey(scope, selectedVersion),
    queryFn: ({ signal }) =>
      getProjectMemoryVersion(privateWork, selectedVersion!, signal),
    enabled: permissions.canRead && selectedVersion !== null,
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
  });
  const pendingQuery = useQuery({
    queryKey: projectMemoryPendingQueryKey(scope),
    queryFn: ({ signal }) => listProjectMemoryPending(privateWork, {}, signal),
    enabled: permissions.canRead,
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
  });

  useEffect(() => {
    if (
      !pendingQuery.data?.items.length ||
      window.location.hash !== "#memory-pending"
    ) {
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      const target = document.getElementById("memory-pending");
      target?.scrollIntoView({ block: "start" });
      target?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [pendingQuery.data?.items.length]);

  const [episodeSearchInput, setEpisodeSearchInput] = useState("");
  const [episodeQuery, setEpisodeQuery] = useState<string | null>(null);
  const [episodeTags, setEpisodeTags] = useState<readonly MemoryEpisodeTag[]>(
    [],
  );
  const episodesFilter = useMemo(
    () => memoryEpisodesFilter(episodeQuery, episodeTags),
    [episodeQuery, episodeTags],
  );
  const episodesQuery = useInfiniteQuery({
    queryKey: projectMemoryEpisodesQueryKey(scope, episodesFilter),
    queryFn: ({ signal, pageParam }) =>
      listProjectMemoryEpisodes(
        privateWork,
        {
          ...episodesFilter,
          ...(pageParam?.kind === "cursor"
            ? { cursor: pageParam.value }
            : pageParam?.kind === "before"
              ? { before: pageParam.value }
              : {}),
          limit: episodeQuery
            ? MEMORY_EPISODE_SEARCH_LIMIT
            : MEMORY_EPISODE_PAGE_SIZE,
        },
        signal,
      ),
    initialPageParam: undefined as MemoryEpisodePageParam | undefined,
    getNextPageParam: (lastPage) =>
      nextMemoryEpisodePageParam(lastPage, episodeQuery !== null),
    enabled: permissions.canRead,
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
  });
  const episodeItems = useMemo(
    () => episodesQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [episodesQuery.data],
  );
  const submitEpisodeSearch = useCallback(() => {
    setEpisodeQuery(normalizeMemoryEpisodeSearch(episodeSearchInput));
  }, [episodeSearchInput]);
  const toggleEpisodeTag = useCallback((tag: MemoryEpisodeTag) => {
    setEpisodeTags((current) =>
      current.includes(tag)
        ? current.filter((value) => value !== tag)
        : [...current, tag],
    );
  }, []);

  const refreshMemory = useCallback(
    () =>
      queryClient.invalidateQueries({
        queryKey: projectMemoryRootQueryKey(scope),
      }),
    [queryClient, scope],
  );
  const refreshOnConflict = useCallback(
    (error: Error) => {
      if (error instanceof GatewayApiError && error.status === 409) {
        void refreshMemory();
      }
    },
    [refreshMemory],
  );

  useEffect(() => {
    const memoryDocument = documentQuery.data;
    if (!memoryDocument) return;
    const next = {
      version: memoryDocument.version,
      pendingCount: memoryDocument.pendingCount,
      dreamRunning: memoryDocument.dreamRunning,
    };
    const previous = previousMemorySnapshotRef.current;
    previousMemorySnapshotRef.current = next;
    if (!previous) return;

    if (previous.version > 0 && next.version === 0) {
      void commitProjectMemoryCacheChanges(queryClient, scope, ["reset"]).catch(
        () => undefined,
      );
      return;
    }
    const changes = new Set<"document" | "pending" | "episodes">();
    if (previous.version !== next.version) {
      changes.add("document");
      changes.add("pending");
      changes.add("episodes");
    } else {
      if (previous.pendingCount !== next.pendingCount) changes.add("pending");
      if (previous.dreamRunning !== next.dreamRunning) changes.add("document");
    }
    if (changes.size > 0) {
      void commitProjectMemoryCacheChanges(queryClient, scope, [
        ...changes,
      ]).catch(() => undefined);
    }
  }, [documentQuery.data, queryClient, scope]);

  const dreamMutation = useMutation({
    mutationKey: projectMemoryMutationKey(scope, "dream"),
    mutationFn: () =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        dreamProjectMemory(privateWork, {}, signal),
      ),
    onSuccess: async (result) => {
      await (result.disposition === "queued"
        ? commitProjectMemoryCacheChanges(queryClient, scope, ["document"])
        : refreshMemory());
      if (result.disposition === "queued") {
        toast.success(
          "admissionKind" in result && result.admissionKind === "budget_rewrite"
            ? copy.dreamQueuedBudget
            : copy.dreamQueuedItems(result.historyCount),
        );
      } else if (result.disposition === "already_running") {
        toast.info(copy.dreamAlreadyRunning);
      } else {
        toast.info(copy.dreamNothingPending);
      }
    },
    onError: (error) => {
      refreshOnConflict(error);
      toast.error(error.message || copy.dreamFailed);
    },
  });

  const restoreMutation = useMutation({
    mutationKey: projectMemoryMutationKey(scope, "restore"),
    mutationFn: (input: {
      targetVersion: number;
      expectedCurrentVersion: number;
    }) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        restoreProjectMemoryVersion(
          privateWork,
          input.targetVersion,
          { expectedCurrentVersion: input.expectedCurrentVersion },
          signal,
        ),
      ),
    onSuccess: async (result) => {
      await commitProjectMemoryCacheChanges(queryClient, scope, ["document"]);
      selectVersion(result.version);
      toast.success(copy.restoreSucceeded(result.version));
    },
    onError: (error) => {
      refreshOnConflict(error);
      toast.error(error.message || copy.restoreFailed);
    },
  });

  const status: ProjectMemoryQueryModelStatus = !permissions.canRead
    ? { kind: "forbidden" }
    : documentQuery.isLoading
      ? { kind: "loading" }
      : documentQuery.error
        ? { kind: "error", error: documentQuery.error }
        : { kind: "ready" };

  return {
    status,
    document: {
      data: documentQuery.data,
      error: documentQuery.error,
      isLoading: documentQuery.isLoading,
      retry: () => void documentQuery.refetch(),
    },
    versions: {
      data: versionsQuery.data?.items.slice(0, MEMORY_VERSION_PAGE_SIZE) ?? [],
      latest: latestVersionsQuery.data?.items[0] ?? null,
      error: versionsQuery.error,
      isLoading: versionsQuery.isLoading,
      retry: () => void versionsQuery.refetch(),
      page: versionPage,
      hasNext:
        (versionsQuery.data?.items.length ?? 0) > MEMORY_VERSION_PAGE_SIZE,
      previous: previousVersionPage,
      next: nextVersionPage,
    },
    detail: {
      data: detailQuery.data,
      error: detailQuery.error,
      isLoading: detailQuery.isLoading,
      retry: () => void detailQuery.refetch(),
      selectedVersion,
      select: selectVersion,
    },
    episodes: {
      items: episodeItems,
      error: episodesQuery.error,
      isLoading: episodesQuery.isLoading,
      retry: () => void episodesQuery.refetch(),
      searchInput: episodeSearchInput,
      setSearchInput: setEpisodeSearchInput,
      submitSearch: submitEpisodeSearch,
      activeQuery: episodeQuery,
      tags: episodeTags,
      toggleTag: toggleEpisodeTag,
      hasMore: Boolean(episodesQuery.hasNextPage),
      loadMore: () => void episodesQuery.fetchNextPage(),
      loadingMore: episodesQuery.isFetchingNextPage,
    },
    pending: {
      items: pendingQuery.data?.items ?? [],
      error: pendingQuery.error,
      isLoading: pendingQuery.isLoading,
      retry: () => void pendingQuery.refetch(),
    },
    actions: {
      canDream: permissions.canDream,
      canRestore: permissions.canRestore && documentQuery.data !== undefined,
      dreaming: dreamMutation.isPending,
      restoringVersion: restoreMutation.isPending
        ? (restoreMutation.variables?.targetVersion ?? null)
        : null,
      dream: () => dreamMutation.mutateAsync().then(() => undefined),
      restore: (version: number) =>
        restoreMutation
          .mutateAsync({
            targetVersion: version,
            expectedCurrentVersion: documentQuery.data?.version ?? 0,
          })
          .then(() => undefined),
    },
  };
}
