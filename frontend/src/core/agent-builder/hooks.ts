"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import { projectAssetKey } from "@/core/shared-assets/query-keys";

import {
  cancelAgentBuilderSession,
  finalizeAgentBuilderSession,
  createAgentBuilderSession,
  getAgentBuilderSession,
  listAgentBuilderSessions,
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
  AgentBuilderTurnInput,
  CancelAgentBuilderSessionInput,
  CommitAgentBuilderSessionInput,
  CreateAgentBuilderSessionInput,
} from "./types";

function useAgentBuilderMutationRunner(
  accountId: string,
  projectId: string,
) {
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
) {
  return session?.status === "generating" ||
    session?.status === "committing"
    ? 1_000
    : false;
}

export function useAgentBuilderSessions(
  accountId: string,
  projectId: string,
  enabled = true,
) {
  return useQuery({
    queryKey: agentBuilderSessionsKey(accountId, projectId),
    queryFn: ({ signal }) =>
      listAgentBuilderSessions(projectId, signal).then(
        (response) => response.data,
      ),
    enabled,
  });
}

export function useAgentBuilderSession(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  return useQuery({
    queryKey: agentBuilderSessionKey(accountId, projectId, sessionId),
    queryFn: ({ signal }) =>
      getAgentBuilderSession(projectId, sessionId, signal).then(
        (response) => response.data,
      ),
    refetchInterval: (query) =>
      agentBuilderPollingInterval(query.state.data),
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
    mutationKey: agentBuilderMutationKey(
      accountId,
      projectId,
      "submit-turn",
    ),
    mutationFn: (input: AgentBuilderTurnInput) =>
      runMutation((signal) =>
        submitAgentBuilderTurn(projectId, sessionId, input, signal),
      ),
    onSuccess: (response) => {
      queryClient.setQueryData(
        agentBuilderSessionKey(accountId, projectId, sessionId),
        response.data,
      );
      void queryClient.invalidateQueries(
        agentBuilderSessionsInvalidation(accountId, projectId),
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
      queryClient.setQueryData(
        agentBuilderSessionKey(accountId, projectId, sessionId),
        response.data.session,
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
      queryClient.setQueryData(
        agentBuilderSessionKey(accountId, projectId, sessionId),
        response.data,
      );
      void queryClient.invalidateQueries(
        agentBuilderSessionsInvalidation(accountId, projectId),
      );
    },
  });
}
