"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useImperativeRequest } from "@/core/api/use-imperative-request";
import { modelsQueryKey } from "@/core/models/hooks";

import {
  clearAdminModelApiKey,
  createAdminModel,
  fetchAdminModelCatalog,
  replaceAdminModel,
  runAbortableAdminModelMutation,
  setAdminModelDefault,
  setAdminModelStatus,
  testAdminModelConnection,
} from "./api";
import {
  adminModelMutationKey,
  adminModelSettingsQueryKey,
  adminModelSettingsRoot,
} from "./query-keys";
import {
  adminModelAccountIdSchema,
  type AdminModelDefaultInput,
  type AdminModelStatusInput,
  type CreateAdminModelInput,
  type ReplaceAdminModelInput,
  type TestAdminModelConnectionInput,
} from "./types";

export function adminModelCatalogQueryOptions(accountId: string) {
  const parsed = adminModelAccountIdSchema.parse(accountId);
  return {
    queryKey: adminModelSettingsQueryKey(parsed),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchAdminModelCatalog(parsed, signal),
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function useAdminModelCatalog(accountId: string) {
  return useQuery(adminModelCatalogQueryOptions(accountId));
}

async function invalidateModelCatalogs(
  queryClient: ReturnType<typeof useQueryClient>,
  accountId: string,
): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({
      queryKey: adminModelSettingsRoot(accountId),
    }),
    queryClient.invalidateQueries({ queryKey: modelsQueryKey }),
  ]);
}

export function useCreateAdminModel(accountId: string) {
  const parsed = adminModelAccountIdSchema.parse(accountId);
  const queryClient = useQueryClient();
  return useImperativeRequest(async (input: CreateAdminModelInput) => {
    const result = await runAbortableAdminModelMutation(parsed, (signal) =>
      createAdminModel(parsed, input, signal),
    );
    await invalidateModelCatalogs(queryClient, parsed);
    return result;
  });
}

export function useReplaceAdminModel(accountId: string) {
  const parsed = adminModelAccountIdSchema.parse(accountId);
  const queryClient = useQueryClient();
  return useImperativeRequest(
    async ({
      modelId,
      input,
    }: {
      modelId: string;
      input: ReplaceAdminModelInput;
    }) => {
      const result = await runAbortableAdminModelMutation(parsed, (signal) =>
        replaceAdminModel(parsed, modelId, input, signal),
      );
      await invalidateModelCatalogs(queryClient, parsed);
      return result;
    },
  );
}

export function useClearAdminModelApiKey(accountId: string) {
  const parsed = adminModelAccountIdSchema.parse(accountId);
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: adminModelMutationKey(parsed, "clear_api_key"),
    mutationFn: (modelId: string) =>
      runAbortableAdminModelMutation(parsed, (signal) =>
        clearAdminModelApiKey(parsed, modelId, { confirmed: true }, signal),
      ),
    onSuccess: () => invalidateModelCatalogs(queryClient, parsed),
  });
}

export function useTestAdminModelConnection(accountId: string) {
  const parsed = adminModelAccountIdSchema.parse(accountId);
  return useImperativeRequest((input: TestAdminModelConnectionInput) =>
    runAbortableAdminModelMutation(parsed, (signal) =>
      testAdminModelConnection(parsed, input, signal),
    ),
  );
}

export function useSetAdminModelStatus(accountId: string) {
  const parsed = adminModelAccountIdSchema.parse(accountId);
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: adminModelMutationKey(parsed, "status"),
    mutationFn: ({
      modelId,
      input,
    }: {
      modelId: string;
      input: AdminModelStatusInput;
    }) =>
      runAbortableAdminModelMutation(parsed, (signal) =>
        setAdminModelStatus(parsed, modelId, input, signal),
      ),
    onSuccess: () => invalidateModelCatalogs(queryClient, parsed),
  });
}

export function useSetAdminModelDefault(accountId: string) {
  const parsed = adminModelAccountIdSchema.parse(accountId);
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: adminModelMutationKey(parsed, "default"),
    mutationFn: ({
      modelId,
      input,
    }: {
      modelId: string;
      input: AdminModelDefaultInput;
    }) =>
      runAbortableAdminModelMutation(parsed, (signal) =>
        setAdminModelDefault(parsed, modelId, input, signal),
      ),
    onSuccess: () => invalidateModelCatalogs(queryClient, parsed),
  });
}
