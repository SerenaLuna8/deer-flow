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
} from "@/core/private-work/types";
import { projectAssetKey } from "@/core/shared-assets/query-keys";

import {
  AgentBuilderApiError,
  cancelAgentBuilderSession,
  finalizeAgentBuilderSession,
  createAgentBuilderSession,
  getAgentBuilderSession,
  listAllAgentBuilderSessions,
  submitAgentBuilderTurn,
} from "./api";
import {
  agentBuilderMutationKey,
  agentBuilderSessionKey,
  agentBuilderSessionsInvalidation,
  agentBuilderSessionsKey,
} from "./query-keys";
import type {
  AgentBuilderSession,
  AgentBuilderSessionSummary,
  AgentBuilderTurnInput,
  CancelAgentBuilderSessionInput,
  CommitAgentBuilderSessionInput,
  CreateAgentBuilderSessionInput,
} from "./types";

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
