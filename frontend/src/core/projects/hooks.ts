import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef } from "react";

import {
  createProject,
  enterProject,
  findProjectBySlug,
  getProject,
  listAllProjects,
  pinProject,
  ProjectApiError,
  updateProject,
} from "./api";
import {
  accountProjectsKey,
  projectBySlugKey,
  projectDetailKey,
  projectKeys,
} from "./query-keys";
import type {
  CreateProjectInput,
  PatchProjectInput,
  Project,
  ProjectFilters,
} from "./types";

export interface ProjectMutationToken {
  readonly userId: string | null;
  readonly projectId: string | null;
  readonly generation: number;
  readonly signal: AbortSignal;
  readonly controller: AbortController;
}

export interface ProjectMutationScope {
  activate: () => void;
  begin: () => ProjectMutationToken;
  finish: (token: ProjectMutationToken) => void;
  update: (userId: string | null, projectId: string | null) => void;
  dispose: () => void;
  isCurrent: (token: ProjectMutationToken) => boolean;
}

export function createProjectMutationScope(
  initialUserId: string | null,
  initialProjectId: string | null,
): ProjectMutationScope {
  let userId = initialUserId;
  let projectId = initialProjectId;
  let generation = 0;
  let disposed = false;
  const controllers = new Set<AbortController>();

  const abortAll = () => {
    for (const controller of controllers) controller.abort();
    controllers.clear();
  };

  return {
    activate() {
      if (!disposed) return;
      disposed = false;
      generation += 1;
    },
    begin() {
      const controller = new AbortController();
      controllers.add(controller);
      return {
        userId,
        projectId,
        generation,
        signal: controller.signal,
        controller,
      };
    },
    finish(token) {
      controllers.delete(token.controller);
    },
    update(nextUserId, nextProjectId) {
      if (userId === nextUserId && projectId === nextProjectId) return;
      generation += 1;
      userId = nextUserId;
      projectId = nextProjectId;
      abortAll();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      generation += 1;
      abortAll();
    },
    isCurrent(token) {
      return (
        !disposed &&
        !token.signal.aborted &&
        token.generation === generation &&
        token.userId === userId &&
        token.projectId === projectId
      );
    },
  };
}

export async function commitProjectMutation(
  queryClient: QueryClient,
  scope: ProjectMutationScope,
  token: ProjectMutationToken,
  project: Project,
): Promise<boolean> {
  if (!token.userId || !scope.isCurrent(token)) return false;
  if (token.projectId !== null && token.projectId !== project.id) return false;
  queryClient.setQueryData(projectDetailKey(token.userId, project.id), project);
  await queryClient.invalidateQueries({
    queryKey: projectKeys.lists(token.userId),
  });
  return true;
}

function useProjectMutationScope(
  userId: string | null | undefined,
  projectId: string | null | undefined,
): ProjectMutationScope {
  const scopeRef = useRef<ProjectMutationScope | null>(null);
  scopeRef.current ??= createProjectMutationScope(
    userId ?? null,
    projectId ?? null,
  );
  const scope = scopeRef.current;
  useLayoutEffect(() => {
    scope.update(userId ?? null, projectId ?? null);
  }, [scope, userId, projectId]);
  useEffect(() => {
    scope.activate();
    return () => scope.dispose();
  }, [scope]);
  return scope;
}

export function requireProjectIdentity(
  userId: string | null | undefined,
  projectId?: string | null,
  projectRequired = false,
): { userId: string; projectId: string | null } {
  if (!userId) {
    throw new ProjectApiError(401, "AUTH_REQUIRED", "Authentication required");
  }
  if (projectRequired && !projectId) {
    throw new ProjectApiError(
      422,
      "PROJECT_VALIDATION_FAILED",
      "Project validation failed",
    );
  }
  return { userId, projectId: projectId ?? null };
}

interface ScopedProjectResult {
  project: Project;
  token: ProjectMutationToken;
}

async function runScopedMutation(
  scope: ProjectMutationScope,
  operation: (signal: AbortSignal) => Promise<Project>,
): Promise<ScopedProjectResult> {
  const token = scope.begin();
  try {
    return { project: await operation(token.signal), token };
  } finally {
    scope.finish(token);
  }
}

export function useProjects(
  userId: string | null | undefined,
  filters: ProjectFilters = {},
) {
  return useQuery({
    queryKey: accountProjectsKey(userId ?? "", filters),
    queryFn: ({ signal }) => {
      requireProjectIdentity(userId);
      return listAllProjects(filters, signal);
    },
    enabled: Boolean(userId),
  });
}

export function useProject(
  userId: string | null | undefined,
  projectId: string | null | undefined,
) {
  return useQuery({
    queryKey: projectDetailKey(userId ?? "", projectId ?? ""),
    queryFn: ({ signal }) => {
      const identity = requireProjectIdentity(userId, projectId, true);
      return getProject(identity.projectId ?? "", signal);
    },
    enabled: Boolean(userId && projectId),
  });
}

export function useProjectBySlug(
  userId: string | null | undefined,
  slug: string | null | undefined,
) {
  return useQuery({
    queryKey: projectBySlugKey(userId ?? "", slug ?? ""),
    queryFn: ({ signal }) => {
      requireProjectIdentity(userId);
      if (!slug) {
        throw new ProjectApiError(
          422,
          "PROJECT_VALIDATION_FAILED",
          "Project validation failed",
        );
      }
      return findProjectBySlug(slug, signal);
    },
    enabled: Boolean(userId && slug),
  });
}

export function useCreateProject(userId: string | null | undefined) {
  const queryClient = useQueryClient();
  const scope = useProjectMutationScope(userId, null);
  const mutation = useMutation<ScopedProjectResult, Error, CreateProjectInput>({
    mutationFn: (input) => {
      requireProjectIdentity(userId);
      return runScopedMutation(scope, (signal) => createProject(input, signal));
    },
    onSuccess: ({ project, token }) =>
      commitProjectMutation(queryClient, scope, token, project),
  });
  return { ...mutation, data: mutation.data?.project };
}

export function useUpdateProject(
  userId: string | null | undefined,
  projectId: string | null | undefined,
) {
  const queryClient = useQueryClient();
  const scope = useProjectMutationScope(userId, projectId);
  const mutation = useMutation<ScopedProjectResult, Error, PatchProjectInput>({
    mutationFn: (input) => {
      const identity = requireProjectIdentity(userId, projectId, true);
      return runScopedMutation(scope, (signal) =>
        updateProject(identity.projectId ?? "", input, signal),
      );
    },
    onSuccess: ({ project, token }) =>
      commitProjectMutation(queryClient, scope, token, project),
  });
  return { ...mutation, data: mutation.data?.project };
}

export function useEnterProject(
  userId: string | null | undefined,
  projectId: string | null | undefined,
) {
  const queryClient = useQueryClient();
  const scope = useProjectMutationScope(userId, projectId);
  const mutation = useMutation<ScopedProjectResult, Error, void>({
    mutationFn: () => {
      const identity = requireProjectIdentity(userId, projectId, true);
      return runScopedMutation(scope, (signal) =>
        enterProject(identity.projectId ?? "", signal),
      );
    },
    onSuccess: ({ project, token }) =>
      commitProjectMutation(queryClient, scope, token, project),
  });
  return { ...mutation, data: mutation.data?.project };
}

export function usePinProject(
  userId: string | null | undefined,
  projectId: string | null | undefined,
) {
  const queryClient = useQueryClient();
  const scope = useProjectMutationScope(userId, projectId);
  const mutation = useMutation<ScopedProjectResult, Error, boolean>({
    mutationFn: (pinned) => {
      const identity = requireProjectIdentity(userId, projectId, true);
      return runScopedMutation(scope, (signal) =>
        pinProject(identity.projectId ?? "", pinned, signal),
      );
    },
    onSuccess: ({ project, token }) =>
      commitProjectMutation(queryClient, scope, token, project),
  });
  return { ...mutation, data: mutation.data?.project };
}
