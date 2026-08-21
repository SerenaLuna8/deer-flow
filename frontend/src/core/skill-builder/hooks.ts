"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import { projectAssetKey } from "@/core/shared-assets/query-keys";

import {
  cancelSkillBuilderSession,
  commitSkillBuilderSession,
  createSkillBuilderRevisionSession,
  createSkillBuilderSession,
  getSkillBuilderSession,
  listSkillBuilderActivities,
  listSkillBuilderSessions,
  submitSkillBuilderTurn,
  setSkillBuilderExecutionPreference,
  skillBuilderActivityStreamURL,
  parseSkillBuilderActivity,
  stopSkillBuilderTurn,
  validateSkillBuilderSession,
} from "./api";
import {
  skillBuilderMutationKey,
  skillBuilderActivitiesKey,
  skillBuilderSessionKey,
  skillBuilderSessionsInvalidation,
  skillBuilderSessionsKey,
} from "./query-keys";
import {
  consumeSkillBuilderRunStream,
  type SkillBuilderRunStreamFrame,
} from "./run-stream";
import { isSkillBuilderRunAdmission } from "./types";
import type {
  CancelSkillBuilderSessionInput,
  CommitSkillBuilderSessionInput,
  CreateSkillBuilderRevisionInput,
  CreateSkillBuilderSessionInput,
  SkillBuilderSession,
  SkillBuilderSessionSummary,
  SkillBuilderRunStreamProjection,
  SkillBuilderActivity,
  SkillBuilderTurnInput,
  SetSkillBuilderExecutionPreferenceInput,
  ValidateSkillBuilderSessionInput,
} from "./types";

function compareActivitySeq(left: string, right: string): number {
  if (left.length !== right.length) return left.length - right.length;
  return left.localeCompare(right);
}

export function mergeSkillBuilderActivities(
  current: readonly SkillBuilderActivity[],
  incoming: readonly SkillBuilderActivity[],
): SkillBuilderActivity[] {
  const bySeq = new Map(current.map((activity) => [activity.seq, activity]));
  for (const activity of incoming) bySeq.set(activity.seq, activity);
  return [...bySeq.values()].sort((left, right) =>
    compareActivitySeq(left.seq, right.seq),
  );
}

export function useSkillBuilderRunStream({
  threadId,
  runId,
  initialStatus,
  enabled,
}: {
  threadId: string | null;
  runId: string | null;
  initialStatus: "pending" | "running";
  enabled: boolean;
}): SkillBuilderRunStreamProjection | null {
  const access = usePrivateWorkAccess();
  const [projection, setProjection] =
    useState<SkillBuilderRunStreamProjection | null>(null);
  const initialStatusRef = useRef(initialStatus);
  initialStatusRef.current = initialStatus;

  useEffect(() => {
    if (!threadId || !runId) {
      setProjection(null);
      return;
    }
    if (!enabled || !isPrivateWorkAccessActive(access)) return;
    const controller = new AbortController();
    setProjection(null);
    void consumeSkillBuilderRunStream({
      runId,
      initialStatus: initialStatusRef.current,
      signal: controller.signal,
      open: () =>
        access.client.runs.joinStream(threadId, runId, {
          signal: controller.signal,
        }) as unknown as AsyncIterable<SkillBuilderRunStreamFrame>,
      onProjection: (next) => {
        if (!controller.signal.aborted && isPrivateWorkAccessActive(access)) {
          setProjection(next);
        }
      },
    });
    return () => controller.abort();
  }, [access, enabled, runId, threadId]);

  return projection?.runId === runId ? projection : null;
}

export type CancelSkillBuilderSessionFromListInput =
  CancelSkillBuilderSessionInput & {
    session_id: string;
  };

type SkillBuilderSessionCacheUpdate = (
  current: SkillBuilderSession | undefined,
) => SkillBuilderSession | undefined;

/**
 * Cancel an older exact-session read before committing mutation authority.
 *
 * A turn admission can add `activeRun` without returning the new durable
 * session revision. Without cancellation, a GET started before the mutation
 * can arrive afterwards and remove that admission from the cache, which also
 * stops polling when the stale session was otherwise idle.
 */
export async function updateSkillBuilderSessionCacheAfterMutation(
  queryClient: QueryClient,
  accountId: string,
  projectId: string,
  sessionId: string,
  update: SkillBuilderSessionCacheUpdate,
): Promise<void> {
  const key = skillBuilderSessionKey(accountId, projectId, sessionId);
  await queryClient.cancelQueries({ queryKey: key, exact: true });
  queryClient.setQueryData<SkillBuilderSession>(key, update);
}

function useSkillBuilderMutationRunner(accountId: string, projectId: string) {
  const access = usePrivateWorkAccess();
  if (
    access.scope.accountId !== accountId ||
    access.scope.projectId !== projectId
  ) {
    throw new Error("Skill Builder scope does not match the active project");
  }

  const inactiveScopeError = () => {
    const error = new Error("Skill Builder scope is inactive");
    error.name = "AbortError";
    return error;
  };

  async function runMutation<T>(
    operation: (signal?: AbortSignal) => Promise<T>,
  ) {
    try {
      const result = await runPrivateWorkAbortable(access, operation);
      if (!isPrivateWorkAccessActive(access)) throw inactiveScopeError();
      return result;
    } catch (error) {
      if (!isPrivateWorkAccessActive(access)) throw inactiveScopeError();
      throw error;
    }
  }

  return { runMutation };
}

export function skillBuilderPollingInterval(
  session: SkillBuilderSession | undefined,
) {
  return session?.activeRun ||
    session?.status === "generating" ||
    session?.status === "committing"
    ? 1_000
    : false;
}

export function useSkillBuilderSessions(
  accountId: string,
  projectId: string,
  enabled = true,
) {
  return useQuery({
    queryKey: skillBuilderSessionsKey(accountId, projectId),
    queryFn: ({ signal }) =>
      listSkillBuilderSessions(projectId, signal).then(
        (response) => response.data,
      ),
    enabled,
  });
}

export function useSkillBuilderActivities(
  accountId: string,
  projectId: string,
  sessionId: string,
  enabled = true,
) {
  const queryClient = useQueryClient();
  const access = usePrivateWorkAccess();
  const key = useMemo(
    () => skillBuilderActivitiesKey(accountId, projectId, sessionId),
    [accountId, projectId, sessionId],
  );
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) =>
      listSkillBuilderActivities(projectId, sessionId, "0", signal).then(
        (response) => response.data,
      ),
    enabled,
  });

  useEffect(() => {
    if (
      !enabled ||
      access.scope.accountId !== accountId ||
      access.scope.projectId !== projectId ||
      !access.subscribeEventStream
    ) {
      return;
    }
    return access.subscribeEventStream(
      skillBuilderActivityStreamURL(projectId, sessionId),
      "activity",
      (data) => {
        if (!isPrivateWorkAccessActive(access)) return;
        try {
          const activity = parseSkillBuilderActivity(JSON.parse(data));
          queryClient.setQueryData<SkillBuilderActivity[]>(key, (current) =>
            mergeSkillBuilderActivities(current ?? [], [activity]),
          );
        } catch {
          // Strict-invalid frames never enter the Builder-owned query cache.
        }
      },
    );
  }, [access, accountId, enabled, key, projectId, queryClient, sessionId]);

  return query;
}

export function useSkillBuilderSession(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  return useQuery({
    queryKey: skillBuilderSessionKey(accountId, projectId, sessionId),
    queryFn: ({ signal }) =>
      getSkillBuilderSession(projectId, sessionId, signal).then(
        (response) => response.data,
      ),
    refetchInterval: (query) => skillBuilderPollingInterval(query.state.data),
  });
}

function useSessionMutation<
  TInput,
  TResponse extends { data: SkillBuilderSession },
>(
  accountId: string,
  projectId: string,
  sessionId: string,
  action: string,
  operation: (
    projectId: string,
    sessionId: string,
    input: TInput,
    signal?: AbortSignal,
  ) => Promise<TResponse>,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useSkillBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: skillBuilderMutationKey(accountId, projectId, action),
    mutationFn: (input: TInput) =>
      runMutation((signal) => operation(projectId, sessionId, input, signal)),
    onSuccess: async (response) => {
      await updateSkillBuilderSessionCacheAfterMutation(
        queryClient,
        accountId,
        projectId,
        sessionId,
        () => response.data,
      );
      void queryClient.invalidateQueries(
        skillBuilderSessionsInvalidation(accountId, projectId),
      );
    },
  });
}

export function useCreateSkillBuilderSession(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useSkillBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: skillBuilderMutationKey(
      accountId,
      projectId,
      "create-session",
    ),
    mutationFn: (input: CreateSkillBuilderSessionInput) =>
      runMutation((signal) =>
        createSkillBuilderSession(projectId, input, signal),
      ),
    onSuccess: (response) => {
      queryClient.setQueryData(
        skillBuilderSessionKey(accountId, projectId, response.data.id),
        response.data,
      );
      void queryClient.invalidateQueries(
        skillBuilderSessionsInvalidation(accountId, projectId),
      );
    },
  });
}

export function useCreateSkillBuilderRevisionSession(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useSkillBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: skillBuilderMutationKey(
      accountId,
      projectId,
      "create-revision-session",
    ),
    mutationFn: (input: CreateSkillBuilderRevisionInput) =>
      runMutation((signal) =>
        createSkillBuilderRevisionSession(projectId, input, signal),
      ),
    onSuccess: (response) => {
      queryClient.setQueryData(
        skillBuilderSessionKey(accountId, projectId, response.data.id),
        response.data,
      );
      void queryClient.invalidateQueries(
        skillBuilderSessionsInvalidation(accountId, projectId),
      );
    },
  });
}

export function useSubmitSkillBuilderTurn(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useSkillBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: skillBuilderMutationKey(accountId, projectId, "submit-turn"),
    mutationFn: (input: SkillBuilderTurnInput) =>
      runMutation((signal) =>
        submitSkillBuilderTurn(projectId, sessionId, input, signal),
      ),
    onSuccess: async (response) => {
      if (isSkillBuilderRunAdmission(response)) {
        await updateSkillBuilderSessionCacheAfterMutation(
          queryClient,
          accountId,
          projectId,
          sessionId,
          (current) =>
            current ? { ...current, activeRun: response } : current,
        );
      } else {
        await updateSkillBuilderSessionCacheAfterMutation(
          queryClient,
          accountId,
          projectId,
          sessionId,
          () => response.data,
        );
      }
      void queryClient.invalidateQueries(
        skillBuilderSessionsInvalidation(accountId, projectId),
      );
    },
  });
}

export function useSetSkillBuilderExecutionPreference(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  return useSessionMutation<
    SetSkillBuilderExecutionPreferenceInput,
    Awaited<ReturnType<typeof setSkillBuilderExecutionPreference>>
  >(
    accountId,
    projectId,
    sessionId,
    "set-execution-preference",
    setSkillBuilderExecutionPreference,
  );
}

export function useStopSkillBuilderTurn(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useSkillBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: skillBuilderMutationKey(accountId, projectId, "stop-turn"),
    mutationFn: () =>
      runMutation((signal) =>
        stopSkillBuilderTurn(projectId, sessionId, signal),
      ),
    onSuccess: async (response) => {
      await updateSkillBuilderSessionCacheAfterMutation(
        queryClient,
        accountId,
        projectId,
        sessionId,
        () => response.data,
      );
      void queryClient.invalidateQueries(
        skillBuilderSessionsInvalidation(accountId, projectId),
      );
    },
  });
}

export function useValidateSkillBuilderSession(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  return useSessionMutation<
    ValidateSkillBuilderSessionInput,
    Awaited<ReturnType<typeof validateSkillBuilderSession>>
  >(accountId, projectId, sessionId, "validate", validateSkillBuilderSession);
}

export function useCancelSkillBuilderSession(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  return useSessionMutation<
    CancelSkillBuilderSessionInput,
    Awaited<ReturnType<typeof cancelSkillBuilderSession>>
  >(accountId, projectId, sessionId, "cancel", cancelSkillBuilderSession);
}

export function useCancelSkillBuilderSessionFromList(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useSkillBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: skillBuilderMutationKey(
      accountId,
      projectId,
      "cancel-session-from-list",
    ),
    mutationFn: ({
      session_id: sessionId,
      ...input
    }: CancelSkillBuilderSessionFromListInput) =>
      runMutation((signal) =>
        cancelSkillBuilderSession(projectId, sessionId, input, signal),
      ),
    onSuccess: async (response, input) => {
      await updateSkillBuilderSessionCacheAfterMutation(
        queryClient,
        accountId,
        projectId,
        input.session_id,
        () => response.data,
      );
      queryClient.setQueryData<SkillBuilderSessionSummary[]>(
        skillBuilderSessionsKey(accountId, projectId),
        (current) =>
          current?.filter((session) => session.id !== input.session_id),
      );
      void queryClient.invalidateQueries(
        skillBuilderSessionsInvalidation(accountId, projectId),
      );
    },
  });
}

export function useCommitSkillBuilderSession(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useSkillBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: skillBuilderMutationKey(accountId, projectId, "commit"),
    mutationFn: (input: CommitSkillBuilderSessionInput) =>
      runMutation((signal) =>
        commitSkillBuilderSession(projectId, sessionId, input, signal),
      ),
    onSuccess: async (response) => {
      await updateSkillBuilderSessionCacheAfterMutation(
        queryClient,
        accountId,
        projectId,
        sessionId,
        () => response.data.session,
      );
      void Promise.all([
        queryClient.invalidateQueries(
          skillBuilderSessionsInvalidation(accountId, projectId),
        ),
        queryClient.invalidateQueries({
          queryKey: projectAssetKey(accountId, projectId, "skills"),
        }),
      ]);
    },
  });
}
