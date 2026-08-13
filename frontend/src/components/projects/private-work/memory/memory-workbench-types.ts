import type {
  MemoryDocument,
  MemoryEpisode,
  MemoryEpisodeTag,
  MemoryPendingEntry,
  MemoryVersionDetail,
  MemoryVersionSummary,
} from "@/core/private-work/memory/types";

export type MemoryWorkbenchTab = "current" | "archive";

export type MemoryQueryState<T> = {
  data: T | undefined;
  error: Error | null;
  isLoading: boolean;
  retry: () => void;
};

export type MemoryDocumentWorkbenchProps = {
  document: MemoryQueryState<MemoryDocument>;
  versions: MemoryQueryState<readonly MemoryVersionSummary[]> & {
    latest: MemoryVersionSummary | null;
    page: number;
    hasNext: boolean;
    previous: () => void;
    next: () => void;
  };
  detail: MemoryQueryState<MemoryVersionDetail> & {
    selectedVersion: number | null;
    select: (version: number | null) => void;
  };
  episodes: {
    items: readonly MemoryEpisode[];
    error: Error | null;
    isLoading: boolean;
    retry: () => void;
    searchInput: string;
    setSearchInput: (value: string) => void;
    submitSearch: () => void;
    activeQuery: string | null;
    tags: readonly MemoryEpisodeTag[];
    toggleTag: (tag: MemoryEpisodeTag) => void;
    hasMore: boolean;
    loadMore: () => void;
    loadingMore: boolean;
  };
  pending: {
    items: readonly MemoryPendingEntry[];
    error: Error | null;
    isLoading: boolean;
    retry: () => void;
  };
  actions: {
    canDream: boolean;
    canRestore: boolean;
    dreaming: boolean;
    restoringVersion: number | null;
    dream: () => Promise<void>;
    restore: (version: number) => Promise<void>;
  };
  activeTab?: MemoryWorkbenchTab;
  onTabChange?: (tab: MemoryWorkbenchTab) => void;
};

export type MemoryDocumentState = MemoryDocumentWorkbenchProps["document"];
export type MemoryVersionsState = MemoryDocumentWorkbenchProps["versions"];
export type MemoryDetailState = MemoryDocumentWorkbenchProps["detail"];
export type MemoryEpisodesState = MemoryDocumentWorkbenchProps["episodes"];
export type MemoryPendingState = MemoryDocumentWorkbenchProps["pending"];
export type MemoryActionsState = MemoryDocumentWorkbenchProps["actions"];
