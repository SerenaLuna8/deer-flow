"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "@/core/private-work/types";
import { invalidateStartedThreadContextUsage } from "@/core/threads/thread-cache";

import {
  AutomationApiError,
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
  type AutomationErrorCode,
} from "./api";
import {
  automationMutationKey,
  automationQueryKey,
  automationRoot,
} from "./query-keys";
import {
  automationListFiltersSchema,
  type AutomationListFilters,
  type AutomationRun,
  type CreateAutomationInput,
  type UpdateAutomationInput,
} from "./types";

export const AUTOMATION_RUN_REFRESH_INTERVAL_MS = 2_000;

const ACTIVE_AUTOMATION_RUN_STATUSES = new Set<AutomationRun["status"]>([
  "queued",
  "launching",
  "running",
]);

const DEFINITIVE_TRIGGER_ERROR_CODES = new Set<AutomationErrorCode>([
  "AUTOMATION_FORBIDDEN",
  "AUTOMATION_NOT_FOUND",
  "AUTOMATION_INVALID",
  "AUTOMATION_VALIDATION_FAILED",
]);

type AutomationTriggerTransport = typeof triggerAutomation;

export type AutomationTriggerIdempotencyRegistry = {
  acquire(
    scope: ProjectClientScope,
    taskId: string,
    createKey: () => string,
  ): string;
  clear(scope: ProjectClientScope, taskId: string, key?: string): void;
  clearScope(scope: ProjectClientScope): void;
};

function triggerScopeKey(scope: ProjectClientScope): string {
  const root = automationRoot(scope);
  return `${root[1]}:${root[3]}`;
}

export function createAutomationTriggerIdempotencyRegistry(): AutomationTriggerIdempotencyRegistry {
  const keysByScope = new Map<string, Map<string, string>>();

  return {
    acquire(scope, taskId, createKey) {
      const scopeKey = triggerScopeKey(scope);
      let keysByTask = keysByScope.get(scopeKey);
      if (!keysByTask) {
        keysByTask = new Map();
        keysByScope.set(scopeKey, keysByTask);
      }
      const existing = keysByTask.get(taskId);
      if (existing) return existing;

      const key = createKey();
      keysByTask.set(taskId, key);
      return key;
    },
    clear(scope, taskId, key) {
      const scopeKey = triggerScopeKey(scope);
      const keysByTask = keysByScope.get(scopeKey);
      if (!keysByTask) return;
      if (key !== undefined && keysByTask.get(taskId) !== key) return;

      keysByTask.delete(taskId);
      if (keysByTask.size === 0) keysByScope.delete(scopeKey);
    },
    clearScope(scope) {
      keysByScope.delete(triggerScopeKey(scope));
    },
  };
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function isDefinitiveTriggerError(error: unknown): boolean {
  return (
    error instanceof AutomationApiError &&
    error.status >= 400 &&
    error.status < 500 &&
    DEFINITIVE_TRIGGER_ERROR_CODES.has(error.code)
  );
}

function sameScope(
  left: ProjectClientScope,
  right: ProjectClientScope,
): boolean {
  return (
    left.accountId === right.accountId && left.projectId === right.projectId
  );
}

function requiredScope(access: PrivateWorkAccess): ProjectClientScope {
  return access.scope;
}

function normalizedFilters(filters: AutomationListFilters = {}) {
  return automationListFiltersSchema.parse(filters);
}

function hasActiveAutomationRun(runs: AutomationRun[] | undefined): boolean {
  return Boolean(
    runs?.some(({ status }) => ACTIVE_AUTOMATION_RUN_STATUSES.has(status)),
  );
}

export function projectAutomationsQueryOptions(
  access: PrivateWorkAccess,
  filters: AutomationListFilters = {},
  enabled = true,
) {
  const scope = access.scope;
  const parsed = normalizedFilters(filters);
  return {
    queryKey: automationQueryKey(scope, "list", parsed.limit, parsed.offset),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      listAutomations(requiredScope(access), parsed, signal),
    enabled,
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
    mutationKey: automationMutationKey(originScope, action),
    mutationFn: async (variables: TVariables) => {
      const scope = requiredScope(access);
      return await runPrivateWorkAbortable(access, (signal) =>
        operation(scope, variables, signal),
      );
    },
    onSuccess: async () => {
      if (
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

export function automationTriggerMutationOptions(
  queryClient: QueryClient,
  access: PrivateWorkAccess,
  registry: AutomationTriggerIdempotencyRegistry,
  transport: AutomationTriggerTransport = triggerAutomation,
  createKey: () => string = createAutomationIdempotencyKey,
) {
  const originScope = access.scope;
  const options = automationMutationOptions(
    queryClient,
    access,
    "trigger",
    async (scope, taskId: string, signal) => {
      const key = registry.acquire(scope, taskId, createKey);
      try {
        const run = await transport(scope, taskId, key, signal);
        registry.clear(scope, taskId, key);
        return run;
      } catch (error) {
        if (isAbortError(error) || isDefinitiveTriggerError(error)) {
          registry.clear(scope, taskId, key);
        }
        throw error;
      }
    },
  );
  return {
    ...options,
    onSuccess: async (run: AutomationRun) => {
      await options.onSuccess();
      if (
        !sameScope(originScope, access.scope) ||
        !isPrivateWorkAccessActive(access) ||
        !run.thread_id ||
        !run.run_id ||
        !ACTIVE_AUTOMATION_RUN_STATUSES.has(run.status)
      ) {
        return;
      }
      await invalidateStartedThreadContextUsage(
        queryClient,
        run.thread_id,
        false,
        originScope,
      );
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
    queryKey: automationQueryKey(
      scope,
      "thread",
      threadId ?? "",
      parsed.limit,
      parsed.offset,
    ),
    queryFn: ({ signal }) =>
      listThreadAutomations(
        requiredScope(access),
        threadId ?? "",
        parsed,
        signal,
      ),
    enabled: enabled && Boolean(threadId),
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
    queryKey: automationQueryKey(scope, "task", taskId ?? ""),
    queryFn: ({ signal }) =>
      getAutomation(requiredScope(access), taskId ?? "", signal),
    enabled: enabled && Boolean(taskId),
    retry: false,
  });
}

export function projectAutomationRunsQueryOptions(
  access: PrivateWorkAccess,
  taskId: string | null | undefined,
  filters: AutomationListFilters = {},
  enabled = true,
) {
  const scope = access.scope;
  const parsed = normalizedFilters(filters);
  return {
    queryKey: automationQueryKey(
      scope,
      "task",
      taskId ?? "",
      "runs",
      parsed.limit,
      parsed.offset,
    ),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      listAutomationRuns(requiredScope(access), taskId ?? "", parsed, signal),
    enabled: enabled && Boolean(taskId),
    retry: false,
    refetchInterval: (query: { state: { data?: AutomationRun[] } }) =>
      hasActiveAutomationRun(query.state.data)
        ? AUTOMATION_RUN_REFRESH_INTERVAL_MS
        : false,
    refetchIntervalInBackground: false,
  };
}

export function useProjectAutomationRuns(
  taskId: string | null | undefined,
  filters: AutomationListFilters = {},
  enabled = true,
) {
  const access = usePrivateWorkAccess();
  return useQuery(
    projectAutomationRunsQueryOptions(access, taskId, filters, enabled),
  );
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
  const queryClient = useQueryClient();
  const access = usePrivateWorkAccess();
  const registryRef = useRef<AutomationTriggerIdempotencyRegistry | null>(null);
  registryRef.current ??= createAutomationTriggerIdempotencyRegistry();
  const registry = registryRef.current;
  const scope = access.scope;

  useEffect(
    () => () => {
      if (scope) registry.clearScope(scope);
    },
    [registry, scope],
  );

  return useMutation(
    automationTriggerMutationOptions(queryClient, access, registry),
  );
}
