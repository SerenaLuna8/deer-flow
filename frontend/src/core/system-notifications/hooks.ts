"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
  type QueryKey,
  type QueryClient,
} from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef } from "react";

import { projectKeys } from "@/core/projects/query-keys";

import {
  acceptSystemNotification,
  listSystemNotifications,
  markAllSystemNotificationsRead,
} from "./api";
import { systemNotificationKeys } from "./query-keys";

export const SYSTEM_NOTIFICATION_POLL_INTERVAL_MS = 30_000;
export const SYSTEM_NOTIFICATION_PAGE_SIZE = 50;

export function systemNotificationPollInterval(
  visibilityState: DocumentVisibilityState,
): number | false {
  return visibilityState === "visible"
    ? SYSTEM_NOTIFICATION_POLL_INTERVAL_MS
    : false;
}

export interface SystemNotificationMutationToken {
  readonly userId: string | null;
  readonly generation: number;
  readonly controller: AbortController;
  readonly signal: AbortSignal;
}

export interface SystemNotificationScope {
  activate: () => void;
  begin: () => SystemNotificationMutationToken;
  finish: (token: SystemNotificationMutationToken) => void;
  update: (userId: string | null) => void;
  dispose: () => void;
  isCurrent: (token: SystemNotificationMutationToken) => boolean;
}

export function createSystemNotificationScope(
  initialUserId: string | null,
): SystemNotificationScope {
  let userId = initialUserId;
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
        generation,
        controller,
        signal: controller.signal,
      };
    },
    finish(token) {
      controllers.delete(token.controller);
    },
    update(nextUserId) {
      if (nextUserId === userId) return;
      generation += 1;
      userId = nextUserId;
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
        token.userId === userId &&
        token.generation === generation
      );
    },
  };
}

export async function commitSystemNotificationMutation(
  queryClient: QueryClient,
  scope: SystemNotificationScope,
  token: SystemNotificationMutationToken,
  options: { refreshProjects: boolean },
): Promise<boolean> {
  if (!token.userId || !scope.isCurrent(token)) return false;
  const keys: QueryKey[] = [systemNotificationKeys.list(token.userId)];
  if (options.refreshProjects) {
    keys.push(projectKeys.workspace(token.userId));
    keys.push(projectKeys.myInvitations(token.userId));
  }
  await Promise.all(
    keys.map((queryKey) => queryClient.cancelQueries({ queryKey })),
  );
  if (!scope.isCurrent(token)) return false;
  await Promise.all(
    keys.map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
  return scope.isCurrent(token);
}

function useSystemNotificationScope(
  userId: string | null | undefined,
): SystemNotificationScope {
  const scopeRef = useRef<SystemNotificationScope | null>(null);
  scopeRef.current ??= createSystemNotificationScope(userId ?? null);
  const scope = scopeRef.current;

  useLayoutEffect(() => {
    scope.update(userId ?? null);
  }, [scope, userId]);
  useEffect(() => {
    scope.activate();
    return () => scope.dispose();
  }, [scope]);
  return scope;
}

async function runScopedMutation<T>(
  scope: SystemNotificationScope,
  onBegin: (token: SystemNotificationMutationToken) => void,
  operation: (signal: AbortSignal) => Promise<T>,
) {
  const token = scope.begin();
  onBegin(token);
  try {
    return { data: await operation(token.signal), token };
  } finally {
    scope.finish(token);
  }
}

function useScopedNotificationMutation<TVariables, TData>(
  userId: string | null | undefined,
  operation: (variables: TVariables, signal: AbortSignal) => Promise<TData>,
  refreshProjects: boolean,
) {
  const queryClient = useQueryClient();
  const scope = useSystemNotificationScope(userId);
  const observerTokenRef = useRef<SystemNotificationMutationToken | null>(null);
  const mutation = useMutation({
    mutationKey: systemNotificationKeys.mutations(userId ?? ""),
    mutationFn: (variables: TVariables) => {
      if (!userId) throw new Error("Authentication required");
      return runScopedMutation(
        scope,
        (token) => {
          observerTokenRef.current = token;
        },
        (signal) => operation(variables, signal),
      );
    },
    onSuccess: ({ token }) => {
      void commitSystemNotificationMutation(queryClient, scope, token, {
        refreshProjects,
      });
    },
  });
  const observerIsCurrent =
    observerTokenRef.current === null ||
    scope.isCurrent(observerTokenRef.current);
  if (!observerIsCurrent) {
    return {
      ...mutation,
      data: undefined,
      error: null,
      failureCount: 0,
      failureReason: null,
      isError: false,
      isIdle: true,
      isPending: false,
      isSuccess: false,
      status: "idle" as const,
      submittedAt: 0,
      variables: undefined,
    };
  }
  return { ...mutation, data: mutation.data?.data };
}

export function useSystemNotifications(userId: string | null | undefined) {
  const queryClient = useQueryClient();
  useEffect(() => {
    const scopedUserId = userId;
    return () => {
      if (!scopedUserId) return;
      const queryKey = systemNotificationKeys.list(scopedUserId);
      void queryClient.cancelQueries({ queryKey });
      queryClient.removeQueries({ queryKey });
    };
  }, [queryClient, userId]);

  return useInfiniteQuery({
    queryKey: systemNotificationKeys.list(userId ?? ""),
    queryFn: ({ pageParam, signal }) => {
      if (!userId) throw new Error("Authentication required");
      return listSystemNotifications(
        {
          ...(pageParam ? { cursor: pageParam } : {}),
          limit: SYSTEM_NOTIFICATION_PAGE_SIZE,
        },
        signal,
      );
    },
    enabled: Boolean(userId),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    refetchInterval: () =>
      systemNotificationPollInterval(
        typeof document === "undefined" ? "hidden" : document.visibilityState,
      ),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
}

export function useMarkAllSystemNotificationsRead(
  userId: string | null | undefined,
) {
  return useScopedNotificationMutation(
    userId,
    (_input: void, signal) => markAllSystemNotificationsRead(signal),
    false,
  );
}

export function useAcceptSystemNotification(userId: string | null | undefined) {
  return useScopedNotificationMutation(
    userId,
    (input: { notificationId: string; version: number }, signal) =>
      acceptSystemNotification(input.notificationId, input.version, signal),
    true,
  );
}
