"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import { projectAssetKey } from "@/core/shared-assets/query-keys";

import {
  cancelSkillBuilderSession,
  commitSkillBuilderSession,
  createSkillBuilderSession,
  getSkillBuilderSession,
  listSkillBuilderSessions,
  submitSkillBuilderTurn,
  validateSkillBuilderSession,
} from "./api";
import {
  skillBuilderMutationKey,
  skillBuilderSessionKey,
  skillBuilderSessionsInvalidation,
  skillBuilderSessionsKey,
} from "./query-keys";
import type {
  CancelSkillBuilderSessionInput,
  CommitSkillBuilderSessionInput,
  CreateSkillBuilderSessionInput,
  SkillBuilderSession,
  ValidateSkillBuilderSessionInput,
} from "./types";

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
  return session?.status === "generating" || session?.status === "committing"
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
    onSuccess: (response) => {
      queryClient.setQueryData(
        skillBuilderSessionKey(accountId, projectId, sessionId),
        response.data,
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

export function useSubmitSkillBuilderTurn(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  return useSessionMutation(
    accountId,
    projectId,
    sessionId,
    "submit-turn",
    submitSkillBuilderTurn,
  );
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
    onSuccess: (response) => {
      queryClient.setQueryData(
        skillBuilderSessionKey(accountId, projectId, sessionId),
        response.data.session,
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
