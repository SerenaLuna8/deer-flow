"use client";

import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import {
  MemoryDocumentWorkbench,
  type MemoryWorkbenchTab,
} from "@/components/projects/private-work/memory/memory-document-workbench";
import { GatewayApiError } from "@/core/api/errors";
import { useI18n } from "@/core/i18n/hooks";
import {
  dreamProjectMemory,
  getProjectMemory,
  getProjectMemoryVersion,
  listProjectMemoryEpisodes,
  listProjectMemoryPending,
  listProjectMemoryVersions,
  MEMORY_EPISODE_PAGE_SIZE,
  MEMORY_EPISODE_QUERY_MAX_LENGTH,
  MEMORY_EPISODE_SEARCH_LIMIT,
  MEMORY_VERSION_PAGE_SIZE,
  projectMemoryDocumentQueryKey,
  projectMemoryEpisodesQueryKey,
  projectMemoryMutationKey,
  projectMemoryPendingQueryKey,
  projectMemoryPermissions,
  projectMemoryRootQueryKey,
  projectMemoryVersionQueryKey,
  projectMemoryVersionsQueryKey,
  restoreProjectMemoryVersion,
  type MemoryEpisodesFilter,
  type MemoryEpisodeTag,
} from "@/core/private-work/memory";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { runPrivateWorkAbortable } from "@/core/private-work/types";
import type { Project } from "@/core/projects/types";

function parseSelectedVersion(value: string | null) {
  if (!value || !/^[1-9][0-9]*$/u.test(value)) return null;
  const version = Number(value);
  return Number.isSafeInteger(version) ? version : null;
}

function parseActiveTab(value: string | null): MemoryWorkbenchTab {
  return value === "archive" ? "archive" : "current";
}

const FIRST_VERSION_REQUEST = {
  limit: MEMORY_VERSION_PAGE_SIZE + 1,
  offset: 0,
} as const;

export function ProjectMemoryPage({ project }: { project: Project }) {
  const { locale } = useI18n();
  const privateWork = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  const router = useRouter();
  const searchParams = useSearchParams();
  const scope = privateWork.scope;
  const permissions = projectMemoryPermissions(project.capabilities);
  const previousDreamRunningRef = useRef(false);
  const selectedVersion = parseSelectedVersion(searchParams.get("version"));
  const activeTab = parseActiveTab(searchParams.get("tab"));
  const [versionPage, setVersionPage] = useState(0);
  const versionRequest = useMemo(
    () =>
      versionPage === 0
        ? FIRST_VERSION_REQUEST
        : {
            limit: MEMORY_VERSION_PAGE_SIZE + 1,
            offset: versionPage * MEMORY_VERSION_PAGE_SIZE,
          },
    [versionPage],
  );

  const replaceMemoryQuery = useCallback(
    (mutate: (parameters: URLSearchParams) => void) => {
      const parameters = new URLSearchParams(searchParams.toString());
      mutate(parameters);
      const query = parameters.toString();
      router.replace(
        `/projects/${encodeURIComponent(project.slug)}/memory${query ? `?${query}` : ""}`,
        { scroll: false },
      );
    },
    [project.slug, router, searchParams],
  );

  const selectVersion = useCallback(
    (version: number | null) => {
      replaceMemoryQuery((parameters) => {
        if (version === null) parameters.delete("version");
        else parameters.set("version", String(version));
      });
    },
    [replaceMemoryQuery],
  );

  const selectTab = useCallback(
    (tab: MemoryWorkbenchTab) => {
      replaceMemoryQuery((parameters) => {
        if (tab === "archive") parameters.set("tab", "archive");
        else parameters.delete("tab");
      });
    },
    [replaceMemoryQuery],
  );

  const documentQuery = useQuery({
    queryKey: projectMemoryDocumentQueryKey(scope),
    queryFn: ({ signal }) => getProjectMemory(privateWork, signal),
    enabled: permissions.canRead,
    refetchInterval: (query) =>
      query.state.data?.dreamRunning ? 2_000 : false,
    refetchIntervalInBackground: false,
  });
  const versionsQuery = useQuery({
    queryKey: projectMemoryVersionsQueryKey(scope, versionRequest),
    queryFn: ({ signal }) =>
      listProjectMemoryVersions(privateWork, versionRequest, signal),
    enabled: permissions.canRead,
    placeholderData: keepPreviousData,
  });
  // Keep the actual latest summary live while the user pages through older
  // versions. On page zero this shares the same React Query key/request as the
  // paginated observer, so it does not add a duplicate network fetch.
  const latestVersionsQuery = useQuery({
    queryKey: projectMemoryVersionsQueryKey(scope, FIRST_VERSION_REQUEST),
    queryFn: ({ signal }) =>
      listProjectMemoryVersions(privateWork, FIRST_VERSION_REQUEST, signal),
    enabled: permissions.canRead,
  });
  const detailQuery = useQuery({
    queryKey:
      selectedVersion === null
        ? [...projectMemoryRootQueryKey(scope), "version", "none"]
        : projectMemoryVersionQueryKey(scope, selectedVersion),
    queryFn: ({ signal }) =>
      getProjectMemoryVersion(privateWork, selectedVersion!, signal),
    enabled: permissions.canRead && selectedVersion !== null,
  });
  const pendingQuery = useQuery({
    queryKey: projectMemoryPendingQueryKey(scope),
    queryFn: ({ signal }) => listProjectMemoryPending(privateWork, {}, signal),
    enabled: permissions.canRead,
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
  const episodesFilter = useMemo<MemoryEpisodesFilter>(
    () => ({
      ...(episodeQuery ? { q: episodeQuery } : {}),
      ...(episodeTags.length ? { tags: [...episodeTags] } : {}),
    }),
    [episodeQuery, episodeTags],
  );
  const episodesQuery = useInfiniteQuery({
    queryKey: projectMemoryEpisodesQueryKey(scope, episodesFilter),
    queryFn: ({ signal, pageParam }) =>
      listProjectMemoryEpisodes(
        privateWork,
        {
          ...episodesFilter,
          ...(pageParam ? { before: pageParam } : {}),
          limit: episodeQuery
            ? MEMORY_EPISODE_SEARCH_LIMIT
            : MEMORY_EPISODE_PAGE_SIZE,
        },
        signal,
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => {
      if (episodeQuery) return undefined;
      if (lastPage.items.length < MEMORY_EPISODE_PAGE_SIZE) return undefined;
      return lastPage.items[lastPage.items.length - 1]?.occurredAt;
    },
    enabled: permissions.canRead,
  });
  const episodeItems = useMemo(
    () => episodesQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [episodesQuery.data],
  );
  const submitEpisodeSearch = useCallback(() => {
    const trimmed = episodeSearchInput
      .trim()
      .slice(0, MEMORY_EPISODE_QUERY_MAX_LENGTH);
    setEpisodeQuery(trimmed ? trimmed : null);
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
    const dreamRunning = documentQuery.data?.dreamRunning ?? false;
    const justFinished = previousDreamRunningRef.current && !dreamRunning;
    previousDreamRunningRef.current = dreamRunning;
    if (justFinished) void refreshMemory();
  }, [documentQuery.data?.dreamRunning, refreshMemory]);

  const dreamMutation = useMutation({
    mutationKey: projectMemoryMutationKey(scope, "dream"),
    mutationFn: () =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        dreamProjectMemory(privateWork, {}, signal),
      ),
    onSuccess: async (result) => {
      await refreshMemory();
      if (result.disposition === "queued") {
        toast.success(
          "admissionKind" in result && result.admissionKind === "budget_rewrite"
            ? locale === "zh-CN"
              ? "已开始将记忆文档压缩到当前注入预算。"
              : "Started compressing the Memory document into the current injection budget."
            : locale === "zh-CN"
              ? `已开始整理 ${result.historyCount} 条记忆。`
              : `Started organizing ${result.historyCount} Memory items.`,
        );
      } else if (result.disposition === "already_running") {
        toast.info(
          locale === "zh-CN"
            ? "记忆整理任务正在运行。"
            : "A Memory organization job is already running.",
        );
      } else {
        toast.info(
          locale === "zh-CN"
            ? "当前没有等待整理的记忆。"
            : "There is no Memory waiting to be organized.",
        );
      }
    },
    onError: (error) => {
      refreshOnConflict(error);
      toast.error(
        error.message ||
          (locale === "zh-CN"
            ? "记忆整理启动失败。"
            : "Memory organization failed."),
      );
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
      await refreshMemory();
      selectVersion(result.version);
      toast.success(
        locale === "zh-CN"
          ? `已恢复为新版本 ${result.version}。`
          : `Restored as new version ${result.version}.`,
      );
    },
    onError: (error) => {
      refreshOnConflict(error);
      toast.error(
        error.message ||
          (locale === "zh-CN"
            ? "记忆版本恢复失败。"
            : "Memory restore failed."),
      );
    },
  });

  return (
    <MemoryDocumentWorkbench
      activeTab={activeTab}
      onTabChange={selectTab}
      document={{
        data: documentQuery.data,
        error: documentQuery.error,
        isLoading: documentQuery.isLoading,
        retry: () => void documentQuery.refetch(),
      }}
      versions={{
        data:
          versionsQuery.data?.items.slice(0, MEMORY_VERSION_PAGE_SIZE) ?? [],
        latest: latestVersionsQuery.data?.items[0] ?? null,
        error: versionsQuery.error,
        isLoading: versionsQuery.isLoading,
        retry: () => void versionsQuery.refetch(),
        page: versionPage,
        hasNext:
          (versionsQuery.data?.items.length ?? 0) > MEMORY_VERSION_PAGE_SIZE,
        previous: () => setVersionPage((current) => Math.max(0, current - 1)),
        next: () => setVersionPage((current) => current + 1),
      }}
      detail={{
        data: detailQuery.data,
        error: detailQuery.error,
        isLoading: detailQuery.isLoading,
        retry: () => void detailQuery.refetch(),
        selectedVersion,
        select: selectVersion,
      }}
      episodes={{
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
      }}
      pending={{
        items: pendingQuery.data?.items ?? [],
        error: pendingQuery.error,
        isLoading: pendingQuery.isLoading,
        retry: () => void pendingQuery.refetch(),
      }}
      actions={{
        canDream: permissions.canDream,
        canRestore: permissions.canRestore && documentQuery.data !== undefined,
        dreaming: dreamMutation.isPending,
        restoringVersion: restoreMutation.isPending
          ? (restoreMutation.variables?.targetVersion ?? null)
          : null,
        dream: () => dreamMutation.mutateAsync().then(() => undefined),
        restore: (version) =>
          restoreMutation
            .mutateAsync({
              targetVersion: version,
              expectedCurrentVersion: documentQuery.data?.version ?? 0,
            })
            .then(() => undefined),
      }}
    />
  );
}
