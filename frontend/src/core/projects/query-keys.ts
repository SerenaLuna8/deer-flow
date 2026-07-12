import { projectFiltersSchema, type ProjectFilters } from "./types";

export interface NormalizedProjectFilters {
  readonly query: string | null;
  readonly pinned: boolean | null;
  readonly cursor: string | null;
  readonly limit: number | null;
}

export function normalizeProjectFilters(
  filters: ProjectFilters = {},
): NormalizedProjectFilters {
  const parsed = projectFiltersSchema.parse(filters);
  const query = parsed.query?.trim();
  return Object.freeze({
    query: query === undefined || query === "" ? null : query,
    pinned: parsed.pinned ?? null,
    cursor: parsed.cursor ?? null,
    limit: parsed.limit ?? null,
  });
}

export const projectKeys = {
  lists: (userId: string) => ["account", userId, "projects"] as const,
  details: (userId: string) => ["account", userId, "project"] as const,
};

export function accountProjectsKey(
  userId: string,
  filters: ProjectFilters = {},
) {
  return [
    ...projectKeys.lists(userId),
    normalizeProjectFilters(filters),
  ] as const;
}

export function projectDetailKey(userId: string, projectId: string) {
  return [...projectKeys.details(userId), projectId, "detail"] as const;
}
