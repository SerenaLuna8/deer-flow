"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "@/core/private-work/types";

import {
  createAutomation,
  createAutomationIdempotencyKey,
  deleteAutomation,
  getAutomation,
  listAutomationRuns,
  listAutomations,
  listThreadAutomations,
  pauseAutomation,
  resumeAutomation,
  triggerAutomation,
  updateAutomation,
} from "./api";
import {
  automationMutationKey,
  automationQueryKey,
  automationRoot,
} from "./query-keys";
import {
  automationListFiltersSchema,
  type AutomationListFilters,
  type CreateAutomationInput,
  type UpdateAutomationInput,
} from "./types";

const INACTIVE_ROOT = ["automations", "inactive"] as const;

function sameScope(
  left: ProjectClientScope | null,
  right: ProjectClientScope | null,
): boolean {
  return (
    left !== null &&
    right !== null &&
    left.accountId === right.accountId &&
    left.projectId === right.projectId
  );
}

function requiredScope(access: PrivateWorkAccess): ProjectClientScope {
  if (!access.scope) {
    throw new Error("Project automation scope is unavailable");
  }
  return access.scope;
}

function normalizedFilters(filters: AutomationListFilters = {}) {
  return automationListFiltersSchema.parse(filters);
}

export function projectAutomationsQueryOptions(
  access: PrivateWorkAccess,
  filters: AutomationListFilters = {},
  enabled = true,
) {
  const scope = access.scope;
  const parsed = normalizedFilters(filters);
  return {
    queryKey: scope
      ? automationQueryKey(scope, "list", parsed.limit, parsed.offset)
      : [...INACTIVE_ROOT, "list"],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      listAutomations(requiredScope(access), parsed, signal),
    enabled: enabled && scope !== null,
    retry: false,
  };
}

export function automationMutationOptions<TData, TVariables>(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  action: string,
  operation: (
    scope: ProjectClientScope,
    variables: TVariables,
    signal?: AbortSignal,
  ) => Promise<TData>,
) {
  const originScope = access.scope;
  return {
    mutationKey: originScope
      ? automationMutationKey(originScope, action)
      : [...INACTIVE_ROOT, "mutation", action],
    mutationFn: async (variables: TVariables) => {
      const scope = requiredScope(access);
      return await runPrivateWorkAbortable(access, (signal) =>
        operation(scope, variables, signal),
      );
    },
    onSuccess: async () => {
      if (
        !originScope ||
        !sameScope(originScope, access.scope) ||
        !isPrivateWorkAccessActive(access)
      ) {
        return;
      }
      await queryClient.invalidateQueries({
        queryKey: automationRoot(originScope),
      });
    },
  };
}

export function useProjectAutomations(
  filters: AutomationListFilters = {},
  enabled = true,
) {
  const access = usePrivateWorkAccess();
  return useQuery(projectAutomationsQueryOptions(access, filters, enabled));
}

export function useThreadProjectAutomations(
  threadId: string | null | undefined,
  filters: AutomationListFilters = {},
  enabled = true,
) {
  const access = usePrivateWorkAccess();
  const scope = access.scope;
  const parsed = normalizedFilters(filters);
  return useQuery({
    queryKey: scope
      ? automationQueryKey(
          scope,
          "thread",
          threadId ?? "",
          parsed.limit,
          parsed.offset,
        )
      : [...INACTIVE_ROOT, "thread", threadId ?? ""],
    queryFn: ({ signal }) =>
      listThreadAutomations(
        requiredScope(access),
        threadId ?? "",
        parsed,
        signal,
      ),
    enabled: enabled && scope !== null && Boolean(threadId),
    retry: false,
  });
}

export function useProjectAutomation(
  taskId: string | null | undefined,
  enabled = true,
) {
  const access = usePrivateWorkAccess();
  const scope = access.scope;
  return useQuery({
    queryKey: scope
      ? automationQueryKey(scope, "task", taskId ?? "")
      : [...INACTIVE_ROOT, "task", taskId ?? ""],
    queryFn: ({ signal }) =>
      getAutomation(requiredScope(access), taskId ?? "", signal),
    enabled: enabled && scope !== null && Boolean(taskId),
    retry: false,
  });
}

export function useProjectAutomationRuns(
  taskId: string | null | undefined,
  filters: AutomationListFilters = {},
  enabled = true,
) {
  const access = usePrivateWorkAccess();
  const scope = access.scope;
  const parsed = normalizedFilters(filters);
  return useQuery({
    queryKey: scope
      ? automationQueryKey(
          scope,
          "task",
          taskId ?? "",
          "runs",
          parsed.limit,
          parsed.offset,
        )
      : [...INACTIVE_ROOT, "task", taskId ?? "", "runs"],
    queryFn: ({ signal }) =>
      listAutomationRuns(requiredScope(access), taskId ?? "", parsed, signal),
    enabled: enabled && scope !== null && Boolean(taskId),
    retry: false,
  });
}

function useAutomationMutation<TData, TVariables>(
  action: string,
  operation: (
    scope: ProjectClientScope,
    variables: TVariables,
    signal?: AbortSignal,
  ) => Promise<TData>,
) {
  const queryClient = useQueryClient();
  const access = usePrivateWorkAccess();
  return useMutation(
    automationMutationOptions(queryClient, access, action, operation),
  );
}

export function useCreateProjectAutomation() {
  return useAutomationMutation(
    "create",
    (scope, input: CreateAutomationInput, signal) =>
      createAutomation(scope, input, signal),
  );
}

export function useUpdateProjectAutomation() {
  return useAutomationMutation(
    "update",
    (
      scope,
      variables: { taskId: string; input: UpdateAutomationInput },
      signal,
    ) => updateAutomation(scope, variables.taskId, variables.input, signal),
  );
}

export function useDeleteProjectAutomation() {
  return useAutomationMutation(
    "delete",
    (scope, variables: { taskId: string; expectedVersion: number }, signal) =>
      deleteAutomation(
        scope,
        variables.taskId,
        variables.expectedVersion,
        signal,
      ),
  );
}

export function usePauseProjectAutomation() {
  return useAutomationMutation(
    "pause",
    (scope, variables: { taskId: string; expectedVersion: number }, signal) =>
      pauseAutomation(
        scope,
        variables.taskId,
        variables.expectedVersion,
        signal,
      ),
  );
}

export function useResumeProjectAutomation() {
  return useAutomationMutation(
    "resume",
    (scope, variables: { taskId: string; expectedVersion: number }, signal) =>
      resumeAutomation(
        scope,
        variables.taskId,
        variables.expectedVersion,
        signal,
      ),
  );
}

export function useTriggerProjectAutomation() {
  return useAutomationMutation("trigger", (scope, taskId: string, signal) =>
    triggerAutomation(scope, taskId, createAutomationIdempotencyKey(), signal),
  );
}
