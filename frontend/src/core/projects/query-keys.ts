import { projectFiltersSchema, type ProjectFilters } from "./types";

export interface NormalizedProjectFilters {
  readonly query: string | null;
  readonly pinned: boolean | null;
  readonly cursor: string | null;
  readonly limit: number | null;
  readonly includeRecoverable: boolean;
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
    includeRecoverable: parsed.includeRecoverable ?? false,
  });
}

export const projectKeys = {
  workspace: (userId: string) => ["account", userId, "projects"] as const,
  lists: (userId: string) => projectKeys.workspace(userId),
  details: (userId: string) => ["account", userId, "project"] as const,
  myInvitations: (userId: string) =>
    ["account", userId, "project-invitations", "mine"] as const,
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

export function projectBySlugKey(userId: string, slug: string) {
  return [...projectKeys.lists(userId), "slug", slug] as const;
}

export function projectMembersKey(userId: string, projectId: string) {
  return [...projectKeys.details(userId), projectId, "members"] as const;
}

export function projectInvitationKey(userId: string, projectId: string) {
  return [...projectKeys.details(userId), projectId, "invitations"] as const;
}
