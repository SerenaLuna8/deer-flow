"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo } from "react";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import { projectAssetKey } from "@/core/shared-assets/query-keys";

import {
  AgentBuilderApiError,
  agentBuilderActivityStreamURL,
  cancelAgentBuilderSession,
  finalizeAgentBuilderSession,
  createAgentBuilderSession,
  getAgentBuilderSession,
  getAgentBuilderSessionByAgent,
  listAllAgentBuilderSessions,
  listAllAgentBuilderActivities,
  parseAgentBuilderActivity,
  setAgentBuilderGenerationPreference,
  submitAgentBuilderTurn,
  stopAgentBuilderTurn,
} from "./api";
import {
  agentBuilderMutationKey,
  agentBuilderActivitiesKey,
  agentBuilderSessionKey,
  agentBuilderSessionByAgentKey,
  agentBuilderSessionsInvalidation,
  agentBuilderSessionsKey,
} from "./query-keys";
import type {
  AgentBuilderSession,
  AgentBuilderActivity,
  AgentBuilderSessionSummary,
  AgentBuilderTurnInput,
  AgentBuilderGenerationPreferenceInput,
  CancelAgentBuilderSessionInput,
  CommitAgentBuilderSessionInput,
  CreateAgentBuilderSessionInput,
} from "./types";

function compareActivitySeq(left: string, right: string): number {
  if (left.length !== right.length) return left.length - right.length;
  return left.localeCompare(right);
}

export function mergeAgentBuilderActivities(
  current: readonly AgentBuilderActivity[],
  incoming: readonly AgentBuilderActivity[],
): AgentBuilderActivity[] {
  const bySeq = new Map(current.map((activity) => [activity.seq, activity]));
  for (const activity of incoming) bySeq.set(activity.seq, activity);
  return [...bySeq.values()].sort((left, right) =>
    compareActivitySeq(left.seq, right.seq),
  );
}

export type CancelAgentBuilderSessionFromListInput =
  CancelAgentBuilderSessionInput & {
    session_id: string;
  };

export type AgentBuilderPollingOptions = {
  canAuthor: boolean;
  requestPending?: boolean;
};

export type AgentBuilderSessionQueryOptions = {
  canAuthor: boolean;
  pollWhileRequestPending?: boolean;
};

function useAgentBuilderMutationRunner(accountId: string, projectId: string) {
  const access = usePrivateWorkAccess();
  if (
    access.scope.accountId !== accountId ||
    access.scope.projectId !== projectId
  ) {
    throw new Error("Agent Builder scope does not match the active project");
  }

  function inactiveScopeError() {
    const error = new Error("Agent Builder scope is inactive");
    error.name = "AbortError";
    return error;
  }

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

  return { access, runMutation };
}

export function agentBuilderPollingInterval(
  session: AgentBuilderSession | undefined,
  { canAuthor, requestPending = false }: AgentBuilderPollingOptions,
) {
  return canAuthor &&
    (requestPending ||
      session?.status === "generating" ||
      session?.status === "committing")
    ? 1_000
    : false;
}

export function newestAgentBuilderSession(
  current: AgentBuilderSession | undefined,
  incoming: AgentBuilderSession,
): AgentBuilderSession {
  return !current || incoming.revision >= current.revision ? incoming : current;
}

function isAgentBuilderRevisionConflict(error: unknown) {
  return (
    error instanceof AgentBuilderApiError &&
    error.code === "AGENT_BUILDER_CONFLICT"
  );
}

async function invalidateAgentBuilderConflictQueries(
  queryClient: QueryClient,
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  await Promise.all([
    queryClient.invalidateQueries(
      agentBuilderSessionsInvalidation(accountId, projectId),
    ),
    queryClient.invalidateQueries({
      queryKey: agentBuilderSessionKey(accountId, projectId, sessionId),
      exact: true,
    }),
  ]);
}

export function useAgentBuilderSessions(
  accountId: string,
  projectId: string,
  enabled = true,
) {
  return useQuery({
    queryKey: agentBuilderSessionsKey(accountId, projectId),
    queryFn: ({ signal }) => listAllAgentBuilderSessions(projectId, signal),
    enabled,
  });
}

export function useAgentBuilderSession(
  accountId: string,
  projectId: string,
  sessionId: string,
  {
    canAuthor,
    pollWhileRequestPending = false,
  }: AgentBuilderSessionQueryOptions,
) {
  const queryClient = useQueryClient();
  const key = agentBuilderSessionKey(accountId, projectId, sessionId);
  return useQuery({
    queryKey: key,
    queryFn: ({ signal }) =>
      getAgentBuilderSession(projectId, sessionId, signal).then((response) =>
        newestAgentBuilderSession(
          queryClient.getQueryData<AgentBuilderSession>(key),
          response.data,
        ),
      ),
    refetchInterval: (query) =>
      agentBuilderPollingInterval(query.state.data, {
        canAuthor,
        requestPending: pollWhileRequestPending,
      }),
  });
}

export function useAgentBuilderSessionByAgent(
  accountId: string,
  projectId: string,
  agentId: string,
  enabled = true,
) {
  return useQuery({
    queryKey: agentBuilderSessionByAgentKey(accountId, projectId, agentId),
    queryFn: async ({ signal }) => {
      try {
        return (await getAgentBuilderSessionByAgent(projectId, agentId, signal))
          .data;
      } catch (error) {
        if (
          error instanceof AgentBuilderApiError &&
          error.code === "AGENT_BUILDER_NOT_FOUND"
        ) {
          return null;
        }
        throw error;
      }
    },
    enabled,
    retry: false,
  });
}

export function useAgentBuilderActivities(
  accountId: string,
  projectId: string,
  sessionId: string,
  enabled = true,
) {
  const queryClient = useQueryClient();
  const access = usePrivateWorkAccess();
  const key = useMemo(
    () => agentBuilderActivitiesKey(accountId, projectId, sessionId),
    [accountId, projectId, sessionId],
  );
  const query = useQuery({
    queryKey: key,
    queryFn: async ({ signal }) => {
      const replay = await listAllAgentBuilderActivities(
        projectId,
        sessionId,
        signal,
      );
      // Activity is append-only. A slower REST replay must merge with, rather
      // than replace, newer SSE frames already committed to the cache.
      return mergeAgentBuilderActivities(
        queryClient.getQueryData<AgentBuilderActivity[]>(key) ?? [],
        replay,
      );
    },
    enabled,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (
      !enabled ||
      !query.isSuccess ||
      access.scope.accountId !== accountId ||
      access.scope.projectId !== projectId ||
      !access.subscribeEventStream
    ) {
      return;
    }
    return access.subscribeEventStream(
      agentBuilderActivityStreamURL(projectId, sessionId),
      "activity",
      (data) => {
        if (!isPrivateWorkAccessActive(access)) return;
        try {
          const activity = parseAgentBuilderActivity(JSON.parse(data));
          queryClient.setQueryData<AgentBuilderActivity[]>(key, (current) =>
            mergeAgentBuilderActivities(current ?? [], [activity]),
          );
        } catch {
          // A strict-invalid public frame is ignored; the durable replay query
          // remains authoritative and no raw payload enters cache state.
        }
      },
    );
  }, [
    access,
    accountId,
    enabled,
    key,
    projectId,
    query.isSuccess,
    queryClient,
    sessionId,
  ]);

  return query;
}

export function useCreateAgentBuilderSession(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useAgentBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: agentBuilderMutationKey(
      accountId,
      projectId,
      "create-session",
    ),
    mutationFn: (input: CreateAgentBuilderSessionInput) =>
      runMutation((signal) =>
        createAgentBuilderSession(projectId, input, signal),
      ),
    onSuccess: (response) => {
      queryClient.setQueryData(
        agentBuilderSessionKey(accountId, projectId, response.data.id),
        response.data,
      );
      void queryClient.invalidateQueries(
        agentBuilderSessionsInvalidation(accountId, projectId),
      );
    },
  });
}

export function useSubmitAgentBuilderTurn(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useAgentBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: agentBuilderMutationKey(accountId, projectId, "submit-turn"),
    mutationFn: (input: AgentBuilderTurnInput) =>
      runMutation((signal) =>
        submitAgentBuilderTurn(projectId, sessionId, input, signal),
      ),
    onSuccess: (response) => {
      queryClient.setQueryData<AgentBuilderSession>(
        agentBuilderSessionKey(accountId, projectId, sessionId),
        (current) => newestAgentBuilderSession(current, response.data),
      );
      void queryClient.invalidateQueries(
        agentBuilderSessionsInvalidation(accountId, projectId),
      );
    },
    onError: async (error) => {
      if (!isAgentBuilderRevisionConflict(error)) return;
      await invalidateAgentBuilderConflictQueries(
        queryClient,
        accountId,
        projectId,
        sessionId,
      );
    },
  });
}

export function useStopAgentBuilderTurn(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useAgentBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: agentBuilderMutationKey(accountId, projectId, "stop-turn"),
    mutationFn: () =>
      runMutation((signal) =>
        stopAgentBuilderTurn(projectId, sessionId, signal),
      ),
    onSuccess: (response) => {
      queryClient.setQueryData<AgentBuilderSession>(
        agentBuilderSessionKey(accountId, projectId, sessionId),
        (current) => newestAgentBuilderSession(current, response.data),
      );
    },
  });
}

export function useSetAgentBuilderGenerationPreference(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useAgentBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: agentBuilderMutationKey(
      accountId,
      projectId,
      "set-generation-preference",
    ),
    mutationFn: (input: AgentBuilderGenerationPreferenceInput) =>
      runMutation((signal) =>
        setAgentBuilderGenerationPreference(
          projectId,
          sessionId,
          input,
          signal,
        ),
      ),
    onSuccess: (response) => {
      queryClient.setQueryData<AgentBuilderSession>(
        agentBuilderSessionKey(accountId, projectId, sessionId),
        (current) => newestAgentBuilderSession(current, response.data),
      );
    },
  });
}

export function useCommitAgentBuilderSession(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useAgentBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: agentBuilderMutationKey(
      accountId,
      projectId,
      "commit-session",
    ),
    mutationFn: (input: CommitAgentBuilderSessionInput) =>
      runMutation((signal) =>
        finalizeAgentBuilderSession(projectId, sessionId, input, signal),
      ),
    onSuccess: (response) => {
      queryClient.setQueryData<AgentBuilderSession>(
        agentBuilderSessionKey(accountId, projectId, sessionId),
        (current) => newestAgentBuilderSession(current, response.data.session),
      );
      void Promise.all([
        queryClient.invalidateQueries(
          agentBuilderSessionsInvalidation(accountId, projectId),
        ),
        queryClient.invalidateQueries({
          queryKey: projectAssetKey(accountId, projectId, "agents"),
        }),
      ]);
    },
    onError: async (error) => {
      if (!isAgentBuilderRevisionConflict(error)) return;
      await invalidateAgentBuilderConflictQueries(
        queryClient,
        accountId,
        projectId,
        sessionId,
      );
    },
  });
}

export function useCancelAgentBuilderSession(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useAgentBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: agentBuilderMutationKey(
      accountId,
      projectId,
      "cancel-session",
    ),
    mutationFn: (input: CancelAgentBuilderSessionInput) =>
      runMutation((signal) =>
        cancelAgentBuilderSession(projectId, sessionId, input, signal),
      ),
    onSuccess: (response) => {
      queryClient.setQueryData<AgentBuilderSession>(
        agentBuilderSessionKey(accountId, projectId, sessionId),
        (current) => newestAgentBuilderSession(current, response.data),
      );
      queryClient.removeQueries({
        queryKey: agentBuilderActivitiesKey(accountId, projectId, sessionId),
        exact: true,
      });
      void queryClient.invalidateQueries(
        agentBuilderSessionsInvalidation(accountId, projectId),
      );
    },
    onError: async (error) => {
      if (!isAgentBuilderRevisionConflict(error)) return;
      await invalidateAgentBuilderConflictQueries(
        queryClient,
        accountId,
        projectId,
        sessionId,
      );
    },
  });
}

export function useCancelAgentBuilderSessionFromList(
  accountId: string,
  projectId: string,
) {
  const queryClient = useQueryClient();
  const { runMutation } = useAgentBuilderMutationRunner(accountId, projectId);
  return useMutation({
    mutationKey: agentBuilderMutationKey(
      accountId,
      projectId,
      "cancel-session-from-list",
    ),
    mutationFn: ({
      session_id: sessionId,
      ...input
    }: CancelAgentBuilderSessionFromListInput) =>
      runMutation((signal) =>
        cancelAgentBuilderSession(projectId, sessionId, input, signal),
      ),
    onSuccess: (response, input) => {
      queryClient.setQueryData<AgentBuilderSession>(
        agentBuilderSessionKey(accountId, projectId, input.session_id),
        (current) => newestAgentBuilderSession(current, response.data),
      );
      queryClient.setQueryData<AgentBuilderSessionSummary[]>(
        agentBuilderSessionsKey(accountId, projectId),
        (current) =>
          current?.filter((session) => session.id !== input.session_id),
      );
      queryClient.removeQueries({
        queryKey: agentBuilderActivitiesKey(
          accountId,
          projectId,
          input.session_id,
        ),
        exact: true,
      });
      void queryClient.invalidateQueries(
        agentBuilderSessionsInvalidation(accountId, projectId),
      );
    },
    onError: async (error, input) => {
      if (!isAgentBuilderRevisionConflict(error)) return;
      await invalidateAgentBuilderConflictQueries(
        queryClient,
        accountId,
        projectId,
        input.session_id,
      );
    },
  });
}
