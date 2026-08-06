"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { listPrivacyCases, requestPrivacyEarlyDelete } from "./api";
import {
  privacyCasesQueryKey,
  privacyEarlyDeleteMutationKey,
} from "./query-keys";
import { privacyAccountIdSchema, privacyProjectIdSchema } from "./types";

const mutationControllers = new Map<string, Set<AbortController>>();

async function runAbortableMutation<T>(
  accountId: string,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  const controllers = mutationControllers.get(accountId) ?? new Set();
  controllers.add(controller);
  mutationControllers.set(accountId, controllers);
  try {
    return await operation(controller.signal);
  } finally {
    controllers.delete(controller);
    if (controllers.size === 0) mutationControllers.delete(accountId);
  }
}

export function abortPrivacyCenterAccount(accountId: string): void {
  const parsed = privacyAccountIdSchema.safeParse(accountId);
  if (!parsed.success) return;
  mutationControllers
    .get(parsed.data)
    ?.forEach((controller) => controller.abort());
  mutationControllers.delete(parsed.data);
}

export function privacyCasesQueryOptions(accountId: string) {
  const parsed = privacyAccountIdSchema.parse(accountId);
  return {
    queryKey: privacyCasesQueryKey(parsed),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      listPrivacyCases(parsed, signal),
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function usePrivacyCases(accountId: string) {
  return useQuery(privacyCasesQueryOptions(accountId));
}

export function usePrivacyEarlyDelete(accountId: string, projectId: string) {
  const parsedAccountId = privacyAccountIdSchema.parse(accountId);
  const parsedProjectId = privacyProjectIdSchema.parse(projectId);
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: privacyEarlyDeleteMutationKey(
      parsedAccountId,
      parsedProjectId,
    ),
    mutationFn: () =>
      runAbortableMutation(parsedAccountId, (signal) =>
        requestPrivacyEarlyDelete(parsedAccountId, parsedProjectId, signal),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: privacyCasesQueryKey(parsedAccountId),
      });
    },
  });
}
