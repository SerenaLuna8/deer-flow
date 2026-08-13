import {
  MEMORY_EPISODE_PAGE_SIZE,
  MEMORY_EPISODE_QUERY_MAX_LENGTH,
  MEMORY_VERSION_PAGE_SIZE,
} from "./schemas";
import type { MemoryEpisodesFilter, MemoryEpisodeTag } from "./types";

export type ProjectMemoryTab = "current" | "archive";
export type MemoryEpisodePageParam = {
  kind: "cursor" | "before";
  value: string;
};

export const FIRST_MEMORY_VERSION_REQUEST = {
  limit: MEMORY_VERSION_PAGE_SIZE + 1,
  offset: 0,
} as const;

export const MEMORY_IDLE_REFRESH_INTERVAL = 15_000;

export function parseProjectMemorySelectedVersion(value: string | null) {
  if (!value || !/^[1-9][0-9]*$/u.test(value)) return null;
  const version = Number(value);
  return Number.isSafeInteger(version) ? version : null;
}

export function parseProjectMemoryTab(value: string | null): ProjectMemoryTab {
  return value === "archive" ? "archive" : "current";
}

export function parseProjectMemoryVersionPage(value: string | null) {
  if (!value || !/^[1-9][0-9]*$/u.test(value)) return 0;
  const page = Number(value);
  return Number.isSafeInteger(page) && page <= 200 ? page : 0;
}

export function projectMemoryVersionRequest(page: number) {
  return page === 0
    ? FIRST_MEMORY_VERSION_REQUEST
    : {
        limit: MEMORY_VERSION_PAGE_SIZE + 1,
        offset: page * MEMORY_VERSION_PAGE_SIZE,
      };
}

export function normalizeMemoryEpisodeSearch(value: string) {
  const trimmed = Array.from(value.trim())
    .slice(0, MEMORY_EPISODE_QUERY_MAX_LENGTH)
    .join("");
  return trimmed || null;
}

export function memoryEpisodesFilter(
  query: string | null,
  tags: readonly MemoryEpisodeTag[],
): MemoryEpisodesFilter {
  return {
    ...(query ? { q: query } : {}),
    ...(tags.length ? { tags: [...tags] } : {}),
  };
}

export function nextMemoryEpisodePageParam(
  lastPage:
    | { items: readonly { occurredAt: string }[] }
    | {
        items: readonly { occurredAt: string }[];
        nextCursor: string | null;
      },
  searching: boolean,
): MemoryEpisodePageParam | undefined {
  if (searching) return undefined;
  if ("nextCursor" in lastPage) {
    return lastPage.nextCursor
      ? { kind: "cursor", value: lastPage.nextCursor }
      : undefined;
  }
  if (lastPage.items.length < MEMORY_EPISODE_PAGE_SIZE) return undefined;
  const lastItem = lastPage.items.at(-1);
  return lastItem ? { kind: "before", value: lastItem.occurredAt } : undefined;
}
